#!/usr/bin/env bash
# Telegram 管理机器人。
# 需要 xvfb：/add 的 session 模式会调 import_session.py，那个走有头 Chromium。
set -uo pipefail
# 本脚本所在目录即上游 clone 目录；换目录部署时用 CHECKIN_DIR 覆盖
cd "${CHECKIN_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
# uv 官方安装器默认装到 ~/.local/bin，而 systemd 的最小 PATH 不含它
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) PATH="$HOME/.local/bin:$PATH" ;; esac
export PATH
export PYTHONUNBUFFERED=1
export PYTHONIOENCODING=utf-8
exec "${UV_BIN:-uv}" run --with cryptography python telegram_bot.py
