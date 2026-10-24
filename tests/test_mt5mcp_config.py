"""T1（S1 安全加固）：mcp.json 的 host 只允许回环地址。

MCP 服务是明文 HTTP + Bearer 令牌：令牌明文过公网/局域网会被链路窃听重放，
所以 load_mcp_config 对非回环 host fail-closed——除非显式写
"allow_plaintext_public": true（逃生门写在明处，自担风险）。
远程访问的正确姿势见 docs/ops/mcp-remote-access.md（SSH 隧道 / Tailscale / WireGuard）。
"""
import json

import pytest

from mt5mcp.config import create_default, load_mcp_config

GOOD_TOKEN = "t" * 40


def _write(tmp_path, **overrides):
    payload = {"host": "127.0.0.1", "port": 8310, "token": GOOD_TOKEN}
    payload.update(overrides)
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_loopback_hosts_accepted(tmp_path):
    # 回环三种写法都放行（localhost 大小写不敏感）
    for host in ("127.0.0.1", "::1", "localhost", "LOCALHOST"):
        p = _write(tmp_path, host=host)
        assert load_mcp_config(p).host == host


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "203.0.113.7", "192.168.1.50", "10.8.0.1", "example.com"],
)
def test_non_loopback_host_rejected_without_override(tmp_path, host):
    # 公网/局域网/通配地址：没有显式豁免就拒绝（fail-closed）
    p = _write(tmp_path, host=host)
    with pytest.raises(ValueError, match="docs/ops/mcp-remote-access.md"):
        load_mcp_config(p)


def test_reject_message_points_to_tunnel_doc_and_escape_hatch(tmp_path):
    p = _write(tmp_path, host="0.0.0.0")
    with pytest.raises(ValueError) as ei:
        load_mcp_config(p)
    msg = str(ei.value)
    assert "docs/ops/mcp-remote-access.md" in msg  # 指向隧道文档
    assert "allow_plaintext_public" in msg  # 告诉用户显式豁免怎么写


def test_non_loopback_allowed_with_explicit_plaintext_flag(tmp_path):
    p = _write(tmp_path, host="0.0.0.0", allow_plaintext_public=True)
    assert load_mcp_config(p).host == "0.0.0.0"


def test_plaintext_flag_must_be_bool(tmp_path):
    p = _write(tmp_path, allow_plaintext_public="yes")
    with pytest.raises(ValueError, match="allow_plaintext_public"):
        load_mcp_config(p)


def test_weak_token_checked_before_host(tmp_path):
    # 令牌校验先于 host 校验：弱令牌报 token 错，而不是 host 错
    p = _write(tmp_path, host="0.0.0.0", token="short")
    with pytest.raises(ValueError, match="token"):
        load_mcp_config(p)


def test_create_default_writes_loopback_only(tmp_path):
    # --init 生成的 mcp.json 只写回环地址，永不生成对外监听
    p = tmp_path / "mcp.json"
    cfg = create_default(p)
    assert cfg.host == "127.0.0.1"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["host"] == "127.0.0.1"
