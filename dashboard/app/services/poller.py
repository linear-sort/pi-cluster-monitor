from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any

import httpx

from app.db import fetch_all_dict, get_conn, utc_now_iso
from app.models import AgentMetrics
from app.services.alerts import evaluate_metric_thresholds, evaluate_offline_alert


class PollingService:
    def __init__(
        self,
        db_path: Path,
        base_tick_seconds: int = 2,
        timeout_seconds: int = 4,
        metric_retention_hours: int = 48,
        alert_event_retention_days: int = 14,
        service_retention_days: int = 7,
        cleanup_interval_seconds: int = 300,
    ) -> None:
        self.db_path = db_path
        self.base_tick_seconds = max(1, base_tick_seconds)
        self.timeout_seconds = timeout_seconds
        self.metric_retention_hours = max(1, metric_retention_hours)
        self.alert_event_retention_days = max(1, alert_event_retention_days)
        self.service_retention_days = max(1, service_retention_days)
        self.cleanup_interval_seconds = max(30, cleanup_interval_seconds)
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._last_poll: dict[int, float] = {}
        self._last_cleanup_epoch: float = 0.0

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
                await asyncio.sleep(self.base_tick_seconds)

    async def _poll_due_nodes(self, client: httpx.AsyncClient) -> None:
        with get_conn(self.db_path) as conn:
            nodes = fetch_all_dict(
                conn,
                """
                SELECT id, ip_address, token, poll_interval_seconds, enabled, agent_port
                FROM nodes
                WHERE enabled = 1
                """,
            )

        now_epoch = datetime.now(timezone.utc).timestamp()
        tasks = []

        for node in nodes:
            node_id = int(node["id"])
            poll_interval = max(3, int(node["poll_interval_seconds"]))
            last_polled = self._last_poll.get(node_id, 0.0)
            if now_epoch - last_polled < poll_interval:
                continue
            self._last_poll[node_id] = now_epoch
            tasks.append(self._poll_node(client, node))

        if tasks:
            await asyncio.gather(*tasks)

    async def _poll_node(self, client: httpx.AsyncClient, node: dict[str, Any]) -> None:
        node_id = int(node["id"])
        ip = node["ip_address"]
        token = node["token"]
        port = int(node.get("agent_port") or 8001)
        base_url = f"http://{ip}:{port}"
        headers = {"Authorization": f"Bearer {token}"}

        try:
            response = await client.get(f"{base_url}/api/v1/metrics", headers=headers)
            response.raise_for_status()
            metrics_payload = response.json()
            metrics = AgentMetrics.model_validate(metrics_payload)
            services_payload = await self._fetch_services(client, base_url, headers)
            await self._record_success(node_id, metrics, metrics_payload, services_payload)
        except Exception:
            await self._record_failure(node_id)

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
                SET last_seen_at = ?, last_status = 'online', updated_at = ?
                WHERE id = ?
                """,
                (now_iso, now_iso, node_id),
            )
            conn.execute(
                """
                INSERT INTO metric_samples (
                    node_id, collected_at, cpu_percent, memory_percent, disk_percent, temperature_c,
                    uptime_seconds, load_1, load_5, load_15, rx_bytes, tx_bytes, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    async def _record_failure(self, node_id: int) -> None:
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
                SET last_status = 'offline', updated_at = ?
                WHERE id = ?
                """,
                (utc_now_iso(), node_id),
            )
            evaluate_offline_alert(conn, node_id, offline_seconds)

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
