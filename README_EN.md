<div align="center">

<img src="docs/logos/banyan-banner.png" alt="Banyan" width="840">

**Turn scattered AI relay credit into one endpoint**

[![License](https://img.shields.io/badge/license-MIT-brightgreen?style=flat)](./LICENSE)
[![Node.js](https://img.shields.io/badge/Node.js-25%2B-339933?logo=node.js&style=flat)](https://nodejs.org)
[![TypeScript](https://img.shields.io/badge/TypeScript-5.x-3178C6?logo=typescript&style=flat)](https://www.typescriptlang.org)
[![Docs](https://img.shields.io/badge/docs-jacky1n7.github.io%2Fbanyan-0ea5e9?style=flat)](https://jacky1n7.github.io/banyan/)

[Quick start](#quick-start) · [Differences from upstream](#differences-from-upstream-metapi) · [Browser backend](#2-optional-deploy-the-browser-backend) · [FAQ](#faq) · [中文](./README.md)

</div>

---

Banyan is a **destructive fork** of [cita-777/metapi](https://github.com/cita-777/metapi).
It keeps everything metapi does — aggregating relay sites behind one OpenAI / Claude compatible
endpoint, model discovery and routing, account and token management, auto check-in, notifications,
proxy logs, a desktop build — and changes exactly one thing:

> **Login, token verification, balance and check-in for anyrouter / agentrouter go through a
> real-browser backend** that clears Alibaba Cloud ESA's new JavaScript challenge. Upstream's
> plain-HTTP implementation cannot.

The name is the banyan tree: one trunk, countless aerial roots, each taking hold as a new trunk.

## The problem it solves

anyrouter / agentrouter sit behind Alibaba Cloud ESA, which now serves an `aliyun_waf_aa` /
`aliyun_waf_bb` challenge that **requires executing page script**. Upstream's `node:vm` solver in
`newApiShield.ts` only understands the old `var arg1` + `acw_sc__v2` form. With upstream you keep hitting:

| Symptom | Actual cause |
|---|---|
| Adding an account fails with `Token verification timed out (10s)` | `verifyToken` received the challenge page, not JSON |
| `This site is shielded by anti-bot challenge` | `login` is a plain HTTP POST and cannot pass |
| Check-in flakes with `Unexpected token '<'`, balance never moves | The response is the challenge page; `getBalance` fails silently |

Banyan forwards those adapter methods to an external browser backend (CloakBrowser real browser plus
an optional proxy) that clears the challenge before calling the API. None of the six call sites or
the frontend had to change.

## Should you use it

- **You don't use anyrouter / agentrouter** — just use upstream metapi, or treat Banyan as
  "metapi with a few ID-resolution fixes, calmer scheduling and a passing CI". Every other site
  behaves exactly like upstream.
- **You do** — you must also deploy the [browser backend](#2-optional-deploy-the-browser-backend)
  (real browser + xvfb + optional proxy) on your own host. Without it, adding accounts, check-in and
  balance refresh all fail.
- **Managed platforms (Render / Zeabur …)** cannot run xvfb or a real browser and cannot reach a
  backend on your machine, so they only run the reduced shape for non-anyrouter sites.

## Quick start

### 1. Run Banyan

This fork publishes no Docker image; compose builds from source:

```bash
git clone https://github.com/Jacky1n7/banyan.git
cd banyan
cp .env.example .env && chmod 600 .env
# must change: AUTH_TOKEN (dashboard login), PROXY_TOKEN (downstream calls)
docker compose -f docker/docker-compose.yml up -d --build
```

Or without Docker: `npm ci && npm run build && npm start` (Node.js 25+, see `.nvmrc`).
On npm 12+ the install may report blocked install scripts, which leaves native modules such as
`better-sqlite3` uncompiled; run `npm install-scripts approve better-sqlite3 esbuild sharp` first.
The dashboard listens on `http://127.0.0.1:4000` by default.

### 2. (Optional) Deploy the browser backend

Only needed for anyrouter / agentrouter. Full steps are in
[`checkin-backend/README.md`](./checkin-backend/README.md); in short: clone
[millylee/anyrouter-check-in](https://github.com/millylee/anyrouter-check-in), copy the files from
this repo's `checkin-backend/` into it, run it under systemd. Then point Banyan at it:

```yaml
# add to the metapi service in docker/docker-compose.yml
services:
  metapi:
    extra_hosts:
      - "host.docker.internal:host-gateway"
    environment:
      EXT_WAF_URL: http://host.docker.internal:4100/__checkin/ext
      EXT_WAF_TIMEOUT_MS: "240000"
```

When adding the site, **`platform` must be `anyrouter`** (use it for agentrouter too — the adapter
tells them apart by URL). `new-api` falls back to the plain-HTTP path and the symptoms above return.

### 3. Do not raise the scheduler frequency

> [!WARNING]
> Every call launches a real browser. Measured on 2026-09-02: upstream's defaults (hourly balance
> refresh + check-in every 6 h) produced about 60 requests/day from one egress IP, and ESA
> blacklisted it — the **whole site**, home page included, returned 403. This fork defaults to
> balance twice a day and check-in every 12 h (about 8 requests/day). The check-in reward is paid
> once per day no matter how often you poll.

## Architecture

```
your client (OpenAI / Claude compatible)
        │  one API key
        ▼
┌───────────────────────────────────────────────┐
│  Banyan (dashboard / connections / scheduler / │
│          routing / logs)                      │
│  NewApiAdapter ──> New API / One API / ...    │
│  AnyRouterAdapter ──HTTP RPC──┐               │
└───────────────────────────────┼───────────────┘
                                ▼
            checkin-backend (real browser + xvfb + proxy)
                 127.0.0.1:4100/__checkin/ext/*
                 └─ clears the JS challenge, then calls the site API
```

The backend is deliberately stateless: scheduling, connection management, check-in logs and
notifications stay in Banyan, so nothing is logged or pushed twice.

## Differences from upstream metapi

| Change | Why |
|---|---|
| The 5 methods in `anyrouter.ts` forward to the browser backend | Passing the new ESA challenge requires JS; `getBalance` no longer swallows errors, which used to write 0 as a real balance |
| `platformUserId` is no longer guessed from the username suffix | E-mail style usernames have no suffix → 401 "New-Api-User missing" → balance stuck at 0 |
| Auto re-login writes the new ID back | Only `accessToken` was written back, so the next call hit 401 again |
| Related timeouts widened from 10 s / 30 s to 240 s | A real browser needs 12–90 s per challenge |
| Calmer scheduler defaults | See the ban measurement above |
| Removed the stale `pnpm-lock.yaml` | It made Dependabot's npm job fail every run, so security PRs never arrived |

Upstream traces that are **kept on purpose**: localStorage keys, backup file names
(`metapi-backup-*.json`), the Docker service name and other internal identifiers still say `metapi`.
Renaming them would break existing configs and backups and make every upstream rebase conflict.
Full technical notes live in [`LOCAL_FORK.md`](./LOCAL_FORK.md).

## Upgrading

This fork is maintained on the `local-fork` branch:

```bash
git pull && docker compose -f docker/docker-compose.yml up -d --build
```

The dashboard's update center only checks this repository's Releases (override with
`UPDATE_CENTER_GITHUB_REPO` / `UPDATE_CENTER_DOCKER_REPO`) and no longer reports upstream versions.
The k3s one-click update needs an image you build and push yourself — **never the upstream image**
`1467078763/metapi`, which would overwrite this fork's changes.

## FAQ

**My egress IP got blacklisted (whole site 403, `via` header contains `403102`).**
Switch egress nodes. It is an IP-level block, not a fingerprint issue: at the same moment a home
broadband line could fetch the challenge page while both of the server's egresses got a hard 403.
After recovering, do not raise the scheduler frequency.

**Why must the browser backend run inside the anyrouter-check-in directory?**
It reuses that project's login and cookie logic, so the files must run inside its clone.
Deployment paths can be overridden with `CHECKIN_DIR` / `METAPI_DIR` / `MIHOMO_DIR`.

**Does the docs site describe upstream features?**
Yes. The [docs site](https://jacky1n7.github.io/banyan/) is inherited from upstream and covers what
both share; the anyrouter-specific parts of this fork live only in this README and `LOCAL_FORK.md`.

**Can I still use it as a plain metapi?**
Yes. Non-anyrouter sites behave exactly like upstream; fill in `platform` as you would there.

## License and credits

[MIT](./LICENSE), same as upstream. Forked from [cita-777/metapi](https://github.com/cita-777/metapi);
the browser backend depends on [millylee/anyrouter-check-in](https://github.com/millylee/anyrouter-check-in)
and [CloakBrowser](https://pypi.org/project/cloakbrowser/). Thanks to the upstream author and all contributors.

<div align="center">
<sub>Check the terms of service of the sites you target. Automated check-in and anti-bot bypass may violate them; use at your own risk.</sub>
</div>
