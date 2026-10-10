#!/usr/bin/env python3
"""用一段 session cookie 导入账号 —— 配合"在本地浏览器登录"的用法。

为什么需要它
------------
OAuth（Linux.do / GitHub）账号没有密码。远程浏览器（browser_login.sh / 面板）
是一条路，但在自己电脑的浏览器里登录体验更好，而且往往本来就是登录状态。
问题是 session cookie 通常带 HttpOnly，网页 JS 读不到，没法一键回传。

所以折中：你在本地浏览器 F12 里复制 session 的值粘进来，剩下的全自动 ——
把 session 注入 CloakBrowser 的上下文，让**浏览器自己**导航到 /console 过 WAF
挑战并调 /api/user/self，`api_user` 和用户名从拦截到的响应里读。

为什么必须用浏览器而不是 httpx：阿里云 WAF 现在下发的是 `aliyun_waf_aa`/
`aliyun_waf_bb` 那套新版 JS 挑战，必须执行页面脚本才能通过。纯 HTTP 请求
（httpx、以及 metapi 自己的 verifyToken）只会拿到挑战页 HTML，表现为
「返回非 JSON」/「Token verification timed out」。只带 acw_tc 是不够的。

session 值经文件传入（不走命令行参数，避免出现在 ps 输出里），读完即删。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from paths import CHECKIN_DIR, bootstrap

bootstrap()

from dotenv import load_dotenv  # noqa: E402

load_dotenv(CHECKIN_DIR / '.env')

import metapi_bridge as mb  # noqa: E402
from utils.proxy import get_playwright_proxy  # noqa: E402
from utils.config import AppConfig  # noqa: E402

UA = ('Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36')


def publish(**fields) -> None:
	path = os.environ.get('IMPORT_SESSION_STATE_FILE')
	if not path:
		return
	existing = {}
	try:
		if Path(path).exists():
			existing = json.loads(Path(path).read_text(encoding='utf-8'))
	except Exception:
		existing = {}
	existing.update(fields)
	try:
		Path(path).write_text(json.dumps(existing, ensure_ascii=False), encoding='utf-8')
	except Exception:
		pass


PROBE_JS = """async (apiUser) => {
	const headers = {'Accept': 'application/json'};
	if (apiUser) headers['new-api-user'] = String(apiUser);
	try {
		const r = await fetch('/api/user/self', {credentials: 'include', headers});
		const text = await r.text();
		let data = null;
		try { data = JSON.parse(text); } catch (e) {}
		return {status: r.status, data: data, raw: text.slice(0, 240)};
	} catch (e) {
		return {status: 0, data: null, raw: String(e)};
	}
}"""


async def verify_in_browser(provider_config, session_value: str, api_user):
	"""把 session 注入浏览器上下文，在页面内主动请求 /api/user/self 确认身份。

	返回 (profile, cookie_header)，失败返回 (None, None)。

	为什么必须在浏览器里做：阿里云 WAF 下发的是 `aliyun_waf_aa`/`aliyun_waf_bb`
	那套新版 JS 挑战，必须执行页面脚本才能通过。服务器上用 httpx 直接请求只会
	拿到挑战页 HTML（metapi 自己的 verifyToken 也栽在这里，报「Token verification
	timed out」）。浏览器打开一次页面就把挑战解了，之后同上下文的 fetch 都正常。

	为什么必须主动 fetch 而不是用 verify_browser_login 被动拦截：注入 session 后
	前端并不认为自己已登录（它还要 localStorage 里的用户信息），于是页面**根本不会
	发** /api/user/self，拦截等于空等。

	为什么必须带 new-api-user 头：不带的话服务端直接 401
	「无权进行此操作，未提供 New-Api-User」。所以 api_user 对这类站点是必填项。
	"""
	from urllib.parse import urlparse

	from cloakbrowser import launch_async

	browser = ctx = None
	try:
		# 必须跟着 provider 的 use_proxy 走代理：anyrouter 的出口 IP 被 ESA 封了，
		# 不走代理这里会直接吃 403（签到侧已经这么做了，这里之前漏了）。
		launch_kwargs: dict = {'headless': False, 'human_preset': 'careful'}
		proxy = get_playwright_proxy(use_proxy=provider_config.use_proxy)
		if proxy:
			launch_kwargs['proxy'] = proxy
			print(f'[INFO] 浏览器经代理: {proxy.get("server")}')
		# 有头模式跑在 xvfb 上：反检测效果比 headless 好
		browser = await launch_async(**launch_kwargs)
		ctx = await browser.new_context(viewport={'width': 1440, 'height': 900})
		page = await ctx.new_page()

		print(f'[INFO] 预热 {provider_config.domain}（让浏览器解掉 WAF 挑战）')
		await page.goto(provider_config.domain, wait_until='domcontentloaded', timeout=90_000)
		await page.wait_for_timeout(6000)

		print('[INFO] 注入 session cookie')
		await ctx.add_cookies([{'name': 'session', 'value': session_value,
								'url': provider_config.domain}])
		# 再导航一次，让后续 fetch 处在目标站 origin 下且带上 cookie
		await page.goto(f'{provider_config.domain}/console',
						wait_until='domcontentloaded', timeout=90_000)
		await page.wait_for_timeout(4000)

		attempts = [api_user] if api_user else [None]
		if api_user is None:
			print('[WARN] 未提供 api_user，先试不带头（多数 NewAPI 站点会 401）')
		for who in attempts:
			res = await page.evaluate(PROBE_JS, str(who) if who else None)
			status = res.get('status')
			data = res.get('data') or {}
			label = f'new-api-user={who}' if who else '不带 new-api-user'
			if status == 200 and data.get('success') and (data.get('data') or {}).get('id') is not None:
				print(f'[SUCCESS] {label}: 认证通过')
				cookies = await ctx.cookies()
				cookie_map = {c['name']: c['value'] for c in cookies if c.get('name')}
				print(f'[INFO] 完整 cookie: {sorted(cookie_map)}')
				return data['data'], '; '.join(f'{k}={v}' for k, v in cookie_map.items())
			snippet = ' '.join(str(res.get('raw') or '').split())[:160]
			print(f'[WARN] {label}: HTTP {status}  {snippet}')
			if status == 401 and 'New-Api-User' in snippet and not who:
				print('[HINT] 该站点必须提供 api_user（就是你的用户 ID），请填上重试')

		return None, None
	finally:
		try:
			if ctx:
				await ctx.close()
			if browser:
				await browser.close()
		except Exception:
			pass


async def main() -> int:
	site_id = None
	session_file = None
	api_user_in = None
	argv = sys.argv[1:]
	i = 0
	while i < len(argv):
		if argv[i] == '--site-id' and i + 1 < len(argv):
			site_id = int(argv[i + 1]); i += 2
		elif argv[i] == '--session-file' and i + 1 < len(argv):
			session_file = argv[i + 1]; i += 2
		elif argv[i] == '--api-user' and i + 1 < len(argv):
			api_user_in = argv[i + 1]; i += 2
		else:
			i += 1

	if not site_id or not session_file:
		print('用法: import_session.py --site-id N --session-file FILE [--api-user X]')
		return 1

	auth = mb.read_env_file(mb.METAPI_ENV, 'AUTH_TOKEN')
	if not auth:
		print('[FAILED] 读不到 metapi AUTH_TOKEN')
		return 1

	sf = Path(session_file)
	session_value = sf.read_text(encoding='utf-8').strip()
	sf.unlink(missing_ok=True)  # 读完即删
	if not session_value:
		print('[FAILED] session 为空')
		publish(state='failed', message='session 为空')
		return 1
	# 容忍粘成 "session=xxx" 或整条 cookie header
	if '=' in session_value:
		parts = dict(p.strip().split('=', 1) for p in session_value.split(';')
					 if '=' in p)
		session_value = parts.get('session', session_value)

	status, sites = mb.metapi_api('/api/sites', auth=auth)
	site_list = sites if isinstance(sites, list) else sites.get('sites', sites.get('data', []))
	site = next((x for x in site_list if x.get('id') == site_id), None)
	if not site:
		print('[FAILED] 站点不存在')
		publish(state='failed', message='站点不存在')
		return 1
	provider_name = mb.resolve_provider(site.get('url'), site.get('platform'))
	provider_config = AppConfig.load_from_env().get_provider(provider_name)
	if not provider_config:
		print(f'[FAILED] 没有 provider={provider_name} 的配置')
		publish(state='failed', message='provider 未配置')
		return 1

	print(f'[INFO] 站点 {site.get("name")} ({provider_config.domain}), provider={provider_name}')
	publish(state='running', message='正在用浏览器过 WAF 挑战并验证 session')

	profile, cookie_header = await verify_in_browser(
		provider_config, session_value, api_user_in)
	if not profile:
		print('[FAILED] session 无效，或站点正在拦截我们的请求')
		publish(state='failed', message='session 无效或站点正在拦截')
		return 1

	username = (profile.get('username') or profile.get('display_name')
				or f'{provider_name}-{profile["id"]}')
	api_user = str(profile['id'])
	quota = profile.get('quota')
	print(f'[SUCCESS] 用户名={username}  api_user={api_user}'
		  + (f'  余额=${round(quota / 500000, 2)}' if quota is not None else ''))

	# 直接写库：走 POST /api/accounts 会触发 metapi 那个 10 秒硬编码的纯 HTTP
	# 验证（就是「Token verification timed out (10s)」的来源），而这份 session
	# 我们上面已经用真 WAF cookie 验过了
	account_id = mb.insert_account(site_id, username, cookie_header,
								   api_user=api_user, profile=profile)
	if not account_id:
		publish(state='failed', message='写入 metapi 失败')
		return 1
	print(f'[DONE] 已写入 metapi: id={account_id}  {username}  api_user={api_user}')
	publish(state='done', message=f'已添加 {username}',
			username=username, api_user=api_user, account_id=account_id)
	return 0


if __name__ == '__main__':
	import asyncio

	try:
		sys.exit(asyncio.run(main()))
	except KeyboardInterrupt:
		sys.exit(1)
	except Exception as exc:
		print(f'[FAILED] 异常: {exc}')
		import traceback

		traceback.print_exc()
		publish(state='failed', message=str(exc)[:200])
		sys.exit(1)
