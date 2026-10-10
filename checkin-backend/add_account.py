#!/usr/bin/env python3
"""往 metapi 添加签到账号（交互式），支持两种凭据。

  1) 邮箱 + 密码 —— 长期有效，桥接每轮用 CloakBrowser 重新登录拿新 session。
  2) session cookie —— 给 OAuth（Linux.do / GitHub）注册的账号用，它们没有密码。
     桥接每轮只让浏览器打开登录页拿 WAF cookie（无头、不提交表单），
     配合这里存的 session 完成签到。session 会过期，过期后回来重新导入即可。

为什么不用 metapi 面板自带的"添加账号"：那条路走 metapi 的纯 HTTP 登录
（NewApiAdapter.login），对 anyrouter/agentrouter 这类阿里云 WAF 站点不可靠。

密码/session 只在本机内存与 metapi 数据库（密码加密）中存在，不打印、不进 history。
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

from paths import CHECKIN_DIR, bootstrap

bootstrap()

from dotenv import load_dotenv  # noqa: E402

load_dotenv(CHECKIN_DIR / '.env')

from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402

import metapi_bridge as mb  # noqa: E402
from checkin import (  # noqa: E402
	login_with_credentials,
	parse_cookies,
	prepare_cookies,
	run_check_in_requests,
)
from utils.config import AccountConfig, AppConfig  # noqa: E402

COOKIE_HELP = """
  怎么拿 session cookie（OAuth 账号用这个）：
    1. 浏览器登录该站点（Linux.do / GitHub 登录都行）
    2. F12 → Application(应用) → Cookies → 选该站点域名
    3. 复制名为 session 的那一行的值（很长一串）

  怎么拿 api_user（请求头 new-api-user 的值，可选但建议填）：
    1. F12 → Network(网络) → 筛选 Fetch/XHR
    2. 页面点一下刷新，找任意带 New-Api-User 请求头的请求
    3. 复制该头的值（正常是 5 位数字；负数或个位数说明没登录）
