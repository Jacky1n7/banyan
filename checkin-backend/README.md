# 浏览器能力后端（配套本 fork）

这些文件运行在宿主机 `/opt/anyrouter-check-in/`（`mihomo_setup_proxy.py` 在
`/opt/mihomo/setup_proxy.py`），**这里只是版本管理副本** —— 机器挂了不至于全丢。
它们依赖上游 [millylee/anyrouter-check-in](https://github.com/millylee/anyrouter-check-in)
的 `checkin.py` / `utils/`（CloakBrowser 登录、WAF cookie 获取、签到请求），
所以不能独立运行。

| 文件 | 职责 |
|---|---|
| `ext_api.py` | 本 fork 的 AnyRouterAdapter 调用的 RPC 实现：login / verify / userinfo / balance / checkin。**无状态** —— 不写库、不推送、不调度 |
| `panel.py` | HTTP 服务，路由 `/__checkin/ext/*`。同时监听回环与 docker 网桥（metapi 是容器，只绑回环连不上）；不绑 `0.0.0.0`（该端口在云安全列表放行，绑通配等于对公网开代理） |
| `run_panel.sh` | systemd 入口。**必须套 xvfb** —— login 走有头 Chromium（反检测优于无头），作为服务没有 X 显示会报 `browser has been closed`。不能写进 ExecStart：systemd 不做 shell 解析，`--server-args="-screen 0 ..."` 的空格会被拆成多个参数 |
| `mihomo_setup_proxy.py` | 生成 mihomo 配置。proxy-group **必须是 `select` 固定节点** —— WAF cookie 与 session 绑出口 IP，`url-test`/`fallback` 换节点会让已登录 session 立刻失效 |
| `metapi_bridge.py` 等 | metapi 接管前的独立签到方案，现已停用（`anyrouter-checkin.timer` disabled），保留作退路 |

## 关键约束

- 站点 `platform` 必须填 `anyrouter`（agentrouter 也填它，adapter 按 URL 分辨）
- 账号必须有 `platformUserId`，否则 `/api/user/self` 一律 401「未提供 New-Api-User」
- 存量 WAF cookie 不能复用，必须每次由浏览器现拿（见 `WAF_COOKIE_NAMES`）

## Telegram 机器人

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
