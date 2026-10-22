"""Bearer 令牌鉴权：HTTP 模式下 MCP 端点唯一的门锁。"""
from __future__ import annotations

import hmac


def token_matches(provided: str | None, expected: str) -> bool:
    if not provided:
        return False
    return hmac.compare_digest(provided.encode(), expected.encode())


def _extract_bearer(header_value: str | None) -> str | None:
    if not header_value:
        return None
    parts = header_value.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def bearer_auth_middleware(app, token: str):
    """包一层 ASGI 应用：无/错令牌一律 401，不透传给 MCP。"""

    async def guarded(scope, receive, send):
        if scope["type"] != "http":
            if scope["type"] == "lifespan":
                return await app(scope, receive, send)
            return  # 其余类型（websocket 等）直接拒绝，不做代理
        # HTTP 头按 latin-1 解码（RFC），遇到任意字节也不会 500
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        provided = _extract_bearer(headers.get("authorization"))
        if not token_matches(provided, token):
            body = b'{"error": "unauthorized: missing or invalid bearer token"}'
            await send({
                "type": "http.response.start", "status": 401,
                "headers": [(b"content-type", b"application/json"),
                            (b"www-authenticate", b"Bearer")],
            })
            await send({"type": "http.response.body", "body": body})
            return
        await app(scope, receive, send)

    return guarded
