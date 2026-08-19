#!/usr/bin/env bash
# Telegram 管理机器人。
# 需要 xvfb：/add 的 session 模式会调 import_session.py，那个走有头 Chromium。
set -uo pipefail
cd /opt/anyrouter-check-in
export PATH="/root/.local/bin:$PATH"
export PYTHONUNBUFFERED=1
export PYTHONIOENCODING=utf-8
exec uv run --with cryptography python telegram_bot.py
