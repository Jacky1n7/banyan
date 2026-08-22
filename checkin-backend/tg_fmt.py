#!/usr/bin/env python3
"""Telegram 消息格式化。

状态灯约定（全局统一，看一眼就知道要不要处理）：
  🟢 正常   🟡 需要注意但不影响运行   🔴 坏了要处理   ⚪ 主动禁用
"""

from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))


def esc(value) -> str:
	"""HTML 转义。Telegram 的 HTML parse_mode 只需处理这三个字符。"""
	return html.escape(str(value if value is not None else ''), quote=False)


def money(v) -> str:
	try:
		return f'${float(v):,.2f}'
	except (TypeError, ValueError):
		return '$—'


def mask_user(u: str) -> str:
	"""邮箱打码，避免通知截图外泄完整账号。"""
	u = str(u or '')
	if '@' not in u:
		return u
	name, _, domain = u.partition('@')
	head = name[:3] if len(name) > 3 else name[:1]
	return f'{head}***@{domain}'


def to_cst(ts: str | None) -> str:
	"""把 metapi 的时间戳统一成北京时间。

	库里两种格式混用：UTC 的 'YYYY-MM-DD HH:MM:SS'（自己写的）
	和 ISO8601 带 Z（drizzle 写的）。
	"""
	if not ts:
		return '—'
	raw = str(ts).strip()
	try:
		if raw.endswith('Z'):
			dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
		elif 'T' in raw:
			dt = datetime.fromisoformat(raw)
			if dt.tzinfo is None:
				dt = dt.replace(tzinfo=timezone.utc)
		else:
			dt = datetime.strptime(raw, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
		return dt.astimezone(CST).strftime('%m-%d %H:%M')
	except Exception:
		return raw[:16]


def now_cst() -> str:
	"""当前北京时间。to_cst(None) 是给「解析不出时间戳」用的，返回破折号，
	不能拿来当「现在」—— 面板底部的时间戳一度显示成了 —。
	"""
	return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime('%m-%d %H:%M')


def ago(ts: str | None) -> str:
	"""距今多久，让「上次签到」一眼能判断是否卡住。"""
	if not ts:
		return '从未'
	raw = str(ts).strip()
	try:
		if raw.endswith('Z'):
			dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
		elif 'T' in raw:
			dt = datetime.fromisoformat(raw)
			if dt.tzinfo is None:
				dt = dt.replace(tzinfo=timezone.utc)
		else:
			dt = datetime.strptime(raw, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
	except Exception:
		return '—'
	delta = (datetime.now(timezone.utc) - dt).total_seconds()
	if delta < 0:
		return '刚刚'
	for limit, div, unit in ((3600, 60, '分钟'), (86400, 3600, '小时'), (float('inf'), 86400, '天')):
		if delta < limit:
			return f'{int(delta // div)}{unit}前'
	return '—'


def account_lamp(acct: dict) -> str:
	"""账号状态灯。

	判断顺序是有意的：先看是否被主动禁用，再看硬故障（缺 api_user / 无凭据），
	最后才看「久未签到」这类软信号 —— 否则一个禁用的账号会被报成红灯。
	"""
	if acct.get('status') != 'active':
		return '⚪'
	if not acct.get('api_user'):
		return '🔴'
	if not acct.get('has_password') and not acct.get('has_session'):
		return '🔴'
	# 这里的 last_checkin 已由调用方替换成 checkin_logs 里真实的最近成功时间，
	# 不是 metapi 的 accounts.last_checkin_at（那个在 interval 调度下不推进）
	last = acct.get('last_checkin')
	if last:
		try:
			raw = str(last)
			dt = (datetime.fromisoformat(raw.replace('Z', '+00:00')) if 'T' in raw
				  else datetime.strptime(raw, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc))
			# 这些站是滚动 24h 一签，超过 30 小时没签上说明有问题
			if (datetime.now(timezone.utc) - dt).total_seconds() > 30 * 3600:
				return '🟡'
		except Exception:
			pass
	else:
		return '🟡'
	if not acct.get('has_password'):
		return '🟡'  # 只有 session，过期后需人工重导
	return '🟢'


def cred_badge(acct: dict) -> str:
	if acct.get('has_password') and acct.get('has_session'):
		return '🔑+🍪'
	if acct.get('has_password'):
		return '🔑'
	if acct.get('has_session'):
		return '🍪'
	return '∅'


def bar(used: float, total: float, width: int = 10) -> str:
	"""额度消耗条。用块字符而不是 emoji，等宽对齐才好看。"""
	try:
		ratio = 0.0 if total <= 0 else max(0.0, min(1.0, used / total))
	except (TypeError, ZeroDivisionError):
		ratio = 0.0
	filled = int(round(ratio * width))
	return '▓' * filled + '░' * (width - filled) + f' {ratio * 100:.0f}%'


def fmt_checkin_event(ev: dict, acct: dict | None, grants: dict | None = None) -> str:
	"""签到结果推送。

	这是最高频的通知，所以第一行就要能回答「拿到了没、拿了多少」—— 其余是上下文。
	带上今日累计是因为单看一条不知道整体进度：四个账号分散在一天里各自到账，
	只有累计数字能说明「今天是否都齐了」。
	"""
	success = ev.get('level') != 'error'
	title = ev.get('title') or ''
	skipped = 'skipped' in title
	raw_reward = str(ev.get('reward') or '').lstrip('$').strip()
	try:
		reward = float(raw_reward) if raw_reward else 0.0
	except ValueError:
		reward = 0.0

	if not success:
		head = '🔴 签到失败'
	elif reward > 0:
		head = f'✅ 签到成功  <b>+{money(reward)}</b>'
	elif skipped:
		head = '⏭ 签到跳过'
	else:
		head = '✅ 已签到'

	lines = [f'<b>{head}</b>', '']
	if acct:
		lines.append(f'👤 <code>{esc(mask_user(acct.get("username")))}</code>'
					 f'   <i>{esc(acct.get("site"))}</i>')
		bal, used = acct.get('balance') or 0, acct.get('used') or 0
		total = bal + used
		lines.append('')
		lines.append(f'💰 余额 <b>{money(bal)}</b>   已用 {money(used)}')
		lines.append(f'📊 <code>{bar(used, total, 12)}</code>  总额 {money(total)}')

	msg = ev.get('message') or ''
	if msg:
		# metapi 的 message 形如 "user @ site: 详情"，前半段上面已经展示过
		detail = msg.split(':', 1)[1].strip() if ':' in msg else msg
		# 奖励金额已经在标题里了，正文里再说一遍是冗余
		if detail and not (reward > 0 and '获得' in detail):
			lines.append('')
			lines.append(f'{"📝" if success else "⚠️"} {esc(detail)[:170]}')

	if grants:
		total_today = sum(v for v in grants.values() if v)
		if total_today > 0:
			lines.append('')
			lines.append(f'💵 今日累计 <b>{money(total_today)}</b>'
						 f'   ({len(grants)} 个账号已领)')

	lines.append('')
	lines.append(f'🕐 {to_cst(ev.get("created_at"))}')
	return chr(10).join(lines)


def fmt_generic_event(ev: dict) -> str:
	icon = {'error': '🔴', 'warning': '🟡'}.get(ev.get('level'), 'ℹ️')
	lines = [f'<b>{icon} {esc(ev.get("title"))}</b>', '']
	if ev.get('message'):
		lines.append(esc(str(ev['message'])[:400]))
		lines.append('')
	lines.append(f'🕐 {to_cst(ev.get("created_at"))}')
	return '\n'.join(lines)


def fmt_auto_panel(sched: dict, accounts: list[dict], grants: dict,
				   proxy: str | None = None) -> str:
	"""自动签到控制台。

	这是最常看的一屏，所以把「今天到手多少」放在最上面 —— 其余都是为了解释它。
	"""
	mode = sched.get('mode') or 'interval'
	hours = sched.get('intervalHours')
	on = [a for a in accounts if a.get('checkin_enabled') and a.get('status') == 'active']
	got = [a for a in on if grants.get(a['id'])]
	income = sum(grants.get(a['id'], 0) for a in on)

	lamp = '🟢' if on else '⚪'
	sched_txt = f'每 {hours}h' if mode == 'interval' and hours else esc(str(mode))

	lines = [
		'<b>🤖 自动签到</b>',
		'',
		f'💵 今日到手  <b>{money(income)}</b>   ({len(got)}/{len(on)} 个账号已领)',
		f'{lamp} 调度      {sched_txt}   ·   {len(on)}/{len(accounts)} 个账号已启用',
	]
	if proxy:
		lines.append(f'🌐 代理      <code>{esc(proxy)}</code>')
	lines.append('')
	lines.append('┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈')

	for a in accounts:
		enabled = bool(a.get('checkin_enabled')) and a.get('status') == 'active'
		g = grants.get(a['id'])
		if not enabled:
			mark, note = '⚪', '已停用'
		elif g:
			mark, note = '✅', f'今日 <b>+{money(g)}</b>'
		else:
			mark, note = '⏳', '待领取'
		bal, used = a.get('balance') or 0, a.get('used') or 0
		lines.append(f'{mark} <code>#{a["id"]}</code> {esc(mask_user(a.get("username")))}'
					 f'  <i>{esc(a.get("site"))}</i>')
		lines.append(f'      {note}  ·  余 {money(bal)}  ·  <code>{bar(used, bal + used, 8)}</code>')

	lines.append('┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈')
	lines.append('')
	nopwd = [a for a in on if not a.get('has_password')]
	if nopwd:
		lines.append(f'⚠️ {len(nopwd)} 个账号只有 session，过期后需人工重导：'
					 + ', '.join(f'#{a["id"]}' for a in nopwd))
	else:
		lines.append('🔑 全部账号都有密码，session 过期可自动续期')
	lines.append('')
	lines.append(f'🕐 {now_cst()} (CST)')
	return chr(10).join(lines)


def fmt_status(data: dict) -> str:
	"""/status 总览。目标：一屏看清「有没有事、钱还剩多少、下次什么时候跑」。"""
	svc = data.get('services') or {}
	accounts = data.get('accounts') or []
	bad = [a for a in accounts if account_lamp(a) == '🔴']
	warn = [a for a in accounts if account_lamp(a) == '🟡']
	svc_bad = [k for k, v in svc.items() if not v.get('ok')]

	if svc_bad or bad:
		overall = '🔴 有故障需要处理'
	elif warn:
		overall = '🟡 运行中，有项需注意'
	else:
		overall = '🟢 一切正常'

	lines = [f'<b>📊 系统总览</b>', '', f'<b>{overall}</b>', '']

	lines.append('<b>🖥 服务</b>')
	for name, info in svc.items():
		lamp = '🟢' if info.get('ok') else '🔴'
		extra = f' — {esc(info["detail"])}' if info.get('detail') else ''
		lines.append(f'{lamp} {esc(name)}{extra}')
	lines.append('')

	proxy = data.get('proxy') or {}
	if proxy:
		lamp = '🟢' if proxy.get('ok') else '🔴'
		lines.append('<b>🔌 代理</b>')
		lines.append(f'{lamp} {esc(proxy.get("node") or "—")}')
		lines.append(f'    出口 <code>{esc(proxy.get("ip") or "—")}</code>')
		lines.append('')

	total_bal = sum((a.get('balance') or 0) for a in accounts)
	total_used = sum((a.get('used') or 0) for a in accounts)
	lines.append(f'<b>💰 总余额 {money(total_bal)}</b>')
	lines.append(f'累计已用 {money(total_used)}')
	lines.append('')

	lines.append(f'<b>👤 账号 {len(accounts)} 个</b>')
	for a in accounts:
		lines.append(f'{account_lamp(a)} <code>{esc(mask_user(a.get("username")))}</code> '
					 f'{money(a.get("balance"))} · {cred_badge(a)}')
	lines.append('')

	sched = data.get('schedule') or {}
	lines.append('<b>⏰ 调度</b>')
	lines.append(f'签到 {esc(sched.get("checkin") or "—")}')
	lines.append(f'余额刷新 {esc(sched.get("balance") or "—")}')
	if data.get('cooldown'):
		lines.append('')
		lines.append('<b>🟡 登录退避中</b>')
		for prov, reason in data['cooldown'].items():
			lines.append(f'· {esc(prov)}: {esc(reason)}')
	lines.append('')
	lines.append(f'🕐 {datetime.now(CST).strftime("%m-%d %H:%M")}')
	return '\n'.join(lines)


def fmt_accounts(accounts: list[dict]) -> str:
	if not accounts:
		return '还没有账号。用 /add 添加。'
	lines = [f'<b>👤 账号列表（{len(accounts)}）</b>', '']
	for a in accounts:
		bal, used = a.get('balance') or 0, a.get('used') or 0
		lines.append(f'{account_lamp(a)} <b>#{a.get("id")}</b> '
					 f'<code>{esc(mask_user(a.get("username")))}</code>')
		lines.append(f'    🌐 {esc(a.get("site"))} · 🔑 {cred_badge(a)} · ID {a.get("api_user") or "—"}')
		lines.append(f'    💰 {money(bal)}  已用 {money(used)}')
		lines.append(f'    <code>{bar(used, bal + used)}</code>')
		lines.append(f'    🚀 上次签到 {ago(a.get("last_checkin"))}'
					 + ('' if a.get('checkin_enabled') else '  ⚪ 已禁用'))
		lines.append('')
	lines.append('🟢 正常  🟡 需注意  🔴 有问题  ⚪ 已禁用')
	lines.append('🔑 密码  🍪 session')
	return '\n'.join(lines)


def fmt_logs(logs: list[dict], accounts: dict) -> str:
	if not logs:
		return '暂无签到记录。'
	lines = [f'<b>📝 签到记录（最近 {len(logs)} 条）</b>', '']
	for lg in logs:
		st = lg.get('status')
		icon = {'success': '✅', 'failed': '🔴', 'skipped': '⏭'}.get(st, 'ℹ️')
		acct = accounts.get(lg.get('account_id')) or {}
		name = mask_user(acct.get('username')) or f'#{lg.get("account_id")}'
		reward = f'  <b>+{money(lg["reward"])}</b>' if lg.get('reward') else ''
		lines.append(f'{icon} <code>{esc(name)}</code>{reward}')
		lines.append(f'    {esc(str(lg.get("message") or "")[:80])}')
		lines.append(f'    🕐 {to_cst(lg.get("created_at"))}')
		lines.append('')
	return '\n'.join(lines)


def fmt_sites(sites: list[dict]) -> str:
	lines = [f'<b>🌐 可签到站点</b>', '']
	for s in sites:
		lamp = '🟢' if s.get('platform') == 'anyrouter' else '🔴'
		lines.append(f'{lamp} <b>#{s.get("id")}</b> {esc(s.get("name"))}')
		lines.append(f'    <code>{esc(s.get("url"))}</code>')
		lines.append(f'    platform=<code>{esc(s.get("platform"))}</code>'
					 + ('' if s.get('platform') == 'anyrouter'
						else '  ⚠️ 必须是 anyrouter，否则会掉回纯 HTTP'))
		lines.append('')
	return '\n'.join(lines)


HELP = """<b>🤖 签到管理机器人</b>

<b>📊 查询</b>
/status — 系统总览
/accounts — 账号列表
/logs [n] — 签到记录
/sites — 站点列表
/proxy — 代理节点

<b>🚀 操作</b>
/checkin — 全部签到
/checkin &lt;id&gt; — 指定账号签到
/refresh — 刷新余额
/add — 添加账号（引导式）
/del &lt;id&gt; — 删除账号
/enable &lt;id&gt; — 启用签到
/disable &lt;id&gt; — 禁用签到

<b>⚙️ 设置</b>
/schedule &lt;小时&gt; — 签到间隔
/proxy &lt;序号&gt; — 切换代理节点
/services — 服务状态

<b>说明</b>
· 账号密码/session 消息会被自动删除，不留在聊天记录里
· 删除账号需二次确认
· 签到走真浏览器过 WAF，单次 12-90 秒，请耐心"""
