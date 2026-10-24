# MCP 远程访问（只经隧道）

MCP 数据服务是**明文 HTTP + Bearer 令牌**。令牌明文跨公网/局域网传输会被链路窃听后
重放，拿到令牌就能读全部行情库。因此（见 `docs/blueprint.md` §4 第 9 条）：

- `mcp.json` 的 `host` 只允许回环地址（`127.0.0.1` / `::1` / `localhost`）；
  非回环地址 `load_mcp_config` 直接报错（fail-closed），报错文案指向本文档。
- 确要明文对外监听（不推荐），必须在 `mcp.json` 显式写 `"allow_plaintext_public": true`
  ——逃生门写在明处，风险自担；此时务必把防火墙限制为远程 agent 的固定 IP。
- 远程 agent 一律经隧道连接，服务端保持只听 `127.0.0.1`。

## 接法一：SSH 本地端口转发（零新增组件）

前提：远程 agent 所在服务器能用 SSH 连到交易主机（或经跳板机/内网穿透后的 SSH 入口）。

1. 交易主机：`mcp.json` 保持默认 `"host": "127.0.0.1", "port": 8310`，正常启动服务。
2. 远程 agent 服务器上建立转发（本机 8310 → 交易主机 8310）：

   ```bash
   ssh -N -L 8310:127.0.0.1:8310 <user>@<交易主机地址>
   ```

3. 远程 agent 的 MCP 客户端连接 `http://127.0.0.1:8310/mcp`（隧道的本地端，
   带上 Bearer 令牌）。链路由 SSH 加密，令牌不再明文。

交易主机若未开 OpenSSH Server（管理员 PowerShell）：

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd; Set-Service -Name sshd -StartupType Automatic
```

若交易主机没有公网 SSH 入口（推荐保持如此）：反过来从交易主机建反向隧道
`ssh -N -R 8310:127.0.0.1:8310 <user>@<跳板机>`，远程 agent 连跳板机的
`http://127.0.0.1:8310/mcp`；或改用接法二。

## 接法二：Tailscale / WireGuard

### Tailscale（推荐：保持回环监听 + 自动 TLS）

1. 交易主机与远程 agent 服务器都安装 Tailscale 并登录同一 tailnet。
2. 交易主机上把回环端口发布进 tailnet（自动配 TLS 证书）：

   ```bash
   tailscale serve --bg http://127.0.0.1:8310
   ```

3. 远程 agent 连接 `https://<交易主机机器名>.<tailnet>.ts.net/mcp`。
   MCP 仍只听 `127.0.0.1`，不需要改 `mcp.json`。

也可两者叠加：tailnet 内跑接法一的 SSH 转发，远程 agent 仍连
`http://127.0.0.1:<端口>/mcp`。

### WireGuard（点对点）

1. 交易主机与远程 agent 服务器互配 WireGuard peer。
2. 服务需要监听本机的 WG 接口地址（如 `10.8.0.1`）——这是非回环地址，需在
   `mcp.json` 里把 `host` 写成该地址并显式加 `"allow_plaintext_public": true`
   （流量在 WG 隧道内加密，明文只存在于隧道里；确认该地址只有 peer 可达）。
3. 远程 agent 连接 `http://<交易主机WG地址>:8310/mcp`。

## 令牌轮换

令牌一经明文传输过（例如历史上 `0.0.0.0` 对外监听时期）就视为已泄露，必须轮换：

1. 交易主机：删掉旧 `mcp.json` 后重新生成（新随机令牌）：

   ```bash
   .venv/Scripts/python.exe scripts/run_mcp_server.py --init
   ```

   或只换令牌：`python -c "import secrets; print(secrets.token_urlsafe(32))"`
   生成后写进 `mcp.json` 的 `token`（≥32 字符）。
2. 重启 MCP 服务（计划任务或命令行），确认新令牌可连、旧令牌 401。
3. 远程 agent 侧同步更新令牌（MCP 客户端配置 / 环境变量），旧令牌随即作废。

## 本机加固清单（S1，由用户手动执行，代理不得代办）

1. `mcp.json` 的 `host` 改回 `127.0.0.1`，并按上面步骤轮换令牌。
2. 删除 Windows 防火墙里 MCP 端口（即本机 mcp.json 里的 port 值）的入站放行规则
   （管理员 PowerShell：`Get-NetFirewallRule | Where-Object ...` 找到后
   `Remove-NetFirewallRule`，或防火墙 GUI 删除）。
3. 确认 RDP（3389）未暴露公网；必须远程则限制来源 IP、开启 NLA、强密码。
4. 仓库改 Private；或由用户决定是否用 `git filter-repo` 清理历史中的公网 IP
   （改写历史 + 强推影响所有克隆，只能用户自己决定）。
