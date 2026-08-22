#!/usr/bin/env python3
"""Telegram 管理机器人：推送 + 命令交互。

为什么单独做一个进程
--------------------
metapi 自带的通知只在**失败**时发（checkinService 里只有 error/warning 两处
sendNotification），签到成功一声不响；而且它没有任何接收侧，没法在 Telegram 上
反过来操作。所以这里接管全部 Telegram 交互：

  * 推送：轮询 checkin_logs（签到结果）+ events（其余事件），格式化后发出。
    比让 metapi 自己发的好处是格式集中在一处，且能顺手把账号余额、额度条这些
    events 里没有的信息一起查出来拼进消息。
  * 命令：长轮询 getUpdates，把操作转成 metapi 的 REST 调用。

因此 metapi 的 TELEGRAM_ENABLED 应当关掉，否则一条事件会被推两次。

安全
----
  * 只响应白名单 chat_id，其它一律忽略（不回复，避免被探测出这是个 bot）
  * 含密码/session 的用户消息处理完立即 deleteMessage，不留在聊天记录里
  * 删除账号走 inline keyboard 二次确认
"""

from __future__ import annotations

import html
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CHECKIN_DIR = Path('/opt/anyrouter-check-in')
sys.path.insert(0, str(CHECKIN_DIR))
os.chdir(CHECKIN_DIR)

import tg_fmt as F  # noqa: E402

METAPI_ENV = Path('/opt/metapi/.env')
METAPI_DB = Path('/opt/metapi/data/hub.db')
METAPI_BASE = 'http://127.0.0.1:4000'
MIHOMO_API = 'http://127.0.0.1:9090'
STATE_FILE = Path('/opt/anyrouter-check-in/.tg_bot_state.json')


def read_env(path: Path, key: str) -> str | None:
	if not path.exists():
		return None
	for line in path.read_text(encoding='utf-8').splitlines():
		if line.startswith(key + '='):
			return line.split('=', 1)[1].strip()
	return None


TOKEN = read_env(METAPI_ENV, 'TELEGRAM_BOT_TOKEN') or ''
OWNER = (read_env(METAPI_ENV, 'TELEGRAM_CHAT_ID') or '').strip()
AUTH = read_env(METAPI_ENV, 'AUTH_TOKEN') or ''
TG = f'https://api.telegram.org/bot{TOKEN}'

SESSIONS: dict[str, dict] = {}   # chat_id -> 多步交互状态
_lock = threading.Lock()


# ------------------------------------------------------------------ 底层


def tg(method: str, **params):
	data = json.dumps(params).encode()
	req = urllib.request.Request(f'{TG}/{method}', data=data,
								 headers={'Content-Type': 'application/json'})
	try:
		with urllib.request.urlopen(req, timeout=70) as r:
			return json.loads(r.read().decode())
	except urllib.error.HTTPError as exc:
		try:
			return json.loads(exc.read().decode())
		except Exception:
			return {'ok': False, 'description': f'HTTP {exc.code}'}
	except Exception as exc:
		return {'ok': False, 'description': str(exc)}


def send(chat_id, text: str, keyboard: list | None = None, silent: bool = False):
	params = {'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML',
			  'disable_web_page_preview': True, 'disable_notification': silent}
	if keyboard:
		params['reply_markup'] = {'inline_keyboard': keyboard}
	return tg('sendMessage', **params)


def send_checked(chat_id, text: str, **kw) -> bool:
	"""发送并检查结果。返回 False 表示这条该留到下轮重发。

	tg() 在任何失败下都只返回 {'ok': False, 'description': ...}，而 send() 的
	调用方从不看返回值 —— 于是发送失败时游标照样推进，消息永久丢失且不留痕迹。
	静默漏推比误报更难发现：看起来一切正常，只是什么都没收到。

	400 是内容本身的问题（HTML 解析失败之类），重试多少次都一样，所以记下日志后
	放行游标；否则整条队列会永久卡在这一条上，后面的全部推不出去。
	"""
	res = send(chat_id, text, **kw) or {}
	if res.get('ok'):
		return True
	code, desc = res.get('error_code'), str(res.get('description') or '')
	if code == 429:
		wait = int((res.get('parameters') or {}).get('retry_after') or 5)
		print(f'[PUSH] 被限流，等待 {wait}s 后重试', flush=True)
		time.sleep(min(wait, 60))
		return False
	permanent = code == 400
	print(f'[PUSH] 发送失败（{"内容问题，跳过该条" if permanent else "将重试"}）: '
		  f'{code} {desc[:140]}', flush=True)
	return permanent


def edit(chat_id, message_id, text: str, keyboard: list | None = None):
	params = {'chat_id': chat_id, 'message_id': message_id, 'text': text,
			  'parse_mode': 'HTML', 'disable_web_page_preview': True}
	if keyboard is not None:
		params['reply_markup'] = {'inline_keyboard': keyboard}
	return tg('editMessageText', **params)


def metapi(path: str, payload=None, method=None, timeout=300):
	data = json.dumps(payload).encode() if payload is not None else None
	req = urllib.request.Request(
		METAPI_BASE + path, data=data,
		method=method or ('POST' if data is not None else 'GET'),
		headers={'Authorization': f'Bearer {AUTH}', 'Content-Type': 'application/json'})
	try:
		with urllib.request.urlopen(req, timeout=timeout) as r:
			body = r.read().decode()
			return r.status, (json.loads(body) if body.strip() else {})
	except urllib.error.HTTPError as exc:
		try:
			return exc.code, json.loads(exc.read().decode())
		except Exception:
			return exc.code, {}
	except Exception as exc:
		return 0, {'error': str(exc)}


