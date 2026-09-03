# 本地 fork 说明

这是 [cita-777/metapi](https://github.com/cita-777/metapi) 的**破坏性 fork**，
不与上游保持兼容。基线 commit：`41767a6`（2026-06-24）。

## 为什么要 fork

metapi 对接 anyrouter / agentrouter 时，`login` / `verifyToken` / `checkin` /
`getBalance` 全都走纯 HTTP。而这两个站前面挂着阿里云 ESA，下发的是
`aliyun_waf_aa` / `aliyun_waf_bb` 那套**新版 JS 挑战 —— 必须执行页面脚本才能通过**。
上游 `newApiShield.ts` 里那套 `node:vm` 求解只认老的 `var arg1` + `acw_sc__v2`
形式，对新挑战无效。实测症状：

| 症状 | 真实原因 |
|---|---|
| 添加账号报 `Token verification timed out (10s)` | `verifyToken` 拿到的是挑战页 HTML，不是 JSON |
| 添加账号报 `This site is shielded by anti-bot challenge` | `login` 走纯 HTTP POST `/api/user/login`，过不了挑战 |
| 签到间歇失败 `Unexpected token '<'` | 同上，响应是挑战页 |
| 面板上余额一直不动 | `getBalance` 静默失败 |

所以把这几个方法转发给一个**外部浏览器能力后端**（CloakBrowser 真浏览器 + 代理），
由它把挑战解掉再请求接口。

## 架构

```
metapi（唯一入口：面板 / 连接管理 / 调度 / 签到日志 / 消息推送）
└── AnyRouterAdapter ──HTTP RPC──> 浏览器能力后端 127.0.0.1:4100/__checkin/ext/*
                                   （无状态：不写库、不推送、不调度）
```

后端**故意做成无状态**：调度、连接管理、`checkin_logs`、消息推送全部由 metapi 负责，
避免两套系统各推一份通知、各记一份日志。

后端代码不在本仓库，位于 `/opt/anyrouter-check-in/`：

| 文件 | 职责 |
|---|---|
| `ext_api.py` | 本仓库 adapter 调用的 RPC 实现（login / verify / userinfo / balance / checkin） |
| `panel.py` | HTTP 服务，路由 `/__checkin/ext/*`；同时监听回环与 docker 网桥（容器要能访问） |
| `run_panel.sh` | systemd 入口，**必须套 xvfb** |

它复用上游 [millylee/anyrouter-check-in](https://github.com/millylee/anyrouter-check-in)
的 `checkin.py` / `utils/`（CloakBrowser 登录、WAF cookie 获取、签到请求）。

## 改了什么

### 1. `src/server/services/platforms/anyrouter.ts`（核心，+148 行）

重写 5 个方法转发到后端：`login` / `verifyToken` / `getUserInfo` / `getBalance` / `checkin`。

**只改 adapter、不改调用点**是有意的：全仓库有 6 处调用
（`checkinService:107`、`balanceService:223`、`manualAccountCreationService:197`、
`accounts.ts:528/911/1145`），改 adapter 一处就让添加账号、token 验证、签到、
余额刷新、失败自动重登**全部**获得真浏览器能力，且 API 契约与前端一行都不用动。

`getBalance` 故意不吞异常 —— 余额读不到必须让调用方知道，否则会把 0 当真实余额写库。

### 2. `platformUserId` 不能靠猜（`base.ts` / `newApi.ts` / `accounts.ts`）

上游用 `guessPlatformUserIdFromUsername()` 从用户名末尾抓 3-8 位数字。
`linuxdo_12345` 碰巧能抓到 `80305`，但 `user@example.com` 末尾是 `.com`、
`user@example.edu.cn` 也抓不到 → ID 为空 → 这两个站的 `/api/user/self`
**不带 `New-Api-User` 头一律 401**「无权进行此操作，未提供 New-Api-User」
→ 余额恒为 0、session 被误判失效、每轮都退回密码登录（正是招来 ESA 限流的原因）。

改动：`LoginResult` 加 `platformUserId`，登录时真浏览器读到的真实 ID 优先，
猜测只作兜底。

### 3. 自动重登要写回 ID（`checkinService.ts` / `balanceService.ts`）

原本只写回 `accessToken`，重登拿到的新 ID 会丢，下次照样 401。
用 `mergeAccountExtraConfig` 合并写入 —— **不能直接覆盖 `extraConfig`**，
否则会抹掉 `autoRelogin` 里的加密密码。

### 4. 超时（`accounts.ts` / `manualAccountCreationService.ts` / `web/api.ts`）

浏览器起进程、过 JS 挑战、跳转 `/console` 再请求接口，单次 12-90 秒是常态。

- 后端：`ACCOUNT_VERIFY_TIMEOUT_MS`、`ACCOUNT_HEALTH_REFRESH_TIMEOUT_MS`
  从硬编码 `10_000` 改为 `240_000`
- 前端：`request()` 默认 `timeoutMs = 30_000` 太短，给 6 个会走浏览器的调用
  加了 `BROWSER_OP_TIMEOUT_MS`（240s）/ `BROWSER_BATCH_TIMEOUT_MS`（600s）

### 5. 调度默认值降频（`config.ts` / `.env.example` / `docker/` / `deploy/k3s/`）

上游默认 `BALANCE_REFRESH_CRON=0 * * * *`（每小时）+ `CHECKIN_INTERVAL_HOURS=6`。
对纯 HTTP 的站点无所谓，但本 fork 每次调用都**真起一个浏览器**过 WAF：
2 个 anyrouter 账号 × 每小时刷余额 ≈ 48 次/天，加上签到共约 60 次/天，
且全部来自同一个出口 IP。

2026-09-02 实测后果：阿里云 ESA 把出口 IP 拉黑，**全站**（含首页 `/`）返回 403，
`via` 头带边缘拦截码 `403102`。定位靠的是对照实验 —— 同一时刻家用宽带 IP
访问 anyrouter.top 正常拿到 JS 挑战页，而服务器两条出口（Oracle 直连
`203.0.113.10` + 机场节点）全部硬 403。所以是 **IP 级封禁**，不是指纹问题、
不是挑战算法变更、也不是站点策略调整：

- CloakBrowser 当时**成功过了 WAF**（日志 `Got 3 WAF cookies` / `Successfully got all WAF cookies`），
  403 发生在拿到 cookie 之后的接口请求
- 把接口请求改成在浏览器页面内 `fetch`（真 TLS 指纹 + 同源 + 全部 WAF cookie）**照样 403**
- 上游 `newApiShield.ts` 的 `node:vm` 求解器输出与挑战页 JS 实际执行结果**逐字节一致**，
  算法没变（顺带说明：那条路径对 anyrouter 是死代码，见本文档开头）

改为 `37 9,21 * * *`（每天 2 次，且避开整点分钟）+ 间隔 12 小时后，
请求量从约 60 次/天降到约 8 次/天，同时保留每天一次重试余量。

**不要把这两个值调回去。** 签到奖励一天只发一份，刷得再勤也拿不到第二份；
而 `checkinService.ts` 里 `shouldRefreshBalance = result.success || alreadyCheckedIn`
—— 签到成功后本来就会顺带刷余额，独立的高频余额 cron 基本是冗余的。

### 6. 删除 `pnpm-lock.yaml`

Dependabot 的 npm 生态任务**每次都失败**（2026-08-19 / 08-23 / 08-30 / 09-03 全部
failure），错误是：

```
dependency_file_not_parseable | "/pnpm-lock.yaml not parseable"
```

后果是 npm 依赖更新与**安全告警 PR 全部收不到**（docker / github-actions 两个生态正常）。

本仓库实际只用 npm —— CI 四处均为 `npm ci` + `cache: npm`，`docker/Dockerfile`
也是 `npm ci`，且 `package.json` 无 `packageManager` 字段、仓库无
`pnpm-workspace.yaml` / `.npmrc`、全仓库零处引用 `pnpm-lock`。该文件是上游带来的
死文件，删掉不影响任何构建路径。

**rebase 时注意**：上游仍在更新这个文件（它与 `package-lock.json` 由同一批
Dependabot 提交一起改），所以每次 rebase 都会在此产生一次冲突，解法固定 ——
**保持删除**。

## 使用约定

**站点的 `platform` 必须填 `anyrouter`**，agentrouter 也填这个 —— adapter 按
站点 URL 分辨具体是哪个站。填 `new-api` 会掉回原版纯 HTTP 实现，症状就是
签到报 `Invalid URL (POST /api/user/checkin)`、验证超时。

环境变量（可选）：

- `EXT_WAF_URL`（默认 `http://host.docker.internal:4100/__checkin/ext`）
- `EXT_WAF_TIMEOUT_MS`（默认 240000）

容器需要 `extra_hosts: ["host.docker.internal:host-gateway"]` 才能解析宿主机。

## 构建与部署

```bash
cd /opt/metapi-src
docker build -f docker/Dockerfile -t metapi-local:vN .
sed -i 's/metapi-local:vN-1/metapi-local:vN/' /opt/metapi/docker-compose.yml
cd /opt/metapi && docker compose up -d
```

宿主机 Node 是 v20 而本项目要求 22+，所以**必须在 Docker 里构建**
（Dockerfile 自带 `node:22`）。arm64 上会原生编译 `better-sqlite3` / `sharp`，
一次约 4 分钟。

## rebase 上游时注意

`git pull` 已不可用。上游更新后手动 rebase，重点检查：

1. `newApiShield.ts` 是否已支持新版挑战 —— 若已支持，本 fork 的大部分改动可以退回
2. 那 6 处 `adapter.login` / `adapter.verifyToken` 调用点是否变动
3. `LoginResult` / `TokenVerifyResult` / `CheckinResult` / `BalanceInfo` 类型是否变动
4. 前端 `api.ts` 里 6 个调用的 `timeoutMs` 是否被覆盖回默认值
5. `config.ts` 的 `balanceRefreshCron` / `checkinIntervalHours` 默认值 ——
   上游若改动，**保留本 fork 的降频值**（原因见「改了什么」第 5 节）
6. `pnpm-lock.yaml` —— 上游会重新带回来，**保持删除**（原因见第 6 节）
