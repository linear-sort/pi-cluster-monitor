from __future__ import annotations

import hashlib
import secrets
import time

from fastapi import HTTPException, Request, status


ROLE_RANK = {"viewer": 1, "operator": 2, "admin": 3}
_AUTH_BUCKETS: dict[str, dict[str, float | int | str]] = {}
_AUTH_RECENT_EVENTS: list[dict[str, str]] = []
_AUTH_CATEGORY_KEYS = ("invalid", "expired", "revoked", "throttled", "missing")
_AUTH_COUNTERS: dict[str, int] = {key: 0 for key in _AUTH_CATEGORY_KEYS}


def _now_epoch() -> float:
    return time.time()


def _append_auth_event(category: str, source: str, detail: str) -> None:
    _AUTH_COUNTERS[category] = int(_AUTH_COUNTERS.get(category, 0)) + 1
    _AUTH_RECENT_EVENTS.append(
        {
            "ts_epoch": f"{_now_epoch():.3f}",
            "category": category,
            "source": source,
            "detail": detail[:200],
        }
    )
    if len(_AUTH_RECENT_EVENTS) > 100:
        del _AUTH_RECENT_EVENTS[:-100]


def _source_bucket(request: Request, token: str) -> str:
    source_ip = request.client.host if request.client else "unknown"
    token_fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12] if token else "missing"
    return f"{source_ip}:{token_fingerprint}"


def _bucket_state(bucket: str) -> dict[str, float | int | str]:
    state = _AUTH_BUCKETS.get(bucket)
    if state is None:
        state = {
            "window_started_at": _now_epoch(),
            "failures": 0,
            "locked_until": 0.0,
            "last_category": "",
        }
        _AUTH_BUCKETS[bucket] = state
    return state


def _mark_failure(request: Request, category: str, detail: str) -> tuple[str, bool]:
    token = (request.headers.get("X-PCM-Operator-Token", "") or "").strip()
    bucket = _source_bucket(request, token)
    state = _bucket_state(bucket)
    now = _now_epoch()
    settings = request.app.state.settings
    window = int(settings.operator_auth_window_seconds)
    if now - float(state.get("window_started_at", 0.0)) > float(window):
        state["window_started_at"] = now
        state["failures"] = 0
    failures = int(state.get("failures", 0)) + 1
    state["failures"] = failures
    state["last_category"] = category
    max_failures = int(settings.operator_auth_max_failures)
    throttled = failures >= max_failures
    if throttled:
        state["locked_until"] = now + float(int(settings.operator_auth_lockout_seconds))
        _append_auth_event("throttled", bucket, f"category={category} detail={detail}")
    _append_auth_event(category, bucket, detail)
    return bucket, throttled


def _is_throttled(request: Request, token: str) -> bool:
    bucket = _source_bucket(request, token)
    state = _bucket_state(bucket)
    return float(state.get("locked_until", 0.0)) > _now_epoch()


def _clear_failure_window(request: Request, token: str) -> None:
    bucket = _source_bucket(request, token)
    state = _bucket_state(bucket)
    state["window_started_at"] = _now_epoch()
    state["failures"] = 0
    state["locked_until"] = 0.0
    state["last_category"] = ""


def request_correlation_id(request: Request) -> str:
    incoming = (request.headers.get("X-Request-ID", "") or "").strip()
    if incoming:
        return incoming[:120]
    return f"req-{secrets.token_hex(8)}"


def require_operator(request: Request, min_role: str = "operator") -> dict[str, str]:
    token = (request.headers.get("X-PCM-Operator-Token", "") or "").strip()
    if _is_throttled(request, token):
        bucket = _source_bucket(request, token)
        _append_auth_event("throttled", bucket, "precheck")
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many auth failures; retry later")
    if not token:
        _, throttled = _mark_failure(request, "missing", "missing_operator_token")
        if throttled:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many auth failures; retry later",
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Operator authentication required")

    settings = request.app.state.settings
    creds = settings.operator_credentials or {}
    principal = creds.get(token)
    if not principal:
        _, throttled = _mark_failure(request, "invalid", "unknown_operator_token")
        if throttled:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many auth failures; retry later",
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid operator token")

    principal_status = str(principal.get("status", "active")).strip().lower()
    if principal_status == "revoked":
        _, throttled = _mark_failure(request, "revoked", "revoked_operator_token")
        if throttled:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many auth failures; retry later",
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Operator token revoked")

    expires_at_epoch = principal.get("expires_at_epoch")
    if expires_at_epoch is not None and float(expires_at_epoch) <= _now_epoch():
        _, throttled = _mark_failure(request, "expired", "expired_operator_token")
        if throttled:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many auth failures; retry later",
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Operator token expired")

    principal_role = str(principal.get("role", "viewer")).strip().lower()
    if ROLE_RANK.get(principal_role, 0) < ROLE_RANK.get(min_role, 0):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient operator permissions")
    _clear_failure_window(request, token)
    return {"principal": str(principal.get("principal", "unknown")), "role": principal_role}


def get_operator_auth_diagnostics() -> dict[str, object]:
    return {
        "counters": {key: int(_AUTH_COUNTERS.get(key, 0)) for key in _AUTH_CATEGORY_KEYS},
        "tracked_buckets": len(_AUTH_BUCKETS),
        "recent_events": list(_AUTH_RECENT_EVENTS[-25:]),
    }


def reset_operator_auth_state() -> None:
    _AUTH_BUCKETS.clear()
    _AUTH_RECENT_EVENTS.clear()
    for key in _AUTH_CATEGORY_KEYS:
        _AUTH_COUNTERS[key] = 0