def db_rows(sql: str, args=()) -> list[dict]:
	conn = sqlite3.connect(f'file:{METAPI_DB}?mode=ro', uri=True, timeout=20)
	try:
		conn.row_factory = sqlite3.Row
		return [dict(r) for r in conn.execute(sql, args)]
	finally:
		conn.close()


def load_state() -> dict:
	try:
		return json.loads(STATE_FILE.read_text(encoding='utf-8'))
	except Exception:
		return {}


def save_state(st: dict) -> None:
	try:
		STATE_FILE.write_text(json.dumps(st), encoding='utf-8')
		STATE_FILE.chmod(0o600)
	except Exception:
		pass


# ------------------------------------------------------------------ 数据聚合


def last_success_map() -> dict[int, str]:
	"""每个账号最近一次**成功**签到的时间，取自 checkin_logs。

	不能用 accounts.last_checkin_at 当判据：metapi 只在 directCheckinSuccess
	（真拿到奖励）时才推进它，而 interval 调度下「今日已签到」不推进
	（checkinService.ts 的 shouldAdvanceLastCheckinAt）。agentrouter 的
	sign_in_path 是 None，查用户信息即完成签到，返回的 message 总是命中
	「今日已签到」—— 于是它的 last_checkin_at 长期为 None，拿它判灯会一直误报黄灯。
	"""
	out: dict[int, str] = {}
	for row in db_rows(
			'SELECT account_id, MAX(created_at) AS last FROM checkin_logs '
			"WHERE status = 'success' GROUP BY account_id"):
		if row.get('last'):
			out[row['account_id']] = row['last']
	return out


def accounts_data() -> list[dict]:
	succ = last_success_map()
	out = []
	for row in db_rows(
			'SELECT a.id, a.username, a.status, a.balance, a.balance_used, '
			'a.checkin_enabled, a.last_checkin_at, a.extra_config, a.access_token, '
			'a.site_id, s.name AS site_name, s.url, s.platform '
			'FROM accounts a JOIN sites s ON a.site_id = s.id ORDER BY a.id'):
		low = str(row['url'] or '').lower()
		if 'anyrouter' not in low and 'agentrouter' not in low:
			continue
		try:
			extra = json.loads(row['extra_config'] or '{}')
		except Exception:
			extra = {}
		out.append({
			'id': row['id'], 'username': row['username'], 'site': row['site_name'],
			'site_id': row['site_id'], 'status': row['status'],
			'balance': row['balance'] or 0, 'used': row['balance_used'] or 0,
			'checkin_enabled': row['checkin_enabled'],
			# 展示与判灯都用真实成功记录；metapi 自己的 last_checkin_at 另存一份备查
			'last_checkin': succ.get(row['id']) or row['last_checkin_at'],
			'metapi_last_checkin': row['last_checkin_at'],
			'api_user': extra.get('platformUserId'),
			'has_password': bool((extra.get('autoRelogin') or {}).get('passwordCipher')),
			'has_session': 'session=' in (row['access_token'] or ''),
		})
	return out


def sites_data() -> list[dict]:
	return [r for r in db_rows('SELECT id, name, url, platform FROM sites ORDER BY id')
			if any(k in str(r['url']).lower() for k in ('anyrouter', 'agentrouter'))]


def proxy_data() -> dict:
	try:
		with urllib.request.urlopen(f'{MIHOMO_API}/proxies/PROXY', timeout=10) as r:
			info = json.load(r)
	except Exception as exc:
		return {'ok': False, 'node': f'读不到 mihomo（{type(exc).__name__}）', 'all': []}
	ip = ''
	try:
		op = urllib.request.build_opener(urllib.request.ProxyHandler(
			{'http': 'http://127.0.0.1:7890', 'https': 'http://127.0.0.1:7890'}))
		ip = op.open('https://api.ipify.org', timeout=20).read().decode().strip()
	except Exception:
		pass
	return {'ok': bool(ip), 'node': info.get('now'), 'ip': ip,
			'all': info.get('all') or [], 'type': info.get('type')}


def services_data() -> dict:
	out = {}
	for label, unit in (('metapi', None), ('浏览器后端', 'checkin-panel'),
						('代理 mihomo', 'mihomo'), ('nginx', 'nginx')):
		if unit is None:
			try:
				st = subprocess.run(['docker', 'inspect', 'metapi', '-f', '{{.State.Status}}'],
									capture_output=True, text=True, timeout=15).stdout.strip()
				out[label] = {'ok': st == 'running', 'detail': st or '未知'}
			except Exception:
				out[label] = {'ok': False, 'detail': '查询失败'}
		else:
			try:
				st = subprocess.run(['systemctl', 'is-active', unit],
									capture_output=True, text=True, timeout=15).stdout.strip()
				out[label] = {'ok': st == 'active', 'detail': st}
			except Exception:
				out[label] = {'ok': False, 'detail': '查询失败'}
	return out


