"""MCP 数据服务 CLI。

    .venv/Scripts/python.exe scripts/run_mcp_server.py --init      # 生成 mcp.json（随机令牌，只写 127.0.0.1）
    .venv/Scripts/python.exe scripts/run_mcp_server.py             # HTTP 模式（远程 agent 经隧道用）
    .venv/Scripts/python.exe scripts/run_mcp_server.py --stdio     # 本机 agent 用

安全口径（S1）：明文 HTTP 只监听回环地址；远程访问一律经隧道
（SSH 本地端口转发 / Tailscale / WireGuard），见 docs/ops/mcp-remote-access.md。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5mcp.config import create_default, load_mcp_config  # noqa: E402
from mt5mcp.server import run_http, run_stdio  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="MT5 K线库 MCP 数据服务")
    ap.add_argument("--config", default="mcp.json")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--init", action="store_true", help="生成默认配置（含随机令牌）")
    ap.add_argument("--stdio", action="store_true", help="stdio 模式（本机 agent）")
    args = ap.parse_args()

    if args.init:
        cfg = create_default(args.config)
        print(f"已生成 {args.config}：host={cfg.host} port={cfg.port}")
        print(f"访问令牌（保管好）：{cfg.token}")
        print("服务只监听回环地址；远程 agent 请走隧道访问，见 docs/ops/mcp-remote-access.md")
        return 0

    if args.stdio:
        run_stdio(args.data_dir)
        return 0

    cfg = load_mcp_config(args.config)
    print(f"MCP 数据服务启动：http://{cfg.host}:{cfg.port}/mcp "
          f"(数据目录 {args.data_dir}，Bearer 令牌鉴权)")
    run_http(cfg, args.data_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
