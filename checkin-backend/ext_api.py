#!/usr/bin/env python3
"""给 metapi 用的「过 WAF 浏览器能力」RPC 后端。

背景
----
metapi 的 NewApiAdapter 全靠纯 HTTP，而 anyrouter/agentrouter 前面是阿里云 ESA，
下发的是 aliyun_waf_aa/aliyun_waf_bb 那套新版 JS 挑战 —— 必须执行页面脚本才能过。
所以它的 login/verifyToken/checkin/getBalance 全都不可靠（表现为
「Token verification timed out」「Unexpected token '<'」「余额刷不动」）。

这里把这些能力用 CloakBrowser 真浏览器实现，metapi 侧的 AnyRouterAdapter
重写对应方法转发过来。返回结构严格对齐 metapi 的 base.ts 类型定义。

本模块是**无状态的**：不写数据库、不发通知、不做调度 —— 那些统一由 metapi 负责，
避免两套并存导致重复推送和记录分裂。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

CHECKIN_DIR = Path('/opt/anyrouter-check-in')
sys.path.insert(0, str(CHECKIN_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(CHECKIN_DIR / '.env')

from checkin import login_with_credentials, parse_cookies, prepare_cookies, run_check_in_requests  # noqa: E402
from utils.config import AccountConfig, AppConfig  # noqa: E402
from utils.proxy import get_playwright_proxy  # noqa: E402

# 阿里云 ESA 下发的 WAF cookie：必须每次由浏览器现场获取，存量的会让请求吃拦截页
WAF_COOKIE_NAMES = {'acw_tc', 'cdn_sec_tc', 'acw_sc__v2'}
KNOWN_PROVIDERS = ('agentrouter', 'anyrouter')

# 在页面内主动请求。必须带 new-api-user —— 不带的话服务端直接
# 401「无权进行此操作，未提供 New-Api-User」。而被动拦截页面自己的请求也不行：
# 注入 session 后前端并不认为已登录，根本不会发这个请求。
PROBE_JS = """async (args) => {
	const [path, apiUser] = args;
	const headers = {'Accept': 'application/json'};
	if (apiUser) headers['new-api-user'] = String(apiUser);
	try {
		const r = await fetch(path, {credentials: 'include', headers});
		const text = await r.text();
		let data = null;
		try { data = JSON.parse(text); } catch (e) {}
		return {status: r.status, data: data, raw: text.slice(0, 240)};
	} catch (e) {
		return {status: 0, data: null, raw: String(e)};
	}
}"""


def resolve_provider(base_url: str) -> str | None:
	low = (base_url or '').lower()
	for name in KNOWN_PROVIDERS:
		if name in low:
			return name
	return None


def provider_config_for(base_url: str):
	name = resolve_provider(base_url)
	if not name:
		return None, None
	return name, AppConfig.load_from_env().get_provider(name)


def identity_cookies(token: str) -> dict:
	"""从 metapi 存的 accessToken 里取身份 cookie，丢掉存量 WAF cookie。

	prepare_cookies() 最后是 {**waf_cookies, **user_cookies}，user_cookies 会覆盖
	浏览器刚拿到的新 WAF cookie。带上过期/被标记的那批，请求会直接吃拦截页。
	"""
	raw = (token or '').strip()
	if not raw:
		return {}
	parsed = parse_cookies(raw) if '=' in raw else {'session': raw}
	return {k: v for k, v in parsed.items() if k.lower() not in WAF_COOKIE_NAMES}


async def _browser_probe(provider_config, token: str, api_user, paths: list[str]) -> dict:
	"""注入 session 到真浏览器，过 WAF 后在页面内请求给定接口。"""
	from cloakbrowser import launch_async

	ident = identity_cookies(token)
	if not ident.get('session'):
		print('[EXT] accessToken 里没有 session，拒绝', flush=True)
		return {'error': 'no-session', 'results': {}}

	kwargs: dict = {'headless': True, 'human_preset': 'careful'}
	proxy = get_playwright_proxy(use_proxy=provider_config.use_proxy)
	if proxy:
		kwargs['proxy'] = proxy

	browser = ctx = None
	try:
		print(f'[EXT] 起浏览器过 WAF: {provider_config.domain}'
			  f'{"（经代理）" if proxy else "（直连）"}', flush=True)
		browser = await launch_async(**kwargs)
		ctx = await browser.new_context(viewport={'width': 1440, 'height': 900})
		page = await ctx.new_page()
		# 先打开首页让浏览器把 JS 挑战解掉，之后同上下文的 fetch 才正常
		await page.goto(provider_config.domain, wait_until='domcontentloaded', timeout=90_000)
		await page.wait_for_timeout(5000)
		await ctx.add_cookies([{'name': k, 'value': v, 'url': provider_config.domain}
							   for k, v in ident.items()])
		await page.goto(f'{provider_config.domain}/console',
						wait_until='domcontentloaded', timeout=90_000)
		await page.wait_for_timeout(3000)

		results = {}
		for path in paths:
			res = await page.evaluate(PROBE_JS, [path, str(api_user) if api_user else None])
			ok = res.get('status') == 200 and isinstance(res.get('data'), dict) \
				and res['data'].get('success') is True
			print(f'[EXT] {path} → HTTP {res.get("status")} '
				  f'{"✓" if ok else "✗ " + str(res.get("raw"))[:90]}', flush=True)
			results[path] = res
		cookies = {c['name']: c['value'] for c in await ctx.cookies() if c.get('name')}
		return {'results': results,
				'cookieHeader': '; '.join(f'{k}={v}' for k, v in cookies.items())}
	finally:
		try:
			if ctx:
				await ctx.close()
			if browser:
				await browser.close()
		except Exception:
			pass


def _user_payload(data: dict) -> dict:
	"""NewAPI 的 /api/user/self 响应 → metapi 的 UserInfo + BalanceInfo。

	quota/used_quota 是内部单位，500000 = $1（与上游 get_user_info 同口径）。
	"""
	out: dict = {}
	if not isinstance(data, dict):
		return out
	out['userInfo'] = {
		'username': str(data.get('username') or ''),
		'displayName': data.get('display_name') or None,
		'email': data.get('email') or None,
		'role': data.get('role') if isinstance(data.get('role'), int) else None,
	}
	q, u = data.get('quota'), data.get('used_quota')
	if isinstance(q, (int, float)) or isinstance(u, (int, float)):
		balance = (q or 0) / 500000
		used = (u or 0) / 500000
		out['balance'] = {'balance': balance, 'used': used, 'quota': balance + used}
	out['platformUserId'] = data.get('id')
	return out


# ------------------------------------------------------------------ 动作


async def act_login(p: dict) -> dict:
	"""对应 adapter.login() → LoginResult"""
	name, pc = provider_config_for(p.get('baseUrl', ''))
	if not pc:
		return {'success': False, 'message': f'unsupported site: {p.get("baseUrl")}'}
	username, password = p.get('username') or '', p.get('password') or ''
	if not username or not password:
		return {'success': False, 'message': 'username/password required'}

	login = await login_with_credentials(username, pc, name, username, password)
	if not login or not login.cookies:
		return {'success': False, 'message': '浏览器登录失败（凭据错误，或站点正在限流）'}
	return {
		'success': True,
		# metapi 把 accessToken 原样存进 accounts.access_token，用完整 cookie header
		'accessToken': '; '.join(f'{k}={v}' for k, v in login.cookies.items()),
		'username': username,
		'platformUserId': login.api_user,
	}


async def act_verify(p: dict) -> dict:
	"""对应 adapter.verifyToken() → TokenVerifyResult"""
	name, pc = provider_config_for(p.get('baseUrl', ''))
	if not pc:
		return {'tokenType': 'unknown'}
	probe = await _browser_probe(pc, p.get('token') or '', p.get('platformUserId'),
								[pc.user_info_path])
	res = (probe.get('results') or {}).get(pc.user_info_path) or {}
	data = res.get('data') or {}
	if res.get('status') == 200 and data.get('success') and (data.get('data') or {}).get('id') is not None:
		payload = _user_payload(data['data'])
		return {'tokenType': 'session', 'userInfo': payload.get('userInfo'),
				'balance': payload.get('balance'), 'apiToken': None,
				'platformUserId': payload.get('platformUserId'),
				'freshCookieHeader': probe.get('cookieHeader')}
	return {'tokenType': 'unknown',
			'message': f"status={res.get('status')} {str(res.get('raw'))[:150]}"}


async def act_userinfo(p: dict) -> dict:
	"""对应 adapter.getUserInfo() → UserInfo | null"""
	out = await act_verify(p)
	return out.get('userInfo') or {}


async def act_balance(p: dict) -> dict:
	"""对应 adapter.getBalance() → BalanceInfo"""
	out = await act_verify(p)
	return out.get('balance') or {'balance': 0, 'used': 0, 'quota': 0}


async def act_checkin(p: dict) -> dict:
	"""对应 adapter.checkin() → CheckinResult

	用现拿的 WAF cookie + 存量身份 cookie 走上游的签到流程。agentrouter 的
	sign_in_path 是 None（查用户信息时自动签到），上游 needs_manual_check_in()
	已处理这个差异。
	"""
	name, pc = provider_config_for(p.get('baseUrl', ''))
	if not pc:
		return {'success': False, 'message': f'unsupported site: {p.get("baseUrl")}'}

	ident = identity_cookies(p.get('token') or '')
	if not ident.get('session'):
		return {'success': False, 'message': 'accessToken 里没有可用的 session'}

	label = p.get('username') or name
	api_user = p.get('platformUserId')
	all_cookies = await prepare_cookies(label, pc, ident)
	if not all_cookies:
		return {'success': False, 'message': '拿不到 WAF cookie（站点可能正在封锁）'}

	cfg = AccountConfig.from_dict(
		{'name': label, 'provider': name, 'cookies': ident,
		 **({'api_user': str(api_user)} if api_user else {})}, 0)
	ok, before, after = run_check_in_requests(
		all_cookies, cfg, label, pc,
		api_user_override=str(api_user) if api_user else None,
		use_proxy=pc.use_proxy)

	reward = None
	if before and after and before.get('success') and after.get('success'):
		delta = round(after['quota'] - before['quota'], 2)
		if delta:
			reward = f'${delta}'
	if not (after and after.get('success')):
		return {'success': False, 'message': '签到请求已发出，但读不到用户信息（凭据可能失效）'}
	msg = ('签到成功' + (f'，获得 {reward}' if reward else '（余额无变化，今日已签到）')) if ok else '签到失败'
	out = {'success': bool(ok), 'message': msg}
	if reward:
		out['reward'] = reward
	if after.get('success'):
		out['balance'] = {'balance': after['quota'], 'used': after['used_quota'],
						  'quota': after['quota'] + after['used_quota']}
	return out


ACTIONS = {
	'login': act_login,
	'verify': act_verify,
	'userinfo': act_userinfo,
	'balance': act_balance,
	'checkin': act_checkin,
}


def handle(action: str, payload: dict) -> dict:
	"""同步入口，供 panel.py 的 HTTP handler 调用。"""
	print(f'[EXT] ← metapi 调用 {action} baseUrl={payload.get("baseUrl")} '
		  f'apiUser={payload.get("platformUserId")}', flush=True)
	fn = ACTIONS.get(action)
	if not fn:
		return {'error': f'unknown action: {action}'}
	try:
		return asyncio.run(fn(payload or {}))
	except Exception as exc:
		import traceback

		traceback.print_exc()
		return {'error': f'{type(exc).__name__}: {exc}'}