def schedule_data() -> dict:
	out = {'checkin': '—', 'balance': '—'}
	try:
		logs = subprocess.run(['docker', 'logs', 'metapi', '--tail', '400'],
							  capture_output=True, text=True, timeout=20)
		for line in (logs.stdout + logs.stderr).splitlines():
			if 'Check-in schedule' in line:
				out['checkin'] = line.split(':', 1)[-1].strip()
			elif 'Balance refresh cron' in line:
				out['balance'] = line.split(':', 1)[-1].strip()
	except Exception:
		pass
	return out


def cooldown_data() -> dict:
	"""登录退避状态。

	这是独立签到方案（metapi_bridge.py + anyrouter-checkin.timer）的机制。
	签到交给 metapi 后那套已停用，文件会变成不再被更新也不被清除的遗物 ——
	所以两道检查缺一不可，否则会长期误报「退避中」：

	1. timer 没启用就不看这个文件（机制本身没在运行）
	2. 即使在运行，也要按 1h/2h/4h… 算出是否**仍未到期**，
	   而不是只看 failures > 0
	"""
	try:
		active = subprocess.run(['systemctl', 'is-enabled', 'anyrouter-checkin.timer'],
								capture_output=True, text=True, timeout=10).stdout.strip()
		if active != 'enabled':
			return {}
	except Exception:
		return {}

	f = CHECKIN_DIR / '.login_cooldown.json'
	if not f.exists():
		return {}
	try:
		raw = json.loads(f.read_text(encoding='utf-8'))
	except Exception:
		return {}
	out = {}
	now = time.time()
	for prov, entry in (raw or {}).items():
		if not isinstance(entry, dict):
			continue
		fails = int(entry.get('failures', 0))
		if fails <= 0:
			continue
		wait_h = min(2 ** (fails - 1), 12)
		elapsed_h = (now - float(entry.get('ts', 0))) / 3600
		if elapsed_h < wait_h:
			out[prov] = f'连续 {fails} 次失败，退避 {wait_h}h，还剩 {wait_h - elapsed_h:.1f}h'
	return out


def status_payload() -> dict:
	return {'services': services_data(), 'accounts': accounts_data(),
			'proxy': proxy_data(), 'schedule': schedule_data(),
			'cooldown': cooldown_data()}


# ------------------------------------------------------------------ 发送辅助


TG_LIMIT = 3900  # Telegram 上限 4096，留余量给 HTML 标签


def send_long(chat_id, text: str, keyboard=None):
	"""超长消息按行切片，避免整条被 Telegram 拒掉。"""
	if len(text) <= TG_LIMIT:
		return send(chat_id, text, keyboard)
	chunk, size = [], 0
	for line in text.split('\n'):
		if size + len(line) + 1 > TG_LIMIT and chunk:
			send(chat_id, '\n'.join(chunk))
			chunk, size = [], 0
		chunk.append(line)
		size += len(line) + 1
	if chunk:
		return send(chat_id, '\n'.join(chunk), keyboard)


def drop(chat_id, message_id):
	"""删除含敏感内容的用户消息。"""
	tg('deleteMessage', chat_id=chat_id, message_id=message_id)


# ------------------------------------------------------------------ 命令


def cmd_status(chat_id, args, msg):
	send(chat_id, '⏳ 正在收集状态…', silent=True)
	send_long(chat_id, F.fmt_status(status_payload()))


def cmd_accounts(chat_id, args, msg):
	send_long(chat_id, F.fmt_accounts(accounts_data()))


def cmd_logs(chat_id, args, msg):
	n = 8
	if args and args[0].isdigit():
		n = max(1, min(25, int(args[0])))
	logs = db_rows('SELECT account_id, status, message, reward, created_at '
				   'FROM checkin_logs ORDER BY id DESC LIMIT ?', (n,))
	amap = {a['id']: a for a in accounts_data()}
	send_long(chat_id, F.fmt_logs(logs, amap))


def cmd_sites(chat_id, args, msg):
	send_long(chat_id, F.fmt_sites(sites_data()))


def cmd_services(chat_id, args, msg):
	svc = services_data()
	lines = ['<b>🖥 服务状态</b>', '']
	for name, info in svc.items():
		lines.append(f'{"🟢" if info.get("ok") else "🔴"} {F.esc(name)} — {F.esc(info.get("detail"))}')
	try:
		mem = subprocess.run(['free', '-h'], capture_output=True, text=True, timeout=10).stdout.splitlines()
		if len(mem) > 1:
			p = mem[1].split()
			lines += ['', f'💾 内存 {F.esc(p[2])} / {F.esc(p[1])}']
		disk = subprocess.run(['df', '-h', '/'], capture_output=True, text=True, timeout=10).stdout.splitlines()
		if len(disk) > 1:
			p = disk[1].split()
			lines.append(f'💿 磁盘 {F.esc(p[2])} / {F.esc(p[1])}（{F.esc(p[4])}）')
	except Exception:
		pass
	send(chat_id, '\n'.join(lines))


