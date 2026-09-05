"""Auth and the request-scoped user.

Two modes. ``open`` trusts the caller and calls them ``dev_user`` — the laptop
posture the desktop tools have today. ``token`` requires a bearer token that
matches the users table, which is the only difference between running this on
one machine and running it for the lab.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request, WebSocket

from ..db import now

ROLES = ("annotator", "reviewer", "admin")


def _lookup(host, token: str | None) -> dict | None:
    if not token:
        return None
    r = host.db.one("SELECT * FROM users WHERE token=?", token)
    return dict(r) if r else None


def ensure_user(host, name: str, role: str = "admin") -> dict:
    r = host.db.one("SELECT * FROM users WHERE name=?", name)
    if r:
        return dict(r)
    host.db.insert("users", name=name, display=name, role=role, token=None, created=now())
    return dict(host.db.one("SELECT * FROM users WHERE name=?", name))


def user_from(host, token: str | None) -> dict:
    cfg = host.cfg
    if cfg.auth_mode == "open":
        return ensure_user(host, cfg.dev_user, "admin")
    u = _lookup(host, token)
    if u is None:
        raise HTTPException(401, "a bearer token is required — ask an admin for one")
    return u


async def current_user(request: Request) -> dict:
    host = request.app.state.host
    tok = None
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        tok = auth[7:].strip()
    tok = tok or request.query_params.get("token")
    return user_from(host, tok)


async def ws_user(ws: WebSocket) -> dict:
    host = ws.app.state.host
    return user_from(host, ws.query_params.get("token"))


def require(role: str):
    """Dependency factory: `Depends(require("reviewer"))`."""
    rank = {"annotator": 0, "reviewer": 1, "admin": 2}

    async def dep(user: dict = Depends(current_user)) -> dict:
        if rank.get(user["role"], -1) < rank[role]:
            raise HTTPException(403, f"this needs the {role} role; you are {user['role']}")
        return user
    return dep
