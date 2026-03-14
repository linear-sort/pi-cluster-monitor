from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from pathlib import Path
import socket
import ssl
from typing import Any

import httpx
from pydantic import ValidationError

from app.db import fetch_all_dict, get_conn, utc_now_iso
from app.models import AgentMetrics
from app.secret_store import open_secret
from app.services.alerts import evaluate_metric_thresholds, evaluate_offline_alert

logger = logging.getLogger(__name__)


class PollingService:
    def __init__(
        self,
        db_path: Path,
        base_tick_seconds: int = 2,
        timeout_seconds: int = 4,
        poll_failure_threshold: int = 3,
        poll_circuit_cooldown_seconds: int = 60,
        metric_retention_hours: int = 48,
        alert_event_retention_days: int = 14,
        service_retention_days: int = 7,
        cleanup_interval_seconds: int = 300,
        webhook_url: str = "",
        webhook_timeout_seconds: int = 3,
        webhook_retry_base_seconds: int = 15,
        webhook_max_attempts: int = 5,
        webhook_dispatch_interval_seconds: int = 5,
        node_token_key: str = "",
    ) -> None:
        self.db_path = db_path
        self.base_tick_seconds = max(1, base_tick_seconds)
        self.timeout_seconds = timeout_seconds
        self.poll_failure_threshold = max(1, poll_failure_threshold)
        self.poll_circuit_cooldown_seconds = max(5, poll_circuit_cooldown_seconds)
        self.metric_retention_hours = max(1, metric_retention_hours)
        self.alert_event_retention_days = max(1, alert_event_retention_days)
        self.service_retention_days = max(1, service_retention_days)
        self.cleanup_interval_seconds = max(30, cleanup_interval_seconds)
        self.webhook_url = webhook_url.strip()
        self.webhook_timeout_seconds = max(1, webhook_timeout_seconds)
        self.webhook_retry_base_seconds = max(3, webhook_retry_base_seconds)
        self.webhook_max_attempts = max(1, webhook_max_attempts)
        self.webhook_dispatch_interval_seconds = max(1, webhook_dispatch_interval_seconds)
        self.node_token_key = node_token_key.strip()
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._last_poll: dict[int, float] = {}
        self._last_cleanup_epoch: float = 0.0
        self._last_webhook_dispatch_epoch: float = 0.0
        self._node_runtime: dict[int, dict[str, float | int]] = {}
        self._telemetry: dict[str, Any] = {
            "poll": {"attempts": 0, "successes": 0, "failures": 0},
            "cleanup": {"runs": 0},
            "services": {"fetch_failures": 0},
            "webhook": {"attempts": 0, "successes": 0, "failures": 0, "dead_letters": 0},
            "recent_errors": [],
            "updated_at": utc_now_iso(),
        }

    def _bump(self, bucket: str, key: str, amount: int = 1) -> None:
        section = self._telemetry.setdefault(bucket, {})
        section[key] = int(section.get(key, 0)) + amount
        self._telemetry["updated_at"] = utc_now_iso()

    def _record_error(self, component: str, category: str, message: str) -> None:
        errors = self._telemetry.setdefault("recent_errors", [])
        errors.append(
            {
                "timestamp": utc_now_iso(),
                "component": component,
                "category": category,
                "message": message[:300],
            }
        )
        if len(errors) > 50:
            del errors[:-50]
        self._telemetry["updated_at"] = utc_now_iso()

    def get_diagnostics(self) -> dict[str, Any]:
        return {
            "poll": dict(self._telemetry.get("poll", {})),
            "cleanup": dict(self._telemetry.get("cleanup", {})),
            "services": dict(self._telemetry.get("services", {})),
            "webhook": dict(self._telemetry.get("webhook", {})),
            "recent_errors": list(self._telemetry.get("recent_errors", [])),
            "node_runtime_count": len(self._node_runtime),
            "updated_at": str(self._telemetry.get("updated_at") or utc_now_iso()),
        }

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            await self._task

    async def _run(self) -> None:
        timeout = httpx.Timeout(self.timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            while not self._stop_event.is_set():
                await self._poll_due_nodes(client)
                await self._run_retention_cleanup_if_due()
                await self._dispatch_webhooks_if_due()
                await asyncio.sleep(self.base_tick_seconds)

    async def _poll_due_nodes(self, client: httpx.AsyncClient) -> None:
        with get_conn(self.db_path) as conn:
            nodes = fetch_all_dict(
                conn,
                """
                SELECT id, ip_address, token, poll_interval_seconds, enabled, agent_port, revoked_at
                       , use_tls, tls_verify, tls_ca_path, tls_fingerprint_sha256, collect_mode
                FROM nodes
                WHERE enabled = 1
                  AND collect_mode IN ('pull', 'hybrid')
                """,
            )

        now_epoch = datetime.now(timezone.utc).timestamp()
        tasks = []

        for node in nodes:
            node_id = int(node["id"])
            poll_interval = max(3, int(node["poll_interval_seconds"]))
            state = self._node_runtime.get(node_id, {})
            circuit_until = float(state.get("circuit_until", 0.0))
            if now_epoch < circuit_until:
                continue
            last_polled = self._last_poll.get(node_id, 0.0)
            if now_epoch - last_polled < poll_interval:
                continue
            self._last_poll[node_id] = now_epoch
            if node.get("revoked_at"):
                tasks.append(
                    self._record_failure(
                        node_id=node_id,
                        category="auth_failure",
                        message="token revoked",
                        reachable=True,
                    )
                )
                continue
            tasks.append(self._poll_node(client, node))

        if tasks:
            await asyncio.gather(*tasks)

    async def _poll_node(self, client: httpx.AsyncClient, node: dict[str, Any]) -> None:
        node_id = int(node["id"])
        self._bump("poll", "attempts")
        ip = node["ip_address"]
        token = open_secret(str(node["token"] or ""), self.node_token_key)
        if not token:
            await self._record_failure(node_id, "auth_failure", "missing_or_unreadable_node_token", reachable=True)
            return
        port = int(node.get("agent_port") or 8001)
        use_tls = bool(int(node.get("use_tls") or 0))
        tls_verify = bool(int(node.get("tls_verify") or 1))
        tls_ca_path = str(node.get("tls_ca_path") or "").strip()
        tls_fingerprint_sha256 = str(node.get("tls_fingerprint_sha256") or "").strip()
        scheme = "https" if use_tls else "http"
        base_url = f"{scheme}://{ip}:{port}"
        headers = {"Authorization": f"Bearer {token}"}
        request_client = client
        dedicated_client: httpx.AsyncClient | None = None

        if use_tls:
            if tls_fingerprint_sha256:
                server_fp = await self._fetch_tls_fingerprint(ip, port)
                expected_fp = self._normalize_fingerprint(tls_fingerprint_sha256)
                if not server_fp:
                    await self._record_failure(node_id, "tls_verify_error", "unable_to_read_peer_certificate", reachable=False)
                    return
                if server_fp != expected_fp:
                    await self._record_failure(
                        node_id,
                        "tls_verify_error",
                        f"fingerprint_mismatch expected={expected_fp} actual={server_fp}",
                        reachable=True,
                    )
                    return

            if not tls_verify:
                dedicated_client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_seconds), verify=False)
                request_client = dedicated_client
            elif tls_ca_path:
                if not Path(tls_ca_path).exists():
                    await self._record_failure(
                        node_id,
                        "tls_config_error",
                        f"tls_ca_path_not_found={tls_ca_path}",
                        reachable=True,
                    )
                    return
                dedicated_client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_seconds), verify=tls_ca_path)
                request_client = dedicated_client

        try:
            health_response = await request_client.get(f"{base_url}/health", headers=headers)
            if health_response.status_code in (401, 403):
                await self._record_failure(node_id, "auth_failure", "health unauthorized", reachable=True)
                return
            health_response.raise_for_status()

            response = await request_client.get(f"{base_url}/api/v1/metrics", headers=headers)
            if response.status_code in (401, 403):
                await self._record_failure(node_id, "auth_failure", "metrics unauthorized", reachable=True)
                return
            response.raise_for_status()
            metrics_payload = response.json()
            metrics = AgentMetrics.model_validate(metrics_payload)
            services_payload = await self._fetch_services(request_client, base_url, headers)
            await self._record_success(node_id, metrics, metrics_payload, services_payload)
        except ValidationError as exc:
            logger.warning("poll metrics parse error node_id=%s error=%s", node_id, exc)
            await self._record_failure(node_id, "metrics_parse_error", str(exc), reachable=True)
        except httpx.TimeoutException as exc:
            logger.warning("poll timeout node_id=%s error=%s", node_id, exc)
            await self._record_failure(node_id, "timeout", str(exc), reachable=False)
        except httpx.ConnectError as exc:
            logger.warning("poll connect error node_id=%s error=%s", node_id, exc)
            await self._record_failure(node_id, "offline", str(exc), reachable=False)
        except httpx.TransportError as exc:
            msg = str(exc)
            category = "tls_verify_error" if "CERTIFICATE_VERIFY_FAILED" in msg else "transport_error"
            logger.warning("poll transport error node_id=%s category=%s error=%s", node_id, category, msg)
            await self._record_failure(node_id, category, msg, reachable=False)
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            category = "auth_failure" if status_code in (401, 403) else "agent_http_error"
            logger.warning("poll http status error node_id=%s category=%s status=%s", node_id, category, status_code)
            await self._record_failure(node_id, category, f"http_status={status_code}", reachable=True)
        except Exception as exc:
            logger.exception("poll unexpected error node_id=%s", node_id)
            await self._record_failure(node_id, "unknown_error", str(exc), reachable=False)
        finally:
            if dedicated_client is not None:
                await dedicated_client.aclose()

    @staticmethod
    def _normalize_fingerprint(value: str) -> str:
        return value.strip().lower().replace(":", "")

    async def _fetch_tls_fingerprint(self, host: str, port: int) -> str | None:
        def _sync_fetch() -> str | None:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            try:
                with socket.create_connection((host, port), timeout=self.timeout_seconds) as sock:
                    with context.wrap_socket(sock, server_hostname=host) as tls_sock:
                        cert_bin = tls_sock.getpeercert(binary_form=True)
                if not cert_bin:
                    return None
                return hashlib.sha256(cert_bin).hexdigest().lower()
            except Exception:
                return None

        return await asyncio.to_thread(_sync_fetch)

    async def _fetch_services(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        headers: dict[str, str],
    ) -> list[dict[str, Any]]:
        try:
            response = await client.get(f"{base_url}/api/v1/services", headers=headers)
            if response.status_code == 404:
                return []
            response.raise_for_status()
            payload = response.json()
            services = payload.get("services")
            if isinstance(services, list):
                return [item for item in services if isinstance(item, dict)]
        except Exception:
            self._bump("services", "fetch_failures")
            logger.exception("poll services fetch failed base_url=%s", base_url)
            return []
        return []

    async def _record_success(
        self,
        node_id: int,
        metrics: AgentMetrics,
        raw_payload: dict[str, Any],
        services_payload: list[dict[str, Any]] | None = None,
    ) -> None:
        with get_conn(self.db_path) as conn:
            now_iso = utc_now_iso()
            conn.execute(
                """
                UPDATE nodes
                SET last_seen_at = ?, last_heartbeat_at = ?, last_status = 'online',
                    last_error_category = NULL, last_error_message = NULL, last_poll_error_at = NULL,
                    consecutive_failures = 0, updated_at = ?
                WHERE id = ?
                """,
                (now_iso, now_iso, now_iso, node_id),
            )
            conn.execute(
                """
                INSERT INTO metric_samples (
                    node_id, collected_at, source, cpu_percent, memory_percent, disk_percent, temperature_c,
                    uptime_seconds, load_1, load_5, load_15, rx_bytes, tx_bytes, raw_json
                ) VALUES (?, ?, 'pull', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node_id,
                    metrics.timestamp.isoformat(),
                    metrics.cpu_percent,
                    metrics.memory_percent,
                    metrics.disk_percent,
                    metrics.temperature_c,
                    metrics.uptime_seconds,
                    metrics.load_1,
                    metrics.load_5,
                    metrics.load_15,
                    metrics.rx_bytes,
                    metrics.tx_bytes,
                    json.dumps(raw_payload),
                ),
            )
            if services_payload:
                conn.execute("DELETE FROM services WHERE node_id = ?", (node_id,))
                for service in services_payload:
                    name = str(service.get("name", "")).strip()
                    status = str(service.get("status", "unknown")).strip() or "unknown"
                    if not name:
                        continue
                    conn.execute(
                        """
                        INSERT INTO services (node_id, name, status, checked_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (node_id, name, status, now_iso),
                    )

            evaluate_metric_thresholds(
                conn,
                node_id,
                {
                    "cpu_percent": metrics.cpu_percent,
                    "memory_percent": metrics.memory_percent,
                    "disk_percent": metrics.disk_percent,
                    "temperature_c": metrics.temperature_c,
                },
            )
            evaluate_offline_alert(conn, node_id, 0)
        self._node_runtime[node_id] = {"failures": 0, "circuit_until": 0.0}
        self._bump("poll", "successes")

    async def _record_failure(self, node_id: int, category: str, message: str, reachable: bool) -> None:
        runtime = self._node_runtime.setdefault(node_id, {"failures": 0, "circuit_until": 0.0})
        failures = int(runtime.get("failures", 0)) + 1
        runtime["failures"] = failures
        if failures >= self.poll_failure_threshold:
            cooldown = min(self.poll_circuit_cooldown_seconds * failures, self.poll_circuit_cooldown_seconds * 5)
            runtime["circuit_until"] = datetime.now(timezone.utc).timestamp() + float(cooldown)

        with get_conn(self.db_path) as conn:
            node = conn.execute(
                "SELECT last_seen_at FROM nodes WHERE id = ?",
                (node_id,),
            ).fetchone()
            last_seen_at = node["last_seen_at"] if node else None

            now = datetime.now(timezone.utc)
            if last_seen_at:
                try:
                    last_seen = datetime.fromisoformat(last_seen_at)
                    offline_seconds = max(0.0, (now - last_seen).total_seconds())
                except ValueError:
                    offline_seconds = 9999.0
            else:
                offline_seconds = 9999.0

            conn.execute(
                """
                UPDATE nodes
                SET last_status = ?, last_heartbeat_at = CASE WHEN ? THEN ? ELSE last_heartbeat_at END,
                    last_error_category = ?, last_error_message = ?, last_poll_error_at = ?,
                    consecutive_failures = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    "online" if reachable else "offline",
                    1 if reachable else 0,
                    utc_now_iso(),
                    category,
                    message[:300],
                    utc_now_iso(),
                    failures,
                    utc_now_iso(),
                    node_id,
                ),
            )
            evaluate_offline_alert(conn, node_id, 0 if reachable else offline_seconds)
        self._bump("poll", "failures")
        self._record_error(component="poll", category=category, message=f"node_id={node_id} {message}")

    async def _run_retention_cleanup_if_due(self) -> None:
        now_epoch = datetime.now(timezone.utc).timestamp()
        if now_epoch - self._last_cleanup_epoch < self.cleanup_interval_seconds:
            return
        self._last_cleanup_epoch = now_epoch

        metric_cutoff = (datetime.now(timezone.utc) - timedelta(hours=self.metric_retention_hours)).isoformat()
        alert_cutoff = (datetime.now(timezone.utc) - timedelta(days=self.alert_event_retention_days)).isoformat()
        service_cutoff = (datetime.now(timezone.utc) - timedelta(days=self.service_retention_days)).isoformat()

        with get_conn(self.db_path) as conn:
            conn.execute("DELETE FROM metric_samples WHERE collected_at < ?", (metric_cutoff,))
            conn.execute("DELETE FROM alert_events WHERE created_at < ? AND resolved_at IS NOT NULL", (alert_cutoff,))
            conn.execute("DELETE FROM services WHERE checked_at < ?", (service_cutoff,))
        self._bump("cleanup", "runs")

    async def _dispatch_webhooks_if_due(self) -> None:
        if not self.webhook_url:
            return
        now_epoch = datetime.now(timezone.utc).timestamp()
        if now_epoch - self._last_webhook_dispatch_epoch < self.webhook_dispatch_interval_seconds:
            return
        self._last_webhook_dispatch_epoch = now_epoch

        with get_conn(self.db_path) as conn:
            rows = fetch_all_dict(
                conn,
                """
                SELECT
                    wd.id, wd.alert_event_id, wd.attempt_count,
                    ae.node_id, ae.severity, ae.message, ae.metric_value, ae.created_at,
                    a.key AS alert_key, n.name AS node_name, n.hostname AS node_hostname
                FROM webhook_deliveries wd
                JOIN alert_events ae ON ae.id = wd.alert_event_id
                JOIN alerts a ON a.id = ae.alert_id
                JOIN nodes n ON n.id = ae.node_id
                WHERE wd.status IN ('pending', 'failed')
                  AND wd.attempt_count < ?
                  AND wd.next_attempt_at <= ?
                ORDER BY wd.created_at ASC
                LIMIT 20
                """,
                (self.webhook_max_attempts, utc_now_iso()),
            )

        for row in rows:
            await self._attempt_webhook_delivery(row)

    async def _attempt_webhook_delivery(self, row: dict[str, Any]) -> None:
        self._bump("webhook", "attempts")
        payload = {
            "alert_event_id": int(row["alert_event_id"]),
            "node_id": int(row["node_id"]),
            "node_name": row["node_name"],
            "node_hostname": row["node_hostname"],
            "alert_key": row["alert_key"],
            "severity": row["severity"],
            "message": row["message"],
            "metric_value": row["metric_value"],
            "created_at": row["created_at"],
        }
        delivery_id = int(row["id"])
        attempts = int(row["attempt_count"])
        now_iso = utc_now_iso()

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.webhook_timeout_seconds)) as client:
                response = await client.post(self.webhook_url, json=payload)
                response.raise_for_status()
            with get_conn(self.db_path) as conn:
                conn.execute(
                    """
                    UPDATE webhook_deliveries
                    SET status='delivered', delivered_at=?, updated_at=?, last_error=NULL
                    WHERE id=?
                    """,
                    (now_iso, now_iso, delivery_id),
                )
        except Exception as exc:
            logger.warning("webhook delivery failed delivery_id=%s attempt=%s error=%s", delivery_id, attempts + 1, exc)
            next_attempts = attempts + 1
            backoff_seconds = min(self.webhook_retry_base_seconds * (2 ** max(0, attempts)), 300)
            next_attempt_at = (datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds)).isoformat()
            next_status = "failed" if next_attempts < self.webhook_max_attempts else "dead"
            with get_conn(self.db_path) as conn:
                conn.execute(
                    """
                    UPDATE webhook_deliveries
                    SET status=?, attempt_count=?, next_attempt_at=?, updated_at=?, last_error=?
                    WHERE id=?
                    """,
                    (next_status, next_attempts, next_attempt_at, utc_now_iso(), str(exc)[:300], delivery_id),
                )

