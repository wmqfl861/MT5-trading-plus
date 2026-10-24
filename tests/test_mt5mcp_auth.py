import pytest

from mt5mcp.auth import token_matches
from mt5mcp.config import MCPServerConfig, create_default, load_mcp_config


def test_token_matches_exact():
    assert token_matches("secret", "secret") is True
    assert token_matches("wrong", "secret") is False
    assert token_matches(None, "secret") is False
    assert token_matches("Bearer secret", "secret") is False  # 只比对裸令牌


def test_config_defaults_are_safe(tmp_path):
    p = tmp_path / "mcp.json"
    cfg = create_default(p)
    assert cfg.host == "127.0.0.1"  # 默认只听本机，暴露外网需显式改
    assert cfg.port > 0
    assert len(cfg.token) >= 32  # 随机令牌足够长
    loaded = load_mcp_config(p)
    assert loaded == cfg  # 落盘可回读


def test_config_load_roundtrip(tmp_path):
    p = tmp_path / "mcp.json"
    cfg = MCPServerConfig(host="0.0.0.0", port=9000, token="t" * 40)
    p.write_text(
        '{"host": "0.0.0.0", "port": 9000, "token": "%s", "allow_plaintext_public": true}' % ("t" * 40),
        encoding="utf-8",
    )
    assert load_mcp_config(p) == cfg


def test_config_dns_rebinding_settings_roundtrip(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text('{"host": "0.0.0.0", "port": 9000, "token": "%s", "allow_plaintext_public": true, "dns_rebinding_protection": true, "allowed_hosts": ["<SERVER_IP>:*"]}' % ("t" * 40), encoding="utf-8")
    cfg = load_mcp_config(p)
    assert cfg.dns_rebinding_protection is True
    assert cfg.allowed_hosts == ("<SERVER_IP>:*",)


def test_config_default_disables_dns_rebinding_protection(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text('{"host": "0.0.0.0", "port": 9000, "token": "%s", "allow_plaintext_public": true}' % ("t" * 40), encoding="utf-8")
    cfg = load_mcp_config(p)
    assert cfg.dns_rebinding_protection is False  # 真门锁是令牌；Host 校验默认关


def test_config_rejects_weak_or_missing_token(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text('{"host": "0.0.0.0", "port": 9000, "token": "short"}', encoding="utf-8")
    with pytest.raises(ValueError, match="token"):
        load_mcp_config(p)  # 弱令牌当场拒绝——这是对外服务的唯一门锁
    p.write_text('{"host": "0.0.0.0", "port": 9000}', encoding="utf-8")
    with pytest.raises(ValueError, match="token"):
        load_mcp_config(p)


def test_middleware_survives_non_utf8_headers():
    # HTTP 头按 latin-1 解码：恶意字节头不得引发 500（虽不构成绕过）
    import asyncio
    from mt5mcp.auth import bearer_auth_middleware
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    guarded = bearer_auth_middleware(app, "right-token-0123456789abcdef")

    async def run():
        scope = {
            "type": "http", "method": "GET", "path": "/mcp",
            "headers": [(b"authorization", b"Bearer " + bytes([255, 254, 128]) + b"bad")],
        }
        sent = []

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(msg):
            sent.append(msg)

        await guarded(scope, receive, send)
        return sent

    sent = asyncio.run(run())
    assert sent[0]["status"] == 401  # 拒绝但不崩溃
    assert seen == []  # 未透传给内层应用


def test_middleware_rejects_websocket_scope():
    import asyncio
    from mt5mcp.auth import bearer_auth_middleware

    async def app(scope, receive, send):
        raise AssertionError("不得透传 websocket 作用域")

    guarded = bearer_auth_middleware(app, "t" * 40)

    async def run():
        scope = {"type": "websocket", "path": "/mcp", "headers": []}

        async def receive():
            return {}

        async def send(msg):
            pass

        await guarded(scope, receive, send)  # 静默拒绝（不发任何东西）

    asyncio.run(run())  # 无异常即通过


def test_middleware_blocks_without_or_wrong_token():
    import starlette.responses
    from mt5mcp.auth import bearer_auth_middleware

    async def inner_app(scope, receive, send):
        resp = starlette.responses.PlainTextResponse("ok")
        await resp(scope, receive, send)

    wrapped = bearer_auth_middleware(inner_app, "right-token-0123456789abcdef")

    from starlette.testclient import TestClient

    client = TestClient(wrapped)
    assert client.get("/mcp").status_code == 401
    assert client.get("/mcp", headers={"Authorization": "Bearer wrong"}).status_code == 401
    # 只认标准 Bearer 方案；裸令牌不比缺失更宽容
    assert client.get("/mcp", headers={"Authorization": "right-token-0123456789abcdef"}).status_code == 401
    assert client.get("/mcp", headers={"Authorization": "Bearer right-token-0123456789abcdef"}).status_code == 200
