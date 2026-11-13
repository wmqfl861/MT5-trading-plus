# T196 S1 安全清单只读核查（2026-11-13，U2/D83）

- 规格：`docs/handoff/2026-11-13-round18-plan.md` §4 T196 行——本机（Windows 云 VM）只读核查；会动网络的改动等 A5 之后。
- 方法：PowerShell / git 只读查询。唯一非只读动作是第 4 节的分支保护设置（D83 已明确授权）。
- 脱敏口径：无 IP（`<内网>`/`<公网>` 占位）、无令牌、无账号；端口可写。本文件过 `tests/test_repo_hygiene.py`。

## 0. 需处理清单（按风险排序，全部等 A5 之后动）

| # | 事项 | 现状 | 建议（A5 后） |
| --- | --- | --- | --- |
| 1 | RDP 未启用 NLA | `UserAuthentication=0`（注册表 RDP-Tcp），3389 全接口监听，网卡直绑 `<公网>`，防火墙任意源放行 | 置 1 启用 NLA；评估 3389 限源（见 §3） |
| 2 | MCP 服务令牌按口径需再轮换 | 当前 `mcp.json` 令牌曾在"全零通配绑定 + 明文 HTTP + 公网直连"时期使用过 | 按 `docs/ops/mcp-remote-access.md` §"明文传输过即视为已泄露"轮换（相对更早的入库版已轮换过一次，见 §1.3） |
| 3 | 工作区 `mcp.json` host 为全零通配绑定（非 127.0.0.1） | 文件仍在（服务当前未运行，端口 47621 无监听）；现行代码 `src/mt5mcp/config.py` 对非回环 host 已 fail-closed，该文件属历史遗留 | host 改回 127.0.0.1（或直接改走隧道方案）后重启 |
| 4 | 防火墙 `mt5mcp` 规则（TCP 47621，任意源、任意 profile） | 服务已停但放行规则仍在，任何程序占住 47621 即被放行 | 删除该规则，或将 RemoteAddress 收紧为回环/指定源（与 #2/#3 一并处理） |
| 5 | 3389 自定义放行规则重复 5 条（规则名就叫"3389"） | 与系统 RDP 规则叠加，任意源任意 profile | 精简为一条并限源（若 #1 采纳限源方案） |
| 6 | 与本仓库无关的对外端口规则（`sub2api-*` 9 条：80/443/8080/18080/18443/28080/39001/52000/61234；`videos-dsh-imgbed-*` 8787/80；`claude-gateway` 48080；`ak-cli` UDP 全端口） | 均任意源任意 profile 放行，属机主既有业务，非本仓库范围 | 仅记录；由机主确认是否保留/收紧 |

已完成项（无需再动）：分支保护已设（§4）；历史入库令牌相对当前已轮换（§1.3）；git 凭据无明文文件（§1.3）。

## 1. `mcp.json` 类配置：监听地址与含令牌清单

### 1.1 配置文件清单与判定

| 文件 | 监听地址判定 | 令牌判定 |
| --- | --- | --- |
| `<主会话 workspace>/mcp.json`（未入库，10-23 起） | **不合规**：host=全零通配绑定（全接口）、port=47621；服务当前未运行（47621 端口无监听，历史日志 `mcp_server.err` 证实曾以全接口启动过） | 含 Bearer 令牌（43 字符）；**需轮换**（曾明文跨公网使用） |
| 仓库 `src/mt5mcp/config.py` | 合规：默认 127.0.0.1；`LOOPBACK_HOSTS` 白名单，非回环 host 一律 fail-closed，除非显式 `allow_plaintext_public: true`（该遗留 mcp.json 无此键） | 令牌最短长度校验（32 字符） |
| `~/.zcode/cli/config.json` | 无 MCP server 定义（仅 hooks/plugins 开关） | 不含令牌 |
| `~/.claude.json` | `mcpServers` 为空 | 含 OAuth 账号字段（本地文件，未入库） |
| `~/.codex/auth.json` | 无监听 | **含 OPENAI_API_KEY**（本地凭据文件，未入库） |
| `~/.zcode/backup/repair-sess_*/provider_config.json` | 无监听 | 不含令牌（仅默认模型选择） |
| 仓库根 `config.example.json` / `config/*.toml` / `demo-configs/*.toml` | 无监听 | 模板/无真实凭据（grep 核对） |
| 仓库 git 追踪文件 | — | 当前 `git ls-files` 无 `mcp.json`/`config.json`（干净） |