def cmd_proxy(chat_id, args, msg):
	info = proxy_data()
	nodes = info.get('all') or []
	if args and args[0].isdigit():
		idx = int(args[0]) - 1
		if not (0 <= idx < len(nodes)):
			send(chat_id, f'⚠️ 序号超出范围（1-{len(nodes)}）')
			return
		target = nodes[idx]
		req = urllib.request.Request(f'{MIHOMO_API}/proxies/PROXY',
									 data=json.dumps({'name': target}).encode(),
									 method='PUT', headers={'Content-Type': 'application/json'})
		try:
			urllib.request.urlopen(req, timeout=15)
		except Exception as exc:
			send(chat_id, f'🔴 切换失败：{F.esc(exc)}')
			return
		time.sleep(3)
		after = proxy_data()
		send(chat_id, f'✅ 已切换\n\n🔌 <b>{F.esc(after.get("node"))}</b>\n'
					  f'出口 <code>{F.esc(after.get("ip") or "获取中")}</code>\n\n'
					  f'⚠️ 换节点会让已登录 session 失效（WAF cookie 绑出口 IP），'
					  f'下轮签到可能触发一次自动重登。')
		return

	lines = ['<b>🔌 代理节点</b>', '',
			 f'{"🟢" if info.get("ok") else "🔴"} 当前 <b>{F.esc(info.get("node"))}</b>',
			 f'出口 <code>{F.esc(info.get("ip") or "—")}</code>',
			 f'模式 <code>{F.esc(info.get("type"))}</code>（固定，不自动漂移）', '']
	for i, n in enumerate(nodes, 1):
		mark = '▶️' if n == info.get('node') else '  '
		lines.append(f'{mark} <b>{i}</b>. {F.esc(n)}')
	lines += ['', '切换：/proxy &lt;序号&gt;']
	send(chat_id, '\n'.join(lines))


def cmd_checkin(chat_id, args, msg):
	if args and args[0].isdigit():
		aid = int(args[0])
		acct = next((a for a in accounts_data() if a['id'] == aid), None)
		if not acct:
			send(chat_id, f'⚠️ 找不到账号 #{aid}，用 /accounts 查看')
			return
		send(chat_id, f'🚀 正在签到 <code>{F.esc(F.mask_user(acct["username"]))}</code>…\n'
					  f'走真浏览器过 WAF，约 15-90 秒')
		st, out = metapi(f'/api/checkin/trigger/{aid}', {})
		ok = out.get('success')
		bal = out.get('balance') or {}
		lines = [f'{"✅ 签到成功" if ok else "🔴 签到失败"}', '',
				 f'👤 <code>{F.esc(F.mask_user(acct["username"]))}</code>',
				 f'📝 {F.esc(str(out.get("message"))[:120])}']
		if bal:
			lines.append(f'💰 余额 <b>{F.money(bal.get("balance"))}</b>  已用 {F.money(bal.get("used"))}')
		send(chat_id, '\n'.join(lines))
		return

	accts = [a for a in accounts_data() if a.get('checkin_enabled') and a.get('status') == 'active']
	send(chat_id, f'🚀 正在为 {len(accts)} 个账号签到…\n每个约 15-90 秒，请耐心')
	st, out = metapi('/api/checkin/trigger', {}, timeout=900)
	items = out.get('results') or out.get('items') or []
	if not items:
		send(chat_id, f'📋 已触发（HTTP {st}）\n\n{F.esc(json.dumps(out, ensure_ascii=False)[:600])}')
		return
	amap = {a['id']: a for a in accounts_data()}
	lines = ['<b>🚀 批量签到结果</b>', '']
	okn = 0
	for it in items:
		# 兼容两种可能的返回结构
		r = it.get('result') or it
		succ = r.get('success') if isinstance(r, dict) else False
		okn += 1 if succ else 0
		a = amap.get(it.get('accountId')) or {}
		lines.append(f'{"✅" if succ else "🔴"} <code>{F.esc(F.mask_user(a.get("username") or it.get("username")))}</code>')
		lines.append(f'    {F.esc(str((r or {}).get("message"))[:70])}')
	lines.insert(2, f'成功 {okn}/{len(items)}')
	lines.insert(3, '')
	send_long(chat_id, '\n'.join(lines))


def cmd_refresh(chat_id, args, msg):
	send(chat_id, '🔄 正在刷新余额（走真浏览器，每账号约 15 秒）…')
	st, _ = metapi('/api/accounts?refresh=1', timeout=600)
	accts = accounts_data()
	total = sum(a['balance'] for a in accts)
	lines = [f'{"✅" if st == 200 else "🔴"} 刷新完成（HTTP {st}）', '',
			 f'<b>💰 总余额 {F.money(total)}</b>', '']
	for a in accts:
		lines.append(f'{F.account_lamp(a)} <code>{F.esc(F.mask_user(a["username"]))}</code> '
					 f'{F.money(a["balance"])}')
	send(chat_id, '\n'.join(lines))


def cmd_toggle(enable: bool):
	def handler(chat_id, args, msg):
		if not args or not args[0].isdigit():
			send(chat_id, f'用法：/{"enable" if enable else "disable"} &lt;账号id&gt;')
			return
		aid = int(args[0])
		st, _ = metapi(f'/api/accounts/{aid}', {'checkinEnabled': enable}, method='PUT')
		metapi('/api/accounts?refresh=0')
		send(chat_id, f'{"✅ 已启用" if enable else "⚪ 已禁用"}账号 #{aid} 的签到（HTTP {st}）')
	return handler


