#!/usr/bin/env bash
# 用一段 session cookie 导入账号（命令行入口，面板里也有同样功能）。
# 用法: ./import_session.sh <siteId> [api_user]
#   session 从标准输入读，不走命令行参数，避免出现在 ps / history 里。
set -uo pipefail
cd /opt/anyrouter-check-in
export PATH="/root/.local/bin:$PATH"
export PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8
SITE_ID="${1:?用法: ./import_session.sh <siteId> [api_user]}"
API_USER="${2:-}"
TMP=$(mktemp); chmod 600 "$TMP"
trap "rm -f \"$TMP\"" EXIT
printf "在浏览器里登录该站点，F12 → Application → Cookies 复制 session 的值\n"
read -r -s -p "粘贴 session（输入不显示）: " SESS; echo
printf "%s" "$SESS" > "$TMP"
ARGS=(--site-id "$SITE_ID" --session-file "$TMP")
[ -n "$API_USER" ] && ARGS+=(--api-user "$API_USER")
# 有头 Chromium 需要 X 显示：新版阿里云 WAF 挑战必须执行 JS
exec xvfb-run -a --server-args="-screen 0 1440x900x24" \
  uv run --with cryptography python import_session.py "${ARGS[@]}"