### 1.2 历史入库情况（关键事实）

- 提交 `9eccfc8`（mt5mcp 数据服务引入时）曾把 `mcp.json` 提交入库：当时 host=127.0.0.1、port=8310、**含令牌（43 字符）**。
- 该提交已随分支推送到远端，且**仓库为 public**——历史令牌确定外露过。
- 提交 `bd876f3` 将其移除改为 untracked，同时加防火墙规则、端口改 47621（即 §0 #4 那条规则的来历）。

### 1.3 令牌轮换状态判定

- 历史入库令牌 vs 当前工作区令牌：sha256 对比**不同** → 相对入库版已轮换过，历史令牌可视为已作废。
- 但当前令牌在"全零通配绑定 + 明文 HTTP"时期使用过（`mcp-remote-prompt.txt` 指示远程方以 Bearer 经 `<公网>:47621` 直连）→ 按仓库自己定的口径（`docs/ops/mcp-remote-access.md`："令牌一经明文传输过就视为已泄露，必须轮换"），**当前令牌仍需轮换**（列入 §0 #2）。
- git 凭据：`credential.helper=manager`（Windows 凭据管理器），无 `~/.git-credentials` 明文文件。

## 2. 防火墙入站规则清单（Get-NetFirewallRule -Direction Inbound -Enabled True）

总计 112 条，全部为 Allow（0 条 Block）。按敏感度归类：

### 2.1 系统默认（约 70 条，无需处理）

- Core Networking 组：DHCP(68/546)、ICMPv6 全套、Teredo、IPHTTPS、IGMP、多播侦听（多为 Any profile，协议层必需）。
- Network Discovery / mDNS(5353) / LLMNR(5355) / SSDP(1900) / UPnP(2869) / WSD(3702/5357/5358)：限 Private+LocalSubnet（个别 Public 变体限 PlayToDevice/LocalSubnet）。
- Cast to Device / Wi-Fi Direct / Wireless Display / Media Foundation(554/5004-5020) / DIAL(10247) / Proximity：限 LocalSubnet 或 Domain/Private。
- Delivery Optimization(7680/TCP+UDP)、商店应用容器组（App Installer、Microsoft Photos、Desktop App Web Viewer、Windows Feature Experience Pack 等，限 Domain/Private）、Edge/Chrome mDNS(5353)。

### 2.2 应用类（与本机软件相关）

| 规则 | 端口 | 范围 | 备注 |
| --- | --- | --- | --- |
| Remote Desktop - User Mode (TCP/UDP-In) | 3389 | Any profile、任意源 | 系统 RDP 组，与 §3 一并评估 |
| Remote Desktop - Shadow (TCP-In) | Any | Any | RDP 阴影会话 |
| "3389"（自定义，5 条重复） | 3389 | Any profile、任意源 | 见 §0 #5 |
| MetaTrader 5 Strategy Tester Agent | Any TCP | Domain/Private | MT5 自带，非公网 profile |
| Microsoft Edge / Google Chrome mDNS | 5353/UDP | Any | mDNS 发现 |

### 2.3 自定义对外服务（重点，见 §0 #4/#6）

| 规则 | 端口 | 范围 | 归类 |
| --- | --- | --- | --- |
| `mt5mcp` | 47621/TCP | Any profile、任意源 | 本仓库 MCP 服务（已停，规则遗留）——需处理 |
| `sub2api-http-80` / `-8080` / `-18080` | 80/8080/18080 TCP | Any、任意源 | 既有业务 |
| `sub2api-https-443` / `-18443` | 443/18443 TCP | Any、任意源 | 既有业务 |
| `sub2api-http-28080` / `-39001` / `-52000` / `-61234` | 28080/39001/52000/61234 TCP | Any、任意源 | 既有业务 |
| `videos-dsh-imgbed-8787` / `-80` | 8787/80 TCP | Any、任意源 | 既有业务 |
| `claude-gateway` | 48080/TCP | Any、任意源 | 既有业务 |
| `ak-cli direct transfer (UDP)` | 全端口 UDP | Any | 网盘 CLI |
| `icmp4` / `icmp6`（约 12 组重复） | Any | Any | ping 放行（云平台惯例，可精简） |