def cmd_schedule(chat_id, args, msg):
	if not args:
		send(chat_id, f'<b>⏰ 当前调度</b>\n\n{F.esc(json.dumps(schedule_data(), ensure_ascii=False))}\n\n'
					  f'设置：/schedule &lt;小时&gt;，例如 /schedule 6')
		return
	if not args[0].isdigit():
		send(chat_id, '用法：/schedule &lt;小时&gt;（整数）')
		return
	hours = max(1, min(24, int(args[0])))
	st, out = metapi('/api/checkin/schedule', {'mode': 'interval', 'intervalHours': hours}, method='PUT')
	send(chat_id, f'{"✅" if st == 200 else "🔴"} 签到间隔已设为 <b>{hours} 小时</b>（HTTP {st}）\n\n'
				  f'💡 这些站是滚动 24h 才能签一次，6 小时一轮是为了容错，'
				  f'不会因为跑得勤就多拿额度。')


def cmd_del(chat_id, args, msg):
	if not args or not args[0].isdigit():
		send(chat_id, '用法：/del &lt;账号id&gt;')
		return
	aid = int(args[0])
	acct = next((a for a in accounts_data() if a['id'] == aid), None)
	if not acct:
		send(chat_id, f'⚠️ 找不到账号 #{aid}')
		return
	send(chat_id,
		 f'<b>🗑 确认删除？</b>\n\n'
		 f'👤 <code>{F.esc(F.mask_user(acct["username"]))}</code>\n'
		 f'🌐 {F.esc(acct["site"])}\n'
		 f'💰 {F.money(acct["balance"])}\n\n'
		 f'⚠️ 删除后需重新添加（有密码的可自动登录，仅 session 的要重新导入）',
		 keyboard=[[{'text': '🗑 确认删除', 'callback_data': f'del:yes:{aid}'},
					{'text': '取消', 'callback_data': 'del:no:0'}]])


def cmd_add(chat_id, args, msg):
	sites = sites_data()
	if not sites:
		send(chat_id, '⚠️ 没有可用站点')
		return
	rows = [[{'text': f'{s["name"]}（{s["url"].replace("https://", "")}）',
			  'callback_data': f'add:site:{s["id"]}'}] for s in sites]
	rows.append([{'text': '取消', 'callback_data': 'add:cancel:0'}])
	send(chat_id, '<b>➕ 添加账号</b>\n\n第 1 步：选择站点', keyboard=rows)


def cmd_help(chat_id, args, msg):
	send(chat_id, F.HELP)


COMMANDS = {
	'start': cmd_help, 'help': cmd_help,
	'status': cmd_status, 'accounts': cmd_accounts, 'logs': cmd_logs,
	'sites': cmd_sites, 'services': cmd_services, 'proxy': cmd_proxy,
	'checkin': cmd_checkin, 'refresh': cmd_refresh,
	'enable': cmd_toggle(True), 'disable': cmd_toggle(False),
	'schedule': cmd_schedule, 'del': cmd_del, 'add': cmd_add,
}


# ------------------------------------------------------------------ 交互回调


def on_callback(cb: dict):
	chat_id = str(((cb.get('message') or {}).get('chat') or {}).get('id') or '')
	if chat_id != OWNER:
		return
	data = cb.get('data') or ''
	mid = (cb.get('message') or {}).get('message_id')
	tg('answerCallbackQuery', callback_query_id=cb.get('id'))
	parts = data.split(':')
	if len(parts) < 3:
		return
	kind, action, value = parts[0], parts[1], parts[2]

	if kind == 'del':
		if action != 'yes':
			edit(chat_id, mid, '已取消。', keyboard=[])
			return
		aid = int(value)
		st, _ = metapi(f'/api/accounts/{aid}', method='DELETE')
		metapi('/api/accounts?refresh=1')
		edit(chat_id, mid, f'{"🗑 已删除" if st == 200 else "🔴 删除失败"}账号 #{aid}（HTTP {st}）',
			 keyboard=[])
		return

	if kind == 'add':
		if action == 'cancel':
			with _lock:
				SESSIONS.pop(chat_id, None)
			edit(chat_id, mid, '已取消。', keyboard=[])
			return
		if action == 'site':
			site = next((s for s in sites_data() if str(s['id']) == value), None)
			if not site:
				edit(chat_id, mid, '⚠️ 站点不存在', keyboard=[])
				return
			with _lock:
				SESSIONS[chat_id] = {'stage': 'mode', 'site': site}
			edit(chat_id, mid,
				 f'<b>➕ 添加账号</b>\n\n🌐 站点：{F.esc(site["name"])}\n\n第 2 步：选择凭据方式',
				 keyboard=[
					 [{'text': '🔑 邮箱 + 密码（推荐，可自动续期）',
					   'callback_data': 'add:mode:password'}],
					 [{'text': '🍪 session cookie（OAuth 账号用）',
					   'callback_data': 'add:mode:session'}],
					 [{'text': '取消', 'callback_data': 'add:cancel:0'}]])
			return
		if action == 'mode':
			with _lock:
				sess = SESSIONS.get(chat_id)
			if not sess:
				edit(chat_id, mid, '会话已过期，重新 /add', keyboard=[])
				return
			with _lock:
				sess['stage'] = 'await_cred'
				sess['mode'] = value
			if value == 'password':
				tip = ('第 3 步：发一条消息，格式为\n\n'
					   '<code>邮箱 密码</code>\n\n'
					   '（中间一个空格。收到后我会立刻删除那条消息，'
					   '密码不会留在聊天记录里）')
			else:
				tip = ('第 3 步：发一条消息，格式为\n\n'
					   '<code>session值 api_user</code>\n\n'
					   'session 取自浏览器 F12 → Application → Cookies；\n'
					   'api_user 是站点用户 ID（<b>必填</b>，这些站不带它一律 401）。\n\n'
					   '（收到后我会立刻删除那条消息）')
			edit(chat_id, mid, f'<b>➕ 添加账号</b>\n\n🌐 {F.esc(sess["site"]["name"])}\n'
								f'🔑 {"邮箱+密码" if value == "password" else "session"}\n\n{tip}',
				 keyboard=[[{'text': '取消', 'callback_data': 'add:cancel:0'}]])
			return