"""


def b64url_encode(raw: bytes) -> str:
	return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def encrypt_password(password: str, secret: str) -> str:
	"""按 metapi accountCredentialService.ts 的格式加密：v1:iv:tag:data（base64url）。

	Node 的 createCipheriv('aes-256-gcm') 把 tag 与 ciphertext 分开存，
	而 Python 的 AESGCM.encrypt 返回 ciphertext+tag 拼接，需要切开。
	"""
	key = hashlib.sha256(secret.encode('utf-8')).digest()
	iv = os.urandom(12)
	blob = AESGCM(key).encrypt(iv, password.encode('utf-8'), None)
	ciphertext, tag = blob[:-16], blob[-16:]
	return 'v1:%s:%s:%s' % (b64url_encode(iv), b64url_encode(tag), b64url_encode(ciphertext))


def ask(prompt: str, secret: bool = False, required: bool = True) -> str:
	while True:
		value = (getpass.getpass(prompt) if secret else input(prompt)).strip()
		if value or not required:
			return value
		print('    不能为空')


def find_account_id(site_id: int, username: str) -> int | None:
	conn = sqlite3.connect(f'file:{mb.METAPI_DB}?mode=ro', uri=True, timeout=30)
	try:
		row = conn.execute(
			'SELECT id FROM accounts WHERE site_id=? AND username=? ORDER BY id DESC LIMIT 1',
			(site_id, username),
		).fetchone()
	finally:
		conn.close()
	return row[0] if row else None


async def main() -> int:
	auth = mb.read_env_file(mb.METAPI_ENV, 'AUTH_TOKEN')
	secret = mb.read_env_file(mb.METAPI_ENV, 'ACCOUNT_CREDENTIAL_SECRET') or auth
	if not auth or not secret:
		print('读不到 metapi 的 AUTH_TOKEN / ACCOUNT_CREDENTIAL_SECRET')
		return 1

	# 加密自检：先确认 encrypt→decrypt 往返一致，再拿真密码去跑
	probe = 'self-check-%s' % os.urandom(4).hex()
	if mb.decrypt_password(encrypt_password(probe, secret), secret) != probe:
		print('加密自检失败，中止（否则会写入 metapi 解不开的密码）')
		return 1

	status, sites = mb.metapi_api('/api/sites', auth=auth)
	site_list = sites if isinstance(sites, list) else sites.get('sites', sites.get('data', []))
	usable = [s for s in site_list if mb.resolve_provider(s.get('url'), s.get('platform'))]
	if not usable:
		print('metapi 里没有可签到的站点，先在面板建站点')
		return 1

	print('=' * 66)
	print('  往 metapi 添加签到账号')
	print('=' * 66)
	print('\n可签到的站点：')
	for i, site in enumerate(usable, 1):
		provider = mb.resolve_provider(site.get('url'), site.get('platform'))
		print('  %d) %-14s %-26s → provider=%s' % (i, site.get('name'), site.get('url'), provider))

	app_config = AppConfig.load_from_env()
	added = 0

	while True:
		print()
		choice = ask('选站点编号（直接回车结束）: ', required=False)
		if not choice:
			break
		try:
			site = usable[int(choice) - 1]
		except (ValueError, IndexError):
			print('    编号无效')
			continue

		provider_name = mb.resolve_provider(site.get('url'), site.get('platform'))
		provider_config = app_config.get_provider(provider_name)
		if not provider_config:
			print(f'    签到脚本里没有 provider={provider_name} 的配置')
			continue

		print('\n  凭据方式：')
		print('    1) 邮箱 + 密码（推荐，长期有效）')
		print('    2) session cookie（OAuth 账号用 —— Linux.do / GitHub 登录的没有密码）')
		mode_choice = ask('  选择 [1/2]: ')
		use_password = mode_choice.strip() != '2'

		password = session_value = None
		api_user = None

		if use_password:
			username = ask('  邮箱: ')
			password = ask('  密码（输入时不显示）: ', secret=True)
		else:
			print(COOKIE_HELP)
			username = ask('  这个账号叫什么（随便起个名，用于面板显示）: ')
			session_value = ask('  session cookie 的值（输入时不显示）: ', secret=True)
			api_user = ask('  api_user（回车跳过）: ', required=False) or None

		# ---- 用真实签到来验证凭据，能签上才写入 ----
		print(f'\n  验证凭据（连 {provider_config.domain}）...')
		all_cookies = None
		if use_password:
			login = await login_with_credentials(username, provider_config, provider_name,
												 username, password)
			if login and login.cookies:
				all_cookies = dict(login.cookies)
				api_user = login.api_user or api_user
				print(f'  登录成功，拿到 {len(all_cookies)} 个 cookie')
			else:
				print('  ✗ 登录失败，未添加。可能是密码错误，或该站点正在限流（稍后再试）。')
				continue
		else:
			user_cookies = parse_cookies(session_value if '=' in session_value
										 else f'session={session_value}')
			all_cookies = await prepare_cookies(username, provider_config, user_cookies)
			if not all_cookies:
				print('  ✗ 拿不到 WAF cookie，未添加（站点可能正在限流，稍后再试）。')
				continue
			print(f'  WAF cookie 就绪，共 {len(all_cookies)} 个 cookie')

		cfg: dict = {'name': username, 'provider': provider_name}
		if use_password:
			cfg['email'] = username
			cfg['password'] = password
		else:
			cfg['cookies'] = dict(parse_cookies(session_value if '=' in session_value
												else f'session={session_value}'))
		if api_user:
			cfg['api_user'] = str(api_user)

		ok, before, after = run_check_in_requests(
			all_cookies, AccountConfig.from_dict(cfg, 0), username, provider_config,
			api_user_override=str(api_user) if api_user else None,
			use_proxy=provider_config.use_proxy,
		)
		if not (after and after.get('success')):
			print('  ✗ 凭据无效：读不到该账号的用户信息，未添加。')
			if not use_password:
				print('    session 可能填错或已过期；也可能是 api_user 没填/填错。')
			continue
		print(f'  ✓ 凭据有效，当前余额 ${after["quota"]:.2f}'
			  + ('，本次签到成功' if ok else '（今日已签到或无需签到）'))

		# ---- 写入 metapi ----
		cookie_header = '; '.join(f'{k}={v}' for k, v in all_cookies.items())
		payload = {
			'siteId': site['id'],
			'username': username,
			'accessToken': cookie_header,
			'credentialMode': 'session',
			# metapi 自己的签到调度不可靠，已全局关闭；签到由 metapi_bridge.py 负责。
			# 桥接按 accounts.status 判断要不要签，想停某账号就在面板设为 disabled。
			'checkinEnabled': False,
			'skipModelFetch': True,
		}
		status, resp = mb.metapi_api('/api/accounts', payload, auth=auth)
		if status >= 400:
			print(f'  ✗ 创建账号失败 HTTP {status}: {str(resp)[:200]}')
			continue

		account_id = find_account_id(site['id'], username)
		if not account_id:
			print('  账号已创建但查不到 id，无法写入凭据信息')
			continue

		extra: dict = {'credentialMode': 'session'}
		if api_user:
			try:
				extra['platformUserId'] = int(api_user)
			except (TypeError, ValueError):
				extra['platformUserId'] = api_user
		if use_password:
			extra['autoRelogin'] = {
				'username': username,
				'passwordCipher': encrypt_password(password, secret),
				'updatedAt': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
			}
		status, _ = mb.metapi_api(f'/api/accounts/{account_id}',
								  {'extraConfig': json.dumps(extra, ensure_ascii=False)},
								  method='PUT', auth=auth)
		mode_label = '邮箱密码' if use_password else 'session cookie'
		print(f'  ✅ 已添加: metapi id={account_id}  {username}  '
			  f'provider={provider_name}  凭据={mode_label}（extraConfig HTTP {status}）')
		added += 1

	if added:
		print(f'\n共添加 {added} 个账号。桥接读取验证：')
		for acct in mb.load_accounts_from_metapi(secret):
			user = acct['username']
			masked = user[:2] + '***@' + user.split('@')[1] if '@' in user else user
			print('  id=%-3s %-24s provider=%-12s mode=%-9s api_user=%s' % (
				acct['id'], masked, acct['provider'], acct['mode'], acct.get('api_user')))
		print('\n下一轮 timer（每 6 小时）会自动带上新账号。')
	return 0


if __name__ == '__main__':
	import asyncio

	try:
		sys.exit(asyncio.run(main()))
	except KeyboardInterrupt:
		print('\n已取消')
		sys.exit(1)
