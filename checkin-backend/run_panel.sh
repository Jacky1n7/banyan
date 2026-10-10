#!/usr/bin/env bash
# 签到运维后端（metapi 的浏览器能力提供者）。
# 必须套 xvfb：login 走有头 Chromium（CHECKIN_HEADLESS=false，反检测优于无头），
# 没有 X 显示会报 "browser has been closed"。
# 不直接写进 systemd 的 ExecStart —— 它不做 shell 解析，
# --server-args="-screen 0 ..." 里的空格会被拆成多个参数。
set -uo pipefail
# 本脚本所在目录即上游 clone 目录；换目录部署时用 CHECKIN_DIR 覆盖
cd "${CHECKIN_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
# uv 官方安装器默认装到 ~/.local/bin，而 systemd 的最小 PATH 不含它
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) PATH="$HOME/.local/bin:$PATH" ;; esac
export PATH
export PYTHONUNBUFFERED=1
export PYTHONIOENCODING=utf-8
export PANEL_PORT="${PANEL_PORT:-4100}"
exec xvfb-run -a --server-args="-screen 0 1920x1080x24" \
  "${UV_BIN:-uv}" run --with cryptography python panel.py