def handle_cred_input(chat_id, text: str, msg: dict) -> bool:
	"""处理 /add 第 3 步的凭据输入。返回 True 表示已消费这条消息。"""
	with _lock:
		sess = SESSIONS.get(chat_id)
	if not sess or sess.get('stage') != 'await_cred':
		return False

	# 先删除用户消息，再做耗时操作 —— 否则密码会在聊天记录里停留一两分钟
	drop(chat_id, msg.get('message_id'))
	with _lock:
		SESSIONS.pop(chat_id, None)

	site = sess['site']
	parts = text.split()
	if sess['mode'] == 'password':
		if len(parts) < 2:
			send(chat_id, '⚠️ 格式应为 <code>邮箱 密码</code>，重新 /add')
			return True
		username, password = parts[0], ' '.join(parts[1:])
		send(chat_id, f'⏳ 正在用真浏览器登录 {F.esc(site["name"])}…\n约 30-120 秒')
		st, out = metapi('/api/accounts/login',
						 {'siteId': site['id'], 'username': username, 'password': password},
						 timeout=600)
		if out.get('success'):
			acct = out.get('account') or {}
			aid = acct.get('id')
			after = next((a for a in accounts_data() if a['id'] == aid), None)
			lines = ['✅ <b>添加成功</b>', '',
					 f'👤 <code>{F.esc(F.mask_user(username))}</code>',
					 f'🌐 {F.esc(site["name"])}  ID <b>#{aid}</b>']
			if after:
				lines.append(f'🆔 api_user {after.get("api_user") or "—"}')
				lines.append(f'💰 {F.money(after.get("balance"))}')
				lines.append(f'🔑 {F.cred_badge(after)}')
			send(chat_id, '\n'.join(lines))
		else:
			send(chat_id, f'🔴 <b>添加失败</b>\n\n{F.esc(str(out.get("message"))[:300])}')
		return True

	# session 模式
	if not parts:
		send(chat_id, '⚠️ 格式应为 <code>session值 api_user</code>，重新 /add')
		return True
	session_value = parts[0]
	api_user = parts[1] if len(parts) > 1 else None
	if not api_user:
		send(chat_id, '⚠️ 这些站必须提供 api_user（站点用户 ID），否则接口一律 401。重新 /add')
		return True
	send(chat_id, f'⏳ 正在用真浏览器验证 session…\n约 30-90 秒')
	tmp = Path('/tmp/.tgbot_session_input.txt')
	tmp.write_text(session_value, encoding='utf-8')
	tmp.chmod(0o600)
	try:
		proc = subprocess.run(
			['xvfb-run', '-a', '--server-args=-screen 0 1440x900x24',
			 '/root/.local/bin/uv', 'run', '--with', 'cryptography', 'python',
			 'import_session.py', '--site-id', str(site['id']),
			 '--session-file', str(tmp), '--api-user', str(api_user)],
			cwd=str(CHECKIN_DIR), capture_output=True, text=True, timeout=600,
			env={**os.environ, 'PATH': '/root/.local/bin:' + os.environ.get('PATH', '')})
		tail = [l for l in (proc.stdout + proc.stderr).splitlines()
				if l.startswith(('[SUCCESS]', '[DONE]', '[FAILED]', '[WARN]', '[HINT]'))]
	except Exception as exc:
		tail = [f'[FAILED] {type(exc).__name__}: {exc}']
	finally:
		tmp.unlink(missing_ok=True)
	ok = any(l.startswith('[DONE]') for l in tail)
	send(chat_id, f'{"✅ <b>添加成功</b>" if ok else "🔴 <b>添加失败</b>"}\n\n'
				  + '\n'.join(F.esc(l)[:200] for l in tail[-6:]))
	return True


# ------------------------------------------------------------------ 推送循环

def checkin_log_events(last_id: int) -> list[dict]:
	"""把 checkin_logs 的新记录转成 events 的形状，复用同一套格式化。

	为什么不能用 events 表当签到源：metapi 的 checkinAll() 对每个账号都传
	skipEvent=true（checkinService.ts:357），而它自己不写汇总事件 —— 于是
	**定时调度的签到一条 event 都不产生**。events 里那些 type=status 的
	「全部账号签到已完成」只有在面板上手动点批量签到时，才由
	backgroundTaskService 写。checkin_logs 才是无论走哪条路径都会写的权威记录。
	"""
	rows = db_rows('SELECT id, account_id, status, message, reward, created_at '
				   'FROM checkin_logs WHERE id > ? ORDER BY id LIMIT 30', (last_id,))
	return [{
		'_log_id': r['id'],
		'related_id': r['account_id'],
		'level': 'info' if r['status'] == 'success' else 'error',
		'title': f'checkin {r["status"]}',
		'reward': r['reward'],
		'message': r['message'],
		'created_at': r['created_at'],
	} for r in rows]


