from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status


def verify_token(request: Request) -> None:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")

    token = auth_header.removeprefix("Bearer " ).strip()
    expected = request.app.state.settings.token
    if token != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


def auth_dependency(_: None = Depends(verify_token)) -> None:
    return None
