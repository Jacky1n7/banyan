# 浏览器能力后端（配套本 fork）

metapi 的 `AnyRouterAdapter` 会把 `login` / `verifyToken` / `getBalance` / `checkin`
转发到这里的 HTTP RPC，由真浏览器解掉阿里云 ESA 的 JS 挑战再请求站点接口。

## 部署形态（先读这一节）

**本目录不能独立运行。** 这些文件必须被放进上游
[millylee/anyrouter-check-in](https://github.com/millylee/anyrouter-check-in) 的
clone 目录里，和上游的 `checkin.py` / `utils/` 同级：

```
anyrouter-check-in/          ← 上游 clone（git clone）
├── checkin.py               ┐
├── utils/                   ├─ 上游代码，本 fork 直接 import
├── pyproject.toml           ┘
├── panel.py                 ┐
├── ext_api.py               ├─ 本目录的文件复制进来
├── telegram_bot.py          │
├── paths.py                 │
├── run_panel.sh             ┘
└── .env                     ← 上游格式的账号/代理配置
```

`paths.py` 默认把 `CHECKIN_DIR` 推导为**自己所在目录**，所以上游 clone 到哪儿都行，
不需要改代码。只有把文件放到别处（比如只放 symlink）时才需要设 `CHECKIN_DIR`。

## 架构

```
metapi（唯一入口：面板 / 连接管理 / 调度 / 签到日志 / 消息推送）
└── AnyRouterAdapter ──HTTP RPC──> 浏览器能力后端 127.0.0.1:4100/__checkin/ext/*
                                   （无状态：不写库、不推送、不调度）
```

后端**故意做成无状态**：调度、连接管理、`checkin_logs`、消息推送全部由 metapi 负责，
避免两套系统各推一份通知、各记一份日志。

## 文件职责

| 文件 | 职责 |
|---|---|
| `paths.py` | 部署路径集中定义（`CHECKIN_DIR` / `METAPI_DIR` / `MIHOMO_DIR` / uv 定位） |
| `ext_api.py` | adapter 调用的 RPC 实现：login / verify / userinfo / balance / checkin。无状态 |
| `panel.py` | HTTP 服务，路由 `/__checkin/ext/*`。同时监听回环与 docker 网桥（metapi 是容器，只绑回环连不上）；**不绑 `0.0.0.0`**（该端口若在云安全列表放行，绑通配等于对公网开代理） |
| `run_panel.sh` / `run_tgbot.sh` | systemd 入口。**必须套 xvfb** —— login 走有头 Chromium（反检测优于无头），作为服务没有 X 显示会报 `browser has been closed`。不能写进 `ExecStart`：systemd 不做 shell 解析，`--server-args="-screen 0 ..."` 的空格会被拆成多个参数 |
| `telegram_bot.py` + `tg_fmt.py` | Telegram 管理机器人（可选，见下文）；`/add` 的 session 模式会调 `import_session.py`，所以也走 xvfb |
| `mihomo_setup_proxy.py` | 生成 mihomo 配置。proxy-group **必须是 `select` 固定节点** —— WAF cookie 与 session 绑出口 IP，`url-test`/`fallback` 换节点会让已登录 session 立刻失效 |
| `mihomo_update_sub.py` | 从订阅拉最新节点（节点全挂时的自救路径） |
| `add_account.py` / `import_session.py` | 账号导入（密码 / session cookie），面板和 bot 都调它们 |
| `metapi_bridge.py` | metapi 接管前的独立签到方案，现已停用，保留作退路 |

## 前置条件

- **Linux**（要 `xvfb`：`apt install xvfb`）
- **Python 3.11+**
- **uv**（推荐，上游用 uv 管理）或 pip + venv
- 一台能访问目标站点的机器。**能否直连取决于你的出口 IP** —— 若出口被 ESA 拦成
  全站 403，需要配代理（见下文「代理」）
- metapi 已部署（本目录要读它的 `.env` 和 `data/hub.db`）

## 部署步骤

### 1. 拉上游并叠加本目录

```bash
git clone https://github.com/millylee/anyrouter-check-in.git ~/anyrouter-check-in
cd ~/anyrouter-check-in
git rev-parse HEAD          # 记下来，本 fork 的适配基线

# 把本目录的文件复制进去（任意方式：cp / rsync / 直接从本仓库 checkout 后拷贝）
cp /path/to/banyan/checkin-backend/*.py .
cp /path/to/banyan/checkin-backend/*.sh .
chmod +x *.sh
```

### 2. 装依赖和浏览器

用 uv（推荐，与上游一致）：

```bash
cd ~/anyrouter-check-in
uv sync
uv run python -m cloakbrowser install      # 装 CloakBrowser 浏览器，必须做
```

用 pip：

```bash
cd ~/anyrouter-check-in
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m cloakbrowser install
```

> 装完浏览器建议先单独验一次登录，别等服务起不来再回头查：
> `uv run python run_checkin.py`（上游自带的单次签到入口，配置读同一个 `.env`）

### 3. 配置账号与代理

按上游的 `.env.example` 建 `.env`（**JSON 必须单行**）：

```bash
cp .env.example .env && chmod 600 .env
```

必填项是 `ANYROUTER_ACCOUNTS`。若需要代理：

```
PROXY_SUBSCRIPTION_URL=https://example.com/sub?token=xxx
CHECKIN_PROXY_URL=http://127.0.0.1:7890
```

调试期可开 `DEBUG_MODE=true`（会存登录截图、打印代理地址），**稳定后关掉**。

### 4. 配 metapi 侧的两个 token

配套后端**不读自己的独立配置**，而是从 **metapi 的 `.env`** 里读这几个键
（路径由 `METAPI_DIR` 决定，默认 `/opt/metapi`）：

| 键 | 用途 |
|---|---|
| `AUTH_TOKEN` | panel 的认证（`panel.py` 用它与请求头比对） |
| `TELEGRAM_BOT_TOKEN` | TG bot（不用 bot 可忽略） |
| `TELEGRAM_CHAT_ID` | TG bot 的白名单 chat id，**同时是唯一 owner**（非白名单静默忽略） |

所以先在 metapi 的 `.env` 里确保 `AUTH_TOKEN` 是强随机值。

### 5. 起服务

```bash
# 前台试跑，确认能起来
PANEL_PORT=4100 ./run_panel.sh
# 另一个终端：确认 RPC 活着
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $AUTH_TOKEN" \
  http://127.0.0.1:4100/__checkin/status
```

转 systemd（`/etc/systemd/system/checkin-panel.service`）：

```ini
[Unit]
Description=签到运维面板（配合 metapi，管签到/远程登录）
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/root/anyrouter-check-in
Environment=PATH=/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONIOENCODING=utf-8
Environment=PANEL_PORT=4100
# 部署目录和默认值不同时才需要这几行：
# Environment=CHECKIN_DIR=/root/anyrouter-check-in
# Environment=METAPI_DIR=/opt/metapi
# Environment=MIHOMO_DIR=/opt/mihomo
ExecStart=/root/anyrouter-check-in/run_panel.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now checkin-panel
journalctl -u checkin-panel -f
```

### 6. 接回 metapi

metapi 是容器，要能访问宿主机上的 4100 端口。在它的 `docker-compose.yml` 里：

```yaml
services:
  metapi:
    extra_hosts:
      - "host.docker.internal:host-gateway"   # 否则容器里解析不到宿主机
    environment:
      EXT_WAF_URL: http://host.docker.internal:4100/__checkin/ext
      EXT_WAF_TIMEOUT_MS: "240000"
```

然后 `docker compose up -d` 让配置生效。加账号时**站点 `platform` 必须填 `anyrouter`**
（agentrouter 也填它，adapter 按 URL 分辨），填 `new-api` 会掉回纯 HTTP 实现，
症状是签到报 `Invalid URL (POST /api/user/checkin)` 或验证超时。

## 环境变量

全部可选，默认值即作者原部署的值。

| 变量 | 默认 | 说明 |
|---|---|---|
| `CHECKIN_DIR` | 本目录 | 上游 clone 目录 |
| `METAPI_DIR` | `/opt/metapi` | metapi 部署目录 |
| `MIHOMO_DIR` | `/opt/mihomo` | mihomo 配置目录 |
| `PANEL_PORT` | `4100` | panel 监听端口 |
| `UV_BIN` | 自动探测 | uv 可执行文件；探测顺序 `UV_BIN` → `PATH` → `~/.local/bin/uv` |
| `EXT_WAF_URL` | `http://host.docker.internal:4100/__checkin/ext` | **metapi 容器侧**读的变量 |
| `EXT_WAF_TIMEOUT_MS` | `240000` | 同上 |

## 关键约束

- 站点 `platform` 必须填 `anyrouter`（agentrouter 也填它）
- 账号必须有 `platformUserId`，否则 `/api/user/self` 一律 401「未提供 New-Api-User」
- 存量 WAF cookie 不能复用，必须每次由浏览器现拿（见 `ext_api.py` 的 `WAF_COOKIE_NAMES`）
- **不要把调度值调高**：每次调用都真起一个浏览器，高频请求会让出口 IP 被 ESA 拉黑
  （本 fork 已把 `BALANCE_REFRESH_CRON` 降到每天 2 次、签到间隔 12 小时）
- 代理必须用 `select` 固定节点：WAF cookie 与 session 绑出口 IP

## Telegram 机器人（可选）

`telegram_bot.py` + `tg_fmt.py` + `run_tgbot.sh`（systemd 单元 `tgbot.service`）。

**为什么不用 metapi 自带的通知**：它只在失败时 `sendNotification`（checkinService
里只有 error/warning 两处），签到成功一声不响；而且没有接收侧，无法反向操作。
所以 bot 接管全部 Telegram 交互，metapi 的 `TELEGRAM_ENABLED` 必须设为 `false`，
否则一条事件会被推两次。

- **推送**：轮询 `events` 表（metapi 每次签到都会写），用 `last_event_id` 记位点 ——
  比时间窗口可靠，重启不漏推不重推；首次启动从当前最大 id 开始，不补推历史。
  `reward` 在 `checkin_logs` 里而 `events` 里没有，会补查后拼进消息。
- **命令**：长轮询 `getUpdates`，操作转成 metapi 的 REST 调用。

**安全**：只响应白名单 `chat_id`，其它静默忽略（不回复，避免被探测出是 bot）；
含密码/session 的用户消息**先删除再执行耗时操作**（否则密码会在聊天记录里停留
一两分钟）；删除账号走 inline keyboard 二次确认。

状态灯统一约定：🟢 正常 / 🟡 需注意 / 🔴 要处理 / ⚪ 主动禁用。
账号灯的判断顺序是有意的 —— 先看是否被禁用，再看硬故障（缺 api_user / 无凭据），
最后才看「久未签到」这类软信号，否则禁用的账号会被误报成红灯。

### 代理看门狗

`telegram_bot.py` 里的 `proxy_watchdog()` 线程，每 5 分钟经代理探一次目标站。

**为什么需要**：proxy-group 用 `select` 固定节点（WAF cookie 与 session 绑出口 IP，
`url-test`/`fallback` 那种自动漂移会让已登录 session 失效）。代价是节点真挂了不会
自动切 —— 2026-08-20 美国-直连 `connect error: EOF`，连续两轮签到全军覆没，
`id=8` 余额被用到只剩 $0.61 才被发现。

**为什么要连续 3 次（约 15 分钟）才切**：网络抖动就切，IP 会无谓漂移，那正是当初
放弃 `fallback` 的原因。宁可多等十几分钟，也不要频繁换 IP。

**为什么不用 mihomo 的 delay 接口做判据**：它只测到 gstatic 的连通性。实测出现过
delay 正常但目标站 EOF 的情况 —— 必须直接请求目标站才算数。目标站返回 403/404
不算节点故障（连得上，只是被拦，属另一类问题）。

切换成功/失败都会推 Telegram，不会默默处理。

### 两个判据上的坑（已修）

**不要用 `accounts.last_checkin_at` 判断「上次签到」。** metapi 只在
`directCheckinSuccess`（真拿到奖励）时才推进它；interval 调度下「今日已签到」
不推进（`checkinService.ts` 的 `shouldAdvanceLastCheckinAt`）。而 agentrouter 的
`sign_in_path` 是 `None`、查用户信息即完成签到，返回 message 总命中「今日已签到」
—— 于是它的 `last_checkin_at` 长期为 `None`，拿它判灯会**永久误报黄灯**。
正确判据是 `checkin_logs` 里 `status=success` 的最近时间。

**退避状态要两道检查。** `.login_cooldown.json` 是独立签到方案的机制，签到交给
metapi 后那套已 disabled，文件变成不再被更新也不被清除的遗物。所以必须：
先看 `anyrouter-checkin.timer` 是否 `enabled`（机制在不在跑），再按 1h/2h/4h…
算是否**仍未到期** —— 只看 `failures > 0` 会把 40 小时前的记录一直报成「退避中」。

误报比不报更糟：它会让人对告警脱敏，真出事时反而忽略。

## 故障排查

| 症状 | 先查这里 |
|---|---|
| `browser has been closed` | 没套 xvfb，或 `DISPLAY` 被清掉 |
| 服务起不来、`uv: command not found` | uv 不在 systemd 的 PATH 里 → 设 `UV_BIN` 或在 unit 里补 `Environment=PATH=...` |
| panel 起来但 metapi 连不上 | `extra_hosts` 没配 / 端口没通；容器内 `curl host.docker.internal:4100` 试 |
| 添加账号报 `Token verification timed out` | 拿到的还是 WAF 挑战页 HTML；查代理出口是否被拉黑 |
| 全站 403（含首页）、`via` 头带 `403102` | 出口 IP 被 ESA 拉黑，换节点，别再调高调度频率 |
| `/api/user/self` 401 未提供 New-Api-User | 账号缺 `platformUserId`，重新登录一次写回 |
| 余额一直 0 | 同上，或 `getBalance` 静默失败（本 fork 已改为不吞异常） |