def worth_pushing(ev: dict) -> bool:
	"""噪音控制。

	interval 6h 调度下每天产生 4 轮 × 账号数 条日志，绝大多数是
	「余额无变化，今日已签到」—— 每条都推会让人对通知彻底脱敏。
	只推真拿到额度的，以及任何失败/跳过。
	"""
	if ev.get('level') != 'info':
		return True
	try:
		return float(str(ev.get('reward') or '').lstrip('$').strip() or 0) > 0
	except ValueError:
		return False




def push_loop():
	"""推送循环。两个独立游标，各推各的，互不影响：

	  * checkin_logs → 签到结果（权威记录，定时调度也有）
	  * events       → 其余事件（余额异常、站点公告…），跳过 type=checkin 避免重复

	用自增 id 而不是时间窗口：重启或卡顿都不会漏推或重推。首次启动不补推历史。
	"""
	state = load_state()
	if state.get('last_event_id') is None:
		rows = db_rows('SELECT MAX(id) AS m FROM events')
		state['last_event_id'] = (rows[0]['m'] if rows and rows[0]['m'] else 0)
	if state.get('last_checkin_log_id') is None:
		rows = db_rows('SELECT MAX(id) AS m FROM checkin_logs')
		state['last_checkin_log_id'] = (rows[0]['m'] if rows and rows[0]['m'] else 0)
	save_state(state)
	last, last_log = state['last_event_id'], state['last_checkin_log_id']

	while True:
		try:
			logs = checkin_log_events(last_log)
			if logs:
				amap = {a['id']: a for a in accounts_data()}
				for ev in logs:
					if worth_pushing(ev):
						if not send_checked(
								OWNER, F.fmt_checkin_event(ev, amap.get(ev['related_id'])),
								silent=(ev['level'] == 'info')):
							break          # 临时故障，本条留到下轮重发
						time.sleep(0.4)   # 避免触发 Telegram 限流
					last_log = ev['_log_id']
					state['last_checkin_log_id'] = last_log
					save_state(state)

			rows = db_rows('SELECT id, type, title, message, level, related_id, created_at '
						   'FROM events WHERE id > ? ORDER BY id LIMIT 20', (last,))
			for ev in rows:
				last = ev['id']
				state['last_event_id'] = last
				save_state(state)
				if ev['type'] == 'checkin':
					continue          # 已由 checkin_logs 覆盖
				if not send_checked(OWNER, F.fmt_generic_event(ev),
										silent=(ev.get('level') == 'info')):
					continue
				time.sleep(0.4)
		except Exception:
			traceback.print_exc()
		time.sleep(20)


# ------------------------------------------------------------------ 代理看门狗


PROXY_PROBE_TARGETS = ('https://anyrouter.top/login', 'https://agentrouter.org/login')
WATCHDOG_INTERVAL = 300      # 5 分钟探一次
WATCHDOG_THRESHOLD = 3       # 连续 3 次（约 15 分钟）失败才动手


def probe_proxy() -> tuple[bool, str]:
	"""经代理探目标站是否真的可达。

	不用 mihomo 自带的 delay 接口 —— 它只测到 gstatic 的连通性，节点对目标站
	是否可用要另说（实测出现过 delay 正常但目标站 EOF 的情况）。
	"""
	op = urllib.request.build_opener(urllib.request.ProxyHandler(
		{'http': 'http://127.0.0.1:7890', 'https': 'http://127.0.0.1:7890'}))
	try:
		ip = op.open('https://api.ipify.org', timeout=20).read().decode().strip()
	except Exception as exc:
		return False, f'出口不通（{type(exc).__name__}）'
	for url in PROXY_PROBE_TARGETS:
		try:
			op.open(urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=25)
		except urllib.error.HTTPError:
			pass   # 403/404 说明连得上，只是被拦，属于另一类问题，不归看门狗管
		except Exception as exc:
			return False, f'{url.split("/")[2]} 不通（{type(exc).__name__}）'
	return True, ip


def switch_node(name: str) -> bool:
	req = urllib.request.Request(f'{MIHOMO_API}/proxies/PROXY',
								 data=json.dumps({'name': name}).encode(), method='PUT',
								 headers={'Content-Type': 'application/json'})
	try:
		urllib.request.urlopen(req, timeout=15)
		return True
	except Exception:
		return False


def find_working_node(exclude: str | None) -> tuple[str | None, str]:
	"""逐个试，返回第一个目标站真正可达的节点。"""
	info = proxy_data()
	for name in info.get('all') or []:
		if name == exclude:
			continue
		if not switch_node(name):
			continue
		time.sleep(2)
		ok, detail = probe_proxy()
		if ok:
			return name, detail
	return None, ''


