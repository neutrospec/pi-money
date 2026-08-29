"""The one gate every write from the browser has to pass.

This project's web layer was GET-only by design, and that was not squeamishness
— a browser will happily POST to ``127.0.0.1:8077`` on behalf of any page the
owner happens to be visiting, and this server holds their account data. The
portfolio design closed web writes until two conditions were met: that CLI
entry proved to be real friction, and that loopback restriction and CSRF were
designed together rather than bolted on. Both are now true, so the gate is
built first and the routes are attached to it.

Four checks, each closing a different door:

``loopback``
    The client address must be the local machine. The server binds 127.0.0.1
    today, but a binding is a launch argument and this is a property of the
    request — the day someone runs it behind a proxy or on 0.0.0.0, the check
    still holds where the assumption would not.

``host``
    The ``Host`` header must name localhost. This is the DNS-rebinding defence:
    an attacker's domain can be made to resolve to 127.0.0.1, at which point
    the client address check passes and only the header reveals that the
    request thinks it is talking to ``evil.example``.

``json``
    The content type must be exactly JSON. HTML forms can only send
    urlencoded, multipart or text/plain, and those are the request kinds that
    cross origins without a preflight. Requiring JSON removes the entire class.

``token``
    A secret minted once and kept in the database, rendered into the page and
    required as a request header. A cross-origin request carrying a custom
    header must preflight, and nothing here answers preflights. Persisted
    rather than held in memory so that a ``--reload`` restart does not silently
    invalidate a form the owner already has open.

No cookie is involved, which sidesteps SameSite behaviour entirely. Agents are
unaffected: MCP and pi have no write tools and are not getting any.
"""
from __future__ import annotations

import secrets

from fastapi import HTTPException, Request

from app import db


TOKEN_KEY = "portfolio_write_token"
TOKEN_HEADER = "X-Portfolio-Token"

# Host names that mean "this machine". Compared without the port, because a
# check that breaks when the port changes is worse than no check — it looks
# present while refusing everything, or gets deleted in frustration.
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
LOCAL_CLIENTS = {"127.0.0.1", "::1", "localhost"}

CONTENT_TYPE = "application/json"


def token() -> str:
    """The write token, minted once and kept across restarts."""
    existing = db.get_meta(TOKEN_KEY)
    if existing:
        return existing
    minted = secrets.token_urlsafe(32)
    db.set_meta(TOKEN_KEY, minted)
    return minted


def _host_of(header: str | None) -> str:
    if not header:
        return ""
    value = header.strip()
    if value.startswith("["):          # bracketed IPv6, port after the bracket
        return value.split("]")[0] + "]"
    return value.rsplit(":", 1)[0] if ":" in value else value


def require_local_write(request: Request) -> None:
    """FastAPI dependency. Every portfolio write route depends on this."""
    client = request.client.host if request.client else None
    if client not in LOCAL_CLIENTS:
        raise HTTPException(
            status_code=403,
            detail="자산 쓰기는 이 컴퓨터에서만 가능합니다.",
        )
    if _host_of(request.headers.get("host")) not in LOCAL_HOSTS:
        raise HTTPException(
            status_code=403,
            detail="요청의 Host 가 로컬이 아닙니다.",
        )
    media = (request.headers.get("content-type") or "").split(";")[0].strip()
    if media != CONTENT_TYPE:
        raise HTTPException(
            status_code=415,
            detail=f"본문은 {CONTENT_TYPE} 이어야 합니다.",
        )
    supplied = request.headers.get(TOKEN_HEADER, "")
    if not supplied or not secrets.compare_digest(supplied, token()):
        raise HTTPException(
            status_code=403,
            detail="쓰기 토큰이 없거나 일치하지 않습니다. 화면을 새로고침하세요.",
        )
