<div align="center">

<img src="docs/logos/banyan-banner.png" alt="Banyan" width="840">

**把散落在各个 AI 中转站的额度，聚成一个入口**

[![License](https://img.shields.io/badge/license-MIT-brightgreen?style=flat)](./LICENSE)
[![Node.js](https://img.shields.io/badge/Node.js-25%2B-339933?logo=node.js&style=flat)](https://nodejs.org)
[![TypeScript](https://img.shields.io/badge/TypeScript-5.x-3178C6?logo=typescript&style=flat)](https://www.typescriptlang.org)
[![Docs](https://img.shields.io/badge/docs-jacky1n7.github.io%2Fbanyan-0ea5e9?style=flat)](https://jacky1n7.github.io/banyan/)

[快速开始](#快速开始) · [与上游的区别](#与上游-metapi-的区别) · [浏览器后端](#2-可选部署浏览器后端) · [常见问题](#常见问题) · [English](./README_EN.md)

</div>

---

Banyan 是 [cita-777/metapi](https://github.com/cita-777/metapi) 的**破坏性 fork**。
它保留 metapi 的全部能力：多站点聚合成一个 OpenAI / Claude 兼容入口、模型发现与智能路由、
账号与令牌管理、自动签到、消息推送、代理日志、桌面版。只改一件事：

> **anyrouter / agentrouter 的登录、校验、余额、签到改走一个真浏览器后端**，
> 用来通过阿里云 ESA 的新版 JS 挑战。上游的纯 HTTP 实现过不了这道挑战。

名字取自榕树：一棵主干垂下无数气生根，根须各自扎地成干，独木成林。

## 它解决什么问题

anyrouter / agentrouter 前面挂着阿里云 ESA，下发的是 `aliyun_waf_aa` / `aliyun_waf_bb`
这套**必须执行页面脚本**才能通过的挑战；上游 `newApiShield.ts` 的 `node:vm` 求解只认老的
`var arg1` + `acw_sc__v2` 形式。用上游对接这两个站，会反复看到：

| 症状 | 真实原因 |
|---|---|
| 添加账号报 `Token verification timed out (10s)` | `verifyToken` 拿到的是挑战页 HTML |
| 报 `This site is shielded by anti-bot challenge` | `login` 走纯 HTTP，过不了挑战 |
| 签到间歇失败 `Unexpected token '<'`、余额一直不动 | 响应是挑战页，`getBalance` 静默失败 |

Banyan 在 adapter 层把这几个方法转发给外部的浏览器能力后端（CloakBrowser 真浏览器 + 可选代理），
由它解掉挑战再请求接口。全仓库 6 处调用点与前端一行都不用改。

## 要不要用 Banyan

- **不碰 anyrouter / agentrouter** —— 直接用上游 metapi 就好；或者把 Banyan 当作
  「修过几个 ID 解析 bug、调度更保守、CI 正常」的 metapi 用，其余站点行为与上游一致。
- **要用这两个站** —— 必须额外部署[浏览器后端](#2-可选部署浏览器后端)（真浏览器 + xvfb + 可选代理，
  跑在你的宿主机上）。不部署它，添加账号 / 签到 / 余额刷新会全部失效。
- **托管平台（Render / Zeabur 等）** 起不了 xvfb 和真浏览器，也访问不到你主机上的后端，
  只能跑非 anyrouter 站点的简化形态。

## 快速开始

### 1. 启动 Banyan

本 fork 不发布 Docker 镜像，compose 直接从源码构建：

```bash
git clone https://github.com/Jacky1n7/banyan.git
cd banyan
cp .env.example .env && chmod 600 .env
# 必须改：AUTH_TOKEN（面板登录）、PROXY_TOKEN（下游调用）
docker compose -f docker/docker-compose.yml up -d --build
```

不用 Docker 也可以：`npm ci && npm run build && npm start`（Node.js 25+，见 `.nvmrc`）。
面板默认在 `http://127.0.0.1:4000`。

### 2.（可选）部署浏览器后端

只有要用 anyrouter / agentrouter 才需要这一步。完整步骤见
[`checkin-backend/README.md`](./checkin-backend/README.md)，概括是：clone
[millylee/anyrouter-check-in](https://github.com/millylee/anyrouter-check-in)，把本仓库
`checkin-backend/` 里的文件复制进去，用 systemd 跑起来。然后让 Banyan 能找到它：

```yaml
# docker/docker-compose.yml 的 metapi 服务里追加
services:
  metapi:
    extra_hosts:
      - "host.docker.internal:host-gateway"
    environment:
      EXT_WAF_URL: http://host.docker.internal:4100/__checkin/ext
      EXT_WAF_TIMEOUT_MS: "240000"
```

添加站点时 **`platform` 必须填 `anyrouter`**（agentrouter 也填这个，adapter 按 URL 分辨）。
填 `new-api` 会掉回纯 HTTP 实现，上面那些症状会原样出现。

### 3. 不要调高调度频率

> [!WARNING]
> 每次调用都会真起一个浏览器。2026-09-02 实测：上游默认的每小时刷余额 + 6 小时签到，
> 约 60 次/天全部来自同一出口 IP，结果 ESA 把该 IP 拉黑，**全站**（含首页）返回 403。
> 本 fork 的默认值已降到每天 2 次余额、签到间隔 12 小时（约 8 次/天）。签到奖励一天只发一份，
> 刷得再勤也拿不到第二份。

## 架构

```
你的客户端（OpenAI / Claude 兼容）
        │  一个 API Key
        ▼
┌───────────────────────────────────────────────┐
│  Banyan（面板 / 连接管理 / 调度 / 路由 / 日志）  │
│                                               │
│  NewApiAdapter ──> New API / One API / ...    │
│  AnyRouterAdapter ──HTTP RPC──┐               │
└───────────────────────────────┼───────────────┘
                                ▼
              checkin-backend（真浏览器 + xvfb + 代理）
                    127.0.0.1:4100/__checkin/ext/*
                    └─ 解掉 JS 挑战，再请求站点接口
```

后端故意做成无状态：调度、连接管理、签到日志、消息推送全部仍由 Banyan 负责，
两边不会各推一份通知、各记一份日志。

## 与上游 metapi 的区别

| 改动 | 为什么 |
|---|---|
| `anyrouter.ts` 的 5 个方法转发到浏览器后端 | 过 ESA 新挑战必须执行 JS；`getBalance` 不再吞异常，否则会把 0 当真实余额写库 |
| `platformUserId` 不再从用户名末尾猜 | 注册邮箱形式的用户名猜不到 → 401「未提供 New-Api-User」→ 余额恒为 0 |
| 自动重登把新 ID 写回 | 原本只写回 `accessToken`，下次照样 401 |
| 相关超时从 10s / 30s 放宽到 240s | 真浏览器过挑战单次 12–90 秒是常态 |
| 调度默认值降频 | 见上面的封禁实测 |
| 删除上游遗留的 `pnpm-lock.yaml` | 它让 Dependabot 的 npm 任务每次都失败，安全告警 PR 收不到 |

有意**保留**的上游痕迹：localStorage 键、备份文件名（`metapi-backup-*.json`）、Docker service
名等内部标识仍叫 `metapi`，改了会破坏已有配置与备份的兼容性，也会让每次 rebase 上游都冲突。
完整技术细节、踩坑记录见 [`LOCAL_FORK.md`](./LOCAL_FORK.md)。

## 升级

本 fork 在 `local-fork` 分支维护：

```bash
git pull && docker compose -f docker/docker-compose.yml up -d --build
```

面板里的「更新中心」只检查本仓库的 Releases（可用 `UPDATE_CENTER_GITHUB_REPO` /
`UPDATE_CENTER_DOCKER_REPO` 覆盖），不会再提示上游版本。k3s 一键更新需要你自己构建推送的镜像，
**不要填上游镜像** `1467078763/metapi`，那会把 fork 的改动覆盖掉。

## 常见问题

**出口 IP 被拉黑了（全站 403，`via` 头带 `403102`）怎么办？**
换出口节点。这是 IP 级封禁而不是指纹问题：同一时刻家用宽带能正常拿到挑战页，
被拉黑的服务器两条出口都是硬 403。恢复后不要再调高调度频率。

**为什么浏览器后端要放在 anyrouter-check-in 的目录里跑？**
它复用该项目的登录与 cookie 获取逻辑，所以那些文件必须在它的 clone 目录内运行。
部署路径可用 `CHECKIN_DIR` / `METAPI_DIR` / `MIHOMO_DIR` 环境变量覆盖。

**文档站写的是上游的功能？**
是。[文档站](https://jacky1n7.github.io/banyan/)内容承自上游，描述的是两边共有的能力；
本 fork 独有的 anyrouter 部分只在本 README 与 `LOCAL_FORK.md` 里。

**还能把它当普通 metapi 用吗？**
能。非 anyrouter 站点的行为与上游一致，`platform` 照上游的填即可。

## 许可与致谢

[MIT](./LICENSE)，与上游一致。本项目 fork 自 [cita-777/metapi](https://github.com/cita-777/metapi)，
浏览器后端依赖 [millylee/anyrouter-check-in](https://github.com/millylee/anyrouter-check-in)
与 [CloakBrowser](https://pypi.org/project/cloakbrowser/)。感谢上游作者与所有贡献者。

<div align="center">
<sub>使用前请自行确认目标站点的服务条款。自动签到与绕过反爬可能违反站点规定，风险自负。</sub>
</div>