def proxy_watchdog():
	"""看住代理节点。

	proxy-group 用的是 select 固定节点 —— 因为 WAF cookie 和 session 绑出口 IP，
	url-test/fallback 那种自动漂移会让已登录 session 失效。代价是节点真挂了
	不会自动切：2026-08-20 美国-直连 EOF，连续两轮签到全军覆没才被发现。

	所以这里补上兜底，但要求**连续多次**失败才动手 —— 网络抖动就切会让 IP
	无谓漂移，那正是当初放弃 fallback 的原因。
	"""
	fails = 0
	while True:
		time.sleep(WATCHDOG_INTERVAL)
		try:
			ok, detail = probe_proxy()
			if ok:
				if fails:
					print(f'[WATCHDOG] 代理恢复（{detail}），失败计数清零', flush=True)
				fails = 0
				continue

			fails += 1
			print(f'[WATCHDOG] 代理探测失败 {fails}/{WATCHDOG_THRESHOLD}: {detail}', flush=True)
			if fails < WATCHDOG_THRESHOLD:
				continue

			current = (proxy_data() or {}).get('now')
			send(OWNER, f'<b>🟡 代理节点异常</b>\n\n'
						f'🔌 <code>{F.esc(current)}</code>\n'
						f'⚠️ {F.esc(detail)}\n\n'
						f'正在自动寻找可用节点…')
			name, info = find_working_node(exclude=current)
			if name:
				send(OWNER, f'<b>✅ 已自动切换节点</b>\n\n'
							f'🔌 <code>{F.esc(name)}</code>\n'
							f'🌐 出口 <code>{F.esc(info)}</code>\n\n'
							f'💡 换 IP 后首轮签到可能触发一次自动重登，属正常。')
				print(f'[WATCHDOG] 已切换到 {name}（{info}）', flush=True)
			else:
				switch_node(current)   # 都不通就切回去，至少状态可预期
				send(OWNER, f'<b>🔴 所有节点都不可用</b>\n\n'
							f'已切回 <code>{F.esc(current)}</code>。\n'
							f'可能是机场故障或订阅过期，需要人工处理。\n'
							f'用 /proxy 查看节点列表。')
				print('[WATCHDOG] 所有节点均不可用', flush=True)
			fails = 0
		except Exception:
			traceback.print_exc()


# ------------------------------------------------------------------ 主循环


def on_message(msg: dict):
	chat_id = str(((msg.get('chat') or {}).get('id')) or '')
	if chat_id != OWNER:
		return   # 静默忽略：不回复陌生人，避免被探测
	text = (msg.get('text') or '').strip()
	if not text:
		return
	if handle_cred_input(chat_id, text, msg):
		return
	if not text.startswith('/'):
		send(chat_id, '发 /help 看可用命令。')
		return
	cmd, *args = text[1:].split()
	cmd = cmd.split('@')[0].lower()   # 兼容 /cmd@BotName
	fn = COMMANDS.get(cmd)
	if not fn:
		send(chat_id, f'未知命令 <code>/{F.esc(cmd)}</code>，发 /help 看列表。')
		return
	try:
		fn(chat_id, args, msg)
	except Exception as exc:
		traceback.print_exc()
		send(chat_id, f'🔴 命令执行出错：<code>{F.esc(type(exc).__name__)}: {F.esc(exc)}</code>')


def register_commands():
	"""注册命令菜单，Telegram 客户端里输入 / 会有提示。"""
	tg('setMyCommands', commands=[
		{'command': 'status', 'description': '系统总览'},
		{'command': 'accounts', 'description': '账号列表'},
		{'command': 'checkin', 'description': '签到（可加账号 id）'},
		{'command': 'refresh', 'description': '刷新余额'},
		{'command': 'logs', 'description': '签到记录'},
		{'command': 'add', 'description': '添加账号'},
		{'command': 'del', 'description': '删除账号'},
		{'command': 'enable', 'description': '启用签到'},
		{'command': 'disable', 'description': '禁用签到'},
		{'command': 'proxy', 'description': '代理节点'},
		{'command': 'schedule', 'description': '签到间隔'},
		{'command': 'sites', 'description': '站点列表'},
		{'command': 'services', 'description': '服务状态'},
		{'command': 'help', 'description': '帮助'},
	])


def main() -> int:
	if not TOKEN or not OWNER:
		print('缺少 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，退出')
		return 1
	me = tg('getMe')
	if not me.get('ok'):
		print('Telegram getMe 失败:', me.get('description'))
		return 1
	print(f'bot @{me["result"].get("username")} 就绪，仅响应 chat_id={OWNER}', flush=True)
	register_commands()

	threading.Thread(target=push_loop, daemon=True).start()
	threading.Thread(target=proxy_watchdog, daemon=True).start()

	state = load_state()
	offset = state.get('update_offset', 0)
	while True:
		try:
			res = tg('getUpdates', offset=offset, timeout=50,
					 allowed_updates=['message', 'callback_query'])
			if not res.get('ok'):
				time.sleep(5)
				continue
			for upd in res.get('result') or []:
				offset = upd['update_id'] + 1
				state['update_offset'] = offset
				save_state(state)
				try:
					if 'message' in upd:
						on_message(upd['message'])
					elif 'callback_query' in upd:
						on_callback(upd['callback_query'])
				except Exception:
					traceback.print_exc()
		except Exception:
			traceback.print_exc()
			time.sleep(5)


if __name__ == '__main__':
	sys.exit(main())
