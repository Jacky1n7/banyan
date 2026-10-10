<div align="center">

<img src="docs/logos/banyan-banner.png" alt="Banyan" width="840">

**Turn scattered AI relay-credit into one endpoint**

[![License](https://img.shields.io/badge/license-MIT-brightgreen?style=flat)](#license)
[![Node.js](https://img.shields.io/badge/Node.js-25%2B-339933?logo=node.js&style=flat)](https://nodejs.org)
[![TypeScript](https://img.shields.io/badge/TypeScript-5.x-3178C6?logo=typescript&style=flat)](https://www.typescriptlang.org)

[Features](#features) · [Differences from upstream](#differences-from-upstream-metapi) · [Quick start](#quick-start) · [FAQ](#faq)

</div>

---

> [!IMPORTANT]
> **Banyan is a destructive fork of [metapi](https://github.com/cita-777/metapi);
> it does not track upstream compatibility.**
>
> It rewrites anyrouter / agentrouter `login` / `verifyToken` / `getBalance` /
> `checkin` to delegate to a **real-browser backend**, so the Aliyun ESA JS challenge
> can be solved (upstream's `node:vm` solver only handles the older format).
>
> **Read before deploying**
>
> - This fork has an **extra external dependency**: [`checkin-backend/`](./checkin-backend/),
>   which you must deploy separately (real browser + xvfb + optional proxy). Without it,
>   adding accounts / check-in / balance refresh all fail, and the only symptom is a
>   hard-to-diagnose timeout such as `Token verification timed out`. Full steps in
>   [`checkin-backend/README.md`](./checkin-backend/README.md) (Chinese).
> - Everything else is unaffected — you can use Banyan purely for the upstream features.
> - Some UI copy and internal identifiers (localStorage keys, backup filenames) still use
>   the upstream `metapi` naming. This is intentional: renaming them would break existing
>   users' configs and backup compatibility.
> - **Managed platforms (Render / Zeabur, etc.) cannot run the full shape**: a container
>   can't run xvfb or a real browser, nor reach the `checkin-backend` on your host.
>   There you are limited to non-anyrouter sites.

## The name

**Banyan** is the banyan tree. A single trunk drops countless aerial roots, and each root
takes hold and becomes a trunk of its own — one tree, a whole grove. That is what this
project does: one endpoint that catches what is scattered.

## Differences from upstream metapi

### Why fork

When metapi talks to anyrouter / agentrouter, `login` / `verifyToken` / `checkin` /
`getBalance` all go over plain HTTP. Those sites sit behind Aliyun ESA, which serves the
new `aliyun_waf_aa` / `aliyun_waf_bb` **JS challenge — it must be executed in a page to
pass**. Upstream's `newApiShield.ts` `node:vm` solver only understands the old
`var arg1` + `acw_sc__v2` form and does nothing for the new one. Observed symptoms:

| Symptom | Actual cause |
|---|---|
| `Token verification timed out (10s)` when adding an account | `verifyToken` gets challenge HTML instead of JSON |
| `This site is shielded by anti-bot challenge` | `login` POSTs over plain HTTP and can't pass the challenge |
| Intermittent `Unexpected token '<'` on check-in | Same — the response is the challenge page |
| Balance never updates in the panel | `getBalance` fails silently |

So these methods now forward to an **external browser-capability backend**
(CloakBrowser real browser + proxy), which solves the challenge and then calls the API.

### What changed

The swap happens at the adapter layer, **without touching a single call site** — all six
callers (`checkinService` / `balanceService` / `manualAccountCreationService` / the
`accounts` routes) gain real-browser capability at once, with no API-contract or
frontend changes.

| # | Change | Why |
|---|---|---|
| 1 | `anyrouter.ts` — 5 methods rewritten | Forward to the browser backend; `getBalance` no longer swallows errors (otherwise a `0` gets written as a real balance) |
| 2 | `platformUserId` no longer guessed | Upstream scrapes digits off the username's tail; email-style usernames yield nothing → 401 `New-Api-User` missing → balance stuck at 0 |
| 3 | Re-login writes the ID back | It used to persist only `accessToken`, so the freshly obtained ID was lost and the next call 401'd again |
| 4 | Timeouts widened | A real browser solving a challenge takes 12-90s; the hard-coded 10s/30s always timed out |
| 5 | Scheduler defaults slowed down | Every call really launches a browser; a high rate gets your egress IP blacklisted (see below) |
| 6 | `pnpm-lock.yaml` removed | A dead upstream file that made Dependabot's npm ecosystem fail every run, so dependency and security PRs never arrived |

> [!WARNING]
> **Do not raise the scheduler frequency.** Measured on 2026-09-02: the defaults
> (hourly balance refresh + 6-hourly check-in) came to ~60 requests/day from a single
> egress IP, and Aliyun ESA blacklisted that IP — **the whole site** (including the
> homepage) returned 403. Slowing to 2 balance runs/day and a 12-hour interval brings it
> to ~8/day. Check-in pays out once per day regardless.

Full technical detail, architecture and field notes: [`LOCAL_FORK.md`](./LOCAL_FORK.md) (Chinese).

## Architecture

```
Your client (OpenAI / Claude compatible)
        │  one API key
        ▼
┌───────────────────────────────────────────────┐
│  Banyan (panel / accounts / scheduling / routing / logs) │
│                                               │
│  NewApiAdapter ──> New API / One API / ...    │
│  AnyRouterAdapter ──HTTP RPC──┐               │
└───────────────────────────────┼───────────────┘
                                ▼
              checkin-backend (real browser + xvfb + proxy)
                    127.0.0.1:4100/__checkin/ext/*
                    └─ solves the JS challenge, then calls the site API
```

The backend is **deliberately stateless**: scheduling, account management, check-in logs
and notifications all stay in Banyan, so you never get double notifications or split logs.

## Features

Beyond the browser backend, Banyan keeps everything metapi offers:

- **Multi-site aggregation** — New API / One API / OneHub / DoneHub / Veloera /
  AnyRouter / Sub2API and more, unified behind one OpenAI / Claude compatible endpoint
- **Automatic model discovery + smart routing** — picked by availability and cost
- **Account & token management** — multi-account, groups, auto re-login, quota monitoring
- **Automatic check-in** — scheduled runs, logs and reward records
- **Notifications** — Webhook / Bark / ServerChan / Telegram, etc.
- **Proxy logs** — upstream and downstream request tracing for debugging
- **Self-hosted** — data lives in local SQLite (MySQL / Postgres also supported), nothing
  is sent to third parties
- **Desktop app** — also runs as an Electron application

For UI and API details see the [upstream docs site](https://metapi.cita777.me) — note it
documents the **upstream version** and does not cover this fork's anyrouter changes.

> [!NOTE]
> This fork additionally ships an optional Telegram admin bot (check-in notifications,
> commands, proxy watchdog). See [`checkin-backend/README.md`](./checkin-backend/README.md).

## Quick start

### Upstream features only (no anyrouter / agentrouter)

Deploy the upstream way:

```bash
git clone https://github.com/Jacky1n7/banyan.git
cd banyan
cp .env.example .env && chmod 600 .env
# Must change: AUTH_TOKEN (panel login), PROXY_TOKEN (downstream clients)
docker compose -f docker/docker-compose.yml up -d --build
```

Or run it directly: `npm ci && npm run build && npm start` (needs Node.js 25+, see `.nvmrc`).

### With anyrouter / agentrouter (browser backend required)

One extra step: deploy the browser backend per
[`checkin-backend/README.md`](./checkin-backend/README.md), then wire it up in your
Banyan compose file:

```yaml
services:
  banyan:
    extra_hosts:
      - "host.docker.internal:host-gateway"
    environment:
      EXT_WAF_URL: http://host.docker.internal:4100/__checkin/ext
      EXT_WAF_TIMEOUT_MS: "240000"
```

When adding an account, **the site's `platform` must be `anyrouter`** (use it for
agentrouter too — the adapter distinguishes by URL).

### Upgrading

This fork is maintained on the `local-fork` branch. The update center's version check now
points at this repository, but **never set its image repository to the upstream image** —
that would overwrite this fork's changes (see Known limitations).

```bash
git pull && docker compose -f docker/docker-compose.yml up -d --build
```

## FAQ

**Q: If I don't use anyrouter, is this any different from upstream?**
Basically no, apart from more conservative scheduler defaults, a few ID-resolution bug
fixes, and CI that actually passes. You can treat it as "metapi with fixes".

**Q: Why must the browser backend be deployed separately?**
Because passing the WAF challenge requires a real browser to execute JS. It relies on
[millylee/anyrouter-check-in](https://github.com/millylee/anyrouter-check-in) for login
and cookie acquisition, so those files have to run inside that project's clone directory.

**Q: My egress IP got blacklisted (whole site 403, `via` header `403102`). What now?**
Switch egress nodes. This is IP-level blocking, not a fingerprint issue — measured at the
same moment, a home broadband line could still fetch the challenge page while both of the
blacklisted server's egresses got a hard 403. After recovering, **do not raise the
scheduler frequency**.

**Q: The update center says a new version is available — should I click it?**
The version check only looks at this repository's Releases now, so it no longer reports
upstream versions. The k3s one-click update still needs an image repository of your own:
do not enter the upstream image `1467078763/metapi`, or the fork's changes get overwritten.

## Known limitations

- **This fork publishes no Docker image** — the version check points at this repository
  (override with `UPDATE_CENTER_GITHUB_REPO` / `UPDATE_CENTER_DOCKER_REPO`) and no longer
  reports upstream versions, but the k3s update center needs an image repository you build
  and push yourself — **never the upstream image**.
- **Managed platforms only run the reduced shape** — see the notice at the top.
- **Only `anyrouter` is accepted as `platform`** — using `new-api` falls back to the plain
  HTTP implementation and anyrouter features stop working.
- **Residual upstream naming in the UI** — some descriptive copy and internal identifiers
  still say `metapi`, deliberately kept (see the notice at the top).
- **Docs site content comes from upstream** — it does not describe this fork's anyrouter
  changes; see `LOCAL_FORK.md` for those.

## License

[MIT](./LICENSE) — same as upstream.

This project is a fork of [cita-777/metapi](https://github.com/cita-777/metapi).
The browser backend builds on
[millylee/anyrouter-check-in](https://github.com/millylee/anyrouter-check-in) and
[CloakBrowser](https://pypi.org/project/cloakbrowser/). Thanks to the upstream authors
and contributors.

---

<div align="center">
<sub>Check your target sites' terms of service first. Automated check-in and anti-bot
bypass may violate site rules — use at your own risk.</sub>
</div>