无名称可疑到指向恶意软件的规则；"可疑"项主要是上表任意源放行的自建服务面。

## 3. RDP 核查

| 检查项 | 结果 | 判定 |
| --- | --- | --- |
| RDP 启用 | `fDenyTSConnections=0`（启用），端口 3389 | — |
| NLA | `UserAuthentication=0` | **未启用——需处理（§0 #1）** |
| 传输层 | `SecurityLayer=2`（TLS） | 有加密，但无 NLA 时连接先达登录界面，暴露用户名枚举/爆破面 |
| 监听绑定 | 3389 在 IPv4+IPv6 全接口由 svchost 监听 | 全接口 |
| 网卡绑定 | 两个活动接口：`external`=<内网>（DHCP）、`本地连接`=<公网>（**手动绑定**）；profile 均 Public | 网卡直绑 `<公网>`，无 NAT 层——**3389 直接暴露公网** |
| 端口映射迹象 | 无需端口映射（公网 IP 直绑网卡）；云安全组为平台层，本机只读无法核查（局限，见 §5） | 未验证 |

## 4. 仓库管理员权限与分支保护（唯一非只读动作，D83 授权）

- 凭据：`git credential fill`（helper=manager）取得 GitHub PAT（长度 40，未落盘、不写入本报告）。
- `GET /repos/wmqfl861/MT5-trading-plus`：`permissions = {admin: true, maintain: true, push: true, triage: true, pull: true}`——**有管理员权限**。仓库为 public，默认分支 main。
- 设置前状态：`main`、`ops/phase-f-log` 均无保护规则（GET 404）。
- `PUT branches/{branch}/protection`（D83 口径：禁止强推、禁止删除，不要求 PR 与检查）：

| 分支 | HTTP | allow_force_pushes | allow_deletions | PR/checks 要求 | 回读验证 |
| --- | --- | --- | --- | --- | --- |
| `main` | 200 | false | false | 无（required_pull_request_reviews/status_checks 均未设置） | GET 确认一致 |
| `ops/phase-f-log` | 200 | false | false | 无 | GET 确认一致 |

- 附注：`enforce_admins=false`（D83 未要求对管理员强制，管理员 token 仍可绕过保护——记录在案，如需收紧可在 A5 后补开）。

## 5. 核查局限

1. 云平台安全组/边界防火墙不在本机视野内，只能核查 OS 层（Windows 防火墙、监听、网卡绑定）；"3389/47621 是否真被云侧放行"未验证，也不做外部扫描（按规格）。
2. `0 条 Block` 入站规则意味着 OS 层没有针对性拦截，暴露面判断以"监听 + Allow 规则"为准。
3. 47621 服务当前未运行：所有"曾经全接口明文监听"的证据来自历史日志/提示文件与 mcp.json 现值，非实时。
4. 令牌轮换判定基于本地两份令牌指纹对比：能证明"入库版已作废"，不能穷尽"当前令牌的全部明文传输史"（故按仓库口径从严判"需再轮换"）。

—— T196 子代理，工作树 `t196-s1-wt`，2026-11-13。

## 16. T204 S1 可逆处置执行记录（2026-11-13 ~08:0x UTC，D89，部署前完成）

| # | 项 | 改前 | 改后 | 备注 |
| --- | --- | --- | --- | --- |
| 1 | RDP NLA（RDP-Tcp UserAuthentication） | 0 | **1** | 既有会话不受影响；D89 已代决；停止条件 armed（若用户无法再登录→回滚为 0） |
| 2 | 工作区 mcp.json 令牌 | 旧 43 字符（历史泄露面） | **已轮换为 48 字节随机**（token_urlsafe(48)） | 服务未运行无需重启；原文件已备份至本机非同步目录 |
| 3 | 工作区 mcp.json host | 全零通配 | **127.0.0.1** | 仅回环监听 |
| 4 | 遗留防火墙规则 mt5mcp（47621/TCP 入站 Allow） | Enabled/Allow | **已删除**（端口核验 0 条规则） | 服务早已停，纯遗留 |

3389 限源与防火墙其余精简仍按 D89 留 A5 后。给用户：若用远程桌面连接本机，客户端需支持 NLA（Windows 自带与微软官方 Mac 客户端均支持）；云控制台可选把 3389 只放行自己的 IP。
