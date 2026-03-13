from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, status


ROLE_RANK = {"viewer": 1, "operator": 2, "admin": 3}


def request_correlation_id(request: Request) -> str:
    incoming = (request.headers.get("X-Request-ID", "") or "").strip()
    if incoming:
        return incoming[:120]
    return f"req-{secrets.token_hex(8)}"


def require_operator(request: Request, min_role: str = "operator") -> dict[str, str]:
    token = (request.headers.get("X-PCM-Operator-Token", "") or "").strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Operator authentication required")

    settings = request.app.state.settings
    creds = settings.operator_credentials or {}
    principal = creds.get(token)
    if not principal:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid operator token")

    principal_role = str(principal.get("role", "viewer")).strip().lower()
    if ROLE_RANK.get(principal_role, 0) < ROLE_RANK.get(min_role, 0):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient operator permissions")
    return {"principal": str(principal.get("principal", "unknown")), "role": principal_role}

