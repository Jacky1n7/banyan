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
