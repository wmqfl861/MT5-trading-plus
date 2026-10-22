"""MCP 服务配置：mcp.json（host/port/token）。"""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

MIN_TOKEN_LEN = 32


@dataclass(frozen=True)
class MCPServerConfig:
    host: str = "127.0.0.1"
    port: int = 8310
    token: str = ""
    # Host 头防 DNS 重绑定校验；真门锁是 Bearer 令牌，默认关闭（SDK 在本机监听且未显式传参会自动开启，本实现总是显式传参）
    dns_rebinding_protection: bool = False
    allowed_hosts: tuple[str, ...] = ()


def load_mcp_config(path: str | Path) -> MCPServerConfig:
    p = Path(path)
    if not p.exists():
        raise ValueError(f"配置不存在：{p}（先运行 run_mcp_server.py --init 生成）")
    data = json.loads(p.read_text(encoding="utf-8"))
    token = data.get("token", "")
    if not token or len(token) < MIN_TOKEN_LEN:
        raise ValueError(
            f"token 缺失或弱于 {MIN_TOKEN_LEN} 字符——对外服务的唯一门锁，不能弱。"
            "用 secrets.token_urlsafe(32) 生成一个。"
        )
    host = data.get("host", "127.0.0.1")
    port = data.get("port", 8310)
    dns_flag = data.get("dns_rebinding_protection", False)
    hosts = data.get("allowed_hosts", [])
    if not isinstance(host, str):
        raise ValueError(f"host 应为字符串，实际 {type(host).__name__}")
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        raise ValueError(f"port 应为 1-65535 的整数，实际 {port!r}")
    if not isinstance(dns_flag, bool):
        raise ValueError(f"dns_rebinding_protection 应为 true/false，实际 {dns_flag!r}")
    if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
        raise ValueError("allowed_hosts 应为字符串数组")
    return MCPServerConfig(
        host=host, port=port, token=token,
        dns_rebinding_protection=dns_flag,
        allowed_hosts=tuple(hosts),
    )


def create_default(path: str | Path) -> MCPServerConfig:
    p = Path(path)
    cfg = MCPServerConfig(
        host="127.0.0.1",  # 默认只听本机；对远程开放时显式改 0.0.0.0 并配防火墙
        port=8310,
        token=secrets.token_urlsafe(32),
    )
    if not p.exists():
        p.write_text(json.dumps(cfg.__dict__, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    return load_mcp_config(p)
