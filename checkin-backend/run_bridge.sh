#!/usr/bin/env bash
# metapi 桥接签到入口（主）。
#   账号信息 ← metapi（唯一维护入口，面板增删改）
#   签到执行 ← CloakBrowser 真浏览器（metapi 自己的 node:vm WAF 求解实测间歇失效）
#   结果回写 → metapi（accessToken / checkin_logs / 余额刷新）
# 独立 fallback 见 run.sh（用 .env 快照，不依赖 metapi）。
set -uo pipefail
# 本脚本所在目录即上游 clone 目录；换目录部署时用 CHECKIN_DIR 覆盖
cd "${CHECKIN_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
# uv 官方安装器默认装到 ~/.local/bin，而 systemd 的最小 PATH 不含它
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) PATH="$HOME/.local/bin:$PATH" ;; esac
export PATH
export PYTHONUNBUFFERED=1
export PYTHONIOENCODING=utf-8
# cryptography 用于解密 metapi 存的密码。它不在 uv.lock 里：不能 uv add（会改上游
# pyproject.toml），也不能 uv pip install（uv sync --frozen 会清掉），故用 --with 叠加。
exec xvfb-run -a --server-args="-screen 0 1920x1080x24" \
  "${UV_BIN:-uv}" run --with cryptography python metapi_bridge.py
