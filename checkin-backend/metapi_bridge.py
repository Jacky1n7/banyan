#!/usr/bin/env python3
"""metapi ↔ CloakBrowser 签到桥接。

为什么需要这个脚本
------------------
metapi 的面板和 API 网关很好用，但它的签到依赖 `newApiShield.ts` 里用 node:vm
沙箱求解阿里云 WAF 挑战 —— 硬编码正则匹配混淆符号（`function a0i()`、`L(0x115)`），
实测间歇失效：2026-08-18 01:08/01:11 两账号签到成功，一小时后 02:03 调度再跑时
两账号同时失败，返回 HTML 挑战页。CloakBrowser 是真浏览器，不依赖挑战脚本形式。

分工：账号只在 metapi 维护（面板增删改）→ 本脚本用 CloakBrowser 签到 →
结果与新鲜 cookies 写回 metapi（面板余额 / 签到历史 / API 网关继续可用）。

凭据策略：session 优先，密码兜底
--------------------------------
anyrouter/agentrouter 前面是阿里云 ESA，`/login` 有 `http_auto_ratelimit` 规则，
密集登录会被 403 封一段时间（见 anyrouter_login_ratelimit 那份记录）。所以：

  1. 优先用 metapi 里存的 session：CloakBrowser 只用无头模式打开登录页拿 WAF
     cookie，**不提交登录表单**，因此根本不碰限流规则。
  2. 只有 session 失效（读不到用户信息）且账号存了密码时，才提交登录表单拿新
     session，并立刻写回 metapi。

于是登录频率从「每 6 小时 × 账号数」降到「session 过期时才一次」（约每月），
既避开限流，又不需要人工重新导入 cookie。

OAuth（Linux.do / GitHub）注册的账号没有密码，只能走 session；它过期时会推送
通知，回来跑 add_account.sh 重新导入即可。

不修改 metapi 与 anyrouter-check-in 的任何上游文件，只复用其函数与 HTTP API。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CHECKIN_DIR = Path('/opt/anyrouter-check-in')
METAPI_ENV = Path('/opt/metapi/.env')
METAPI_DB = Path('/opt/metapi/data/hub.db')
METAPI_BASE = 'http://127.0.0.1:4000'

sys.path.insert(0, str(CHECKIN_DIR))
os.chdir(CHECKIN_DIR)

from dotenv import load_dotenv  # noqa: E402

# 必须在 import utils.notify 之前：其末行是模块级单例 notify = NotificationKit()，
# __init__ 期就用 os.getenv 读通知配置。
load_dotenv(CHECKIN_DIR / '.env')

from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402

from checkin import (  # noqa: E402
	login_with_credentials,
	parse_cookies,
	prepare_cookies,
	run_check_in_requests,
)
from utils.config import AccountConfig, AppConfig  # noqa: E402
from utils.notify import notify  # noqa: E402


# ---------------------------------------------------------------- 基础工具


def read_env_file(path: Path, key: str) -> str | None:
	if not path.exists():
		return None
	for line in path.read_text(encoding='utf-8').splitlines():
		if line.startswith(key + '='):
			return line.split('=', 1)[1].strip()
	return None


def b64url_decode(raw: str) -> bytes:
	return base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4))


def decrypt_password(cipher_text: str, secret: str) -> str | None:
	"""复现 metapi accountCredentialService.ts 的解密。

	格式 v1:iv:tag:data（base64url），AES-256-GCM，key = SHA256(secret)。
	Node 把 tag 与 ciphertext 分开存，Python 的 AESGCM 需要两者拼接。
	"""
	parts = (cipher_text or '').split(':')
	if len(parts) != 4 or parts[0] != 'v1':
		return None
	try:
		iv, tag, data = (b64url_decode(p) for p in parts[1:])
		key = hashlib.sha256(secret.encode('utf-8')).digest()
		return AESGCM(key).decrypt(iv, data + tag, None).decode('utf-8')
	except Exception as exc:
		print(f'[WARN] 密码解密失败: {type(exc).__name__}')
		return None


def metapi_api(path: str, payload=None, method=None, auth: str = ''):
	data = json.dumps(payload).encode() if payload is not None else None
	req = urllib.request.Request(
		METAPI_BASE + path,
		data=data,
		method=method or ('POST' if data else 'GET'),
		headers={'Authorization': f'Bearer {auth}', 'Content-Type': 'application/json'},
	)
	try:
		with urllib.request.urlopen(req, timeout=180) as resp:
			return resp.status, json.loads(resp.read().decode())
	except urllib.error.HTTPError as exc:
		try:
			return exc.code, json.loads(exc.read().decode())
		except Exception:
			return exc.code, {}
	except Exception as exc:
		return 0, {'error': str(exc)}


def is_login_rate_limited(domain: str) -> str | None:
	"""探测 /login 是否已被明确封禁。只在真的要提交登录表单前调用。

	能力有限：ESA 的 `http_auto_ratelimit` 实测针对浏览器请求（带 WAF cookies +
	自动化特征）—— 被封时 CloakBrowser 打开 /login 得到 403，而同一时刻普通
	curl/urlopen 请求同一路径仍是 200。所以这里只拦得住"连普通请求都 403"的
	重度封禁，轻度封禁靠 cooldown 退避兜。
	"""
	req = urllib.request.Request(
		f'{domain}/login',
		headers={'User-Agent': 'Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 '
							   '(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36'},
	)
	try:
		with urllib.request.urlopen(req, timeout=20) as resp:
			return 'HTTP 403' if resp.status == 403 else None
	except urllib.error.HTTPError as exc:
		if exc.code == 403:
			body = ''
			try:
				body = exc.read().decode('utf-8', 'ignore')[:200]
			except Exception:
				pass
			for marker in ('http_auto_ratelimit', 'ratelimit', 'been blocked'):
				if marker in body.lower():
					return f'HTTP 403 / {marker}'
			return 'HTTP 403'
		return None
	except Exception:
		return None


# ---------------------------------------------------------------- 退避


COOLDOWN_FILE = CHECKIN_DIR / '.login_cooldown.json'


def load_cooldown_state() -> dict:
	"""退避状态按 provider 分开存：{provider: {failures, ts}}。

	必须分站点记：anyrouter 被限流时若 agentrouter 仍成功，全局计数会被清零，
	下一轮又去冲击正在封锁中的 anyrouter，退避形同失效。

	注意退避只针对**登录**失败。session 模式不碰 /login，不参与退避计数。
	"""
	if not COOLDOWN_FILE.exists():
		return {}
	try:
		state = json.loads(COOLDOWN_FILE.read_text(encoding='utf-8'))
	except Exception:
		return {}
	if not isinstance(state, dict) or 'consecutive_failures' in state:
		return {}  # 旧的全局格式作废，重新累积
	return {k: v for k, v in state.items() if isinstance(v, dict)}


def save_cooldown_state(state: dict) -> None:
	if state:
		COOLDOWN_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
	else:
		COOLDOWN_FILE.unlink(missing_ok=True)


def cooldown_reason(provider: str, state: dict) -> str | None:
	"""该 provider 的登录是否还在退避中。1h → 2h → 4h → 8h → 12h（上限）。"""
	entry = state.get(provider) or {}
	fails = int(entry.get('failures', 0))
	if fails <= 0:
		return None
	wait_hours = min(2 ** (fails - 1), 12)
	elapsed = (datetime.now(timezone.utc).timestamp() - float(entry.get('ts', 0))) / 3600
	if elapsed < wait_hours:
		return f'连续 {fails} 次登录失败，退避 {wait_hours}h，还需 {wait_hours - elapsed:.1f}h'
	return None


def update_cooldown(provider: str, login_failed: bool, state: dict) -> None:
	if login_failed:
		fails = int((state.get(provider) or {}).get('failures', 0)) + 1
		state[provider] = {'failures': fails, 'ts': datetime.now(timezone.utc).timestamp()}
		print(f'[COOLDOWN] {provider} 登录失败（累计 {fails} 次），下次登录退避 '
			  f'{min(2 ** (fails - 1), 12)}h')
	elif provider in state:
		del state[provider]
		print(f'[COOLDOWN] {provider} 登录成功，退避已清零')


# ---------------------------------------------------------------- 账号读取


# metapi 的 platforms 目录里没有 agentrouter 适配器（全仓库零匹配），面板里只能把它的
# platform 填成别的。所以签到用的 provider 按站点 URL 推断，而不是按 platform 字段 ——
# URL 是用户填的真实域名，比 platform 可靠。
# 阿里云 ESA 下发的 WAF cookie 名。这些必须每次由浏览器现场获取，绝不能用存量的：
# 过期或被标记的 WAF cookie 会让请求直接吃拦截页。
WAF_COOKIE_NAMES = {'acw_tc', 'cdn_sec_tc', 'acw_sc__v2'}

KNOWN_PROVIDERS = ('agentrouter', 'anyrouter')


def resolve_provider(site_url, platform):
	"""从站点 URL / platform 推断签到脚本用的 provider 名，认不出返回 None。"""
	haystack = ('%s %s' % (site_url or '', platform or '')).lower()
	for name in KNOWN_PROVIDERS:
		if name in haystack:
			return name
	return None


def load_accounts_from_metapi(secret: str) -> list[dict]:
	"""从 metapi 读账号。只读连接，不干扰 metapi 自身的写入。

	password 与 cookies 两者都读出来（不再二选一）：运行时先试 cookies，
	失效再用 password 续期。
	"""
	conn = sqlite3.connect(f'file:{METAPI_DB}?mode=ro', uri=True, timeout=30)
	try:
		conn.row_factory = sqlite3.Row
		rows = conn.execute(
			"""
			SELECT a.id, a.username, a.access_token, a.extra_config, a.status,
			       s.platform, s.url, s.name AS site_name
			FROM accounts a JOIN sites s ON a.site_id = s.id
			WHERE a.status = 'active' AND s.status != 'disabled'
			ORDER BY a.id
			"""
		).fetchall()
	finally:
		conn.close()

	out = []
	for row in rows:
		provider = resolve_provider(row['url'], row['platform'])
		if not provider:
			continue  # 不是签到站点（OpenAI/Claude 等预设站），静默跳过

		try:
			extra = json.loads(row['extra_config'] or '{}')
		except json.JSONDecodeError:
			extra = {}
		relogin = extra.get('autoRelogin') or {}

		entry = {
			'id': row['id'],
			'username': relogin.get('username') or row['username'],
			'provider': provider,
			'api_user': extra.get('platformUserId'),
			'password': None,
			'cookies': None,
		}

		cipher = relogin.get('passwordCipher')
		if cipher:
			password = decrypt_password(cipher, secret)
			if password:
				entry['password'] = password
			else:
				print(f'[WARN] id={row["id"]} {row["username"]}: 密码解密失败'
					  f'（检查 ACCOUNT_CREDENTIAL_SECRET）')

		token = (row['access_token'] or '').strip()
		if token:
			parsed = parse_cookies(token) if '=' in token else {'session': token}
			# 只留身份 cookie，把存下来的 WAF cookie 全部丢掉。
			#
			# 这一步是必须的：prepare_cookies() 最后是 `{**waf_cookies, **user_cookies}`，
			# user_cookies 会覆盖浏览器刚拿到的新 WAF cookie。而 metapi 的 accessToken
			# 存的是完整 cookie header（含 acw_tc/cdn_sec_tc/acw_sc__v2），那批 cookie
			# 有时效、且一旦被 ESA 标记就会让请求直接吃拦截页 —— 表现为
			# 「Got WAF cookies 成功，但接口返回 HTML 而非 JSON」。带旧的反而不如不带。
			identity = {k: v for k, v in parsed.items()
						if k.lower() not in WAF_COOKIE_NAMES}
			if identity.get('session'):
				entry['cookies'] = '; '.join(f'{k}={v}' for k, v in identity.items())

		if entry['password'] or entry['cookies']:
			out.append(entry)
		else:
			print(f'[SKIP] id={row["id"]} {row["username"]}: 既没有密码也没有 session，无法签到')
	return out


def write_checkin_log(account_id: int, status: str, message: str) -> None:
	"""写 metapi 的 checkin_logs。没有对应写入 API，只能直接落库。

	metapi 用 WAL，读写可并发；短事务 + busy timeout 足够。created_at 存 UTC，
	与 metapi 自身写入的时区保持一致。
	"""
	stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
	conn = sqlite3.connect(str(METAPI_DB), timeout=30)
	try:
		conn.execute('PRAGMA busy_timeout = 30000')
		conn.execute(
			'INSERT INTO checkin_logs (account_id, status, message, created_at) VALUES (?, ?, ?, ?)',
			(account_id, status, message[:500], stamp),
		)
		conn.commit()
	except Exception as exc:
		print(f'[WARN] 写 checkin_logs 失败: {exc}')
	finally:
		conn.close()


def save_api_user(account_id: int, api_user, auth: str) -> None:
	"""把登录时拿到的 api_user 写回 metapi 的 extra_config.platformUserId。

	为什么必须存：anyrouter/agentrouter 的 /api/user/self 不带 `New-Api-User`
	请求头会直接 401。缺了它，session 模式每轮都读不到用户信息、被判定失效，
	于是每轮都退回密码登录 —— 白白去碰 /login，正是招来 ESA 限流的原因。
	存下之后 session 模式才真正生效，登录频率从「每轮」降到「session 过期时」。

	注意 metapi 的 PUT 是整体替换 extraConfig（accounts.ts 里 updates[key]=body[key]），
	所以必须先读出来合并，不能直接覆盖，否则会抹掉 autoRelogin 里的密码。
	"""
	if not api_user:
		return
	conn = sqlite3.connect(f'file:{METAPI_DB}?mode=ro', uri=True, timeout=20)
	try:
		row = conn.execute('SELECT extra_config FROM accounts WHERE id=?',
						   (account_id,)).fetchone()
	finally:
		conn.close()
	try:
		extra = json.loads((row[0] if row else None) or '{}')
	except json.JSONDecodeError:
		extra = {}
	try:
		value = int(api_user)
	except (TypeError, ValueError):
		value = api_user
	if extra.get('platformUserId') == value:
		return  # 已是最新，不必写
	extra['platformUserId'] = value
	status, _ = metapi_api(f'/api/accounts/{account_id}',
						   {'extraConfig': json.dumps(extra, ensure_ascii=False)},
						   method='PUT', auth=auth)
	print(f'[METAPI] 记住 api_user={value} → HTTP {status}（下轮 session 模式即可直接用）')


def insert_account(site_id: int, username: str, cookie_header: str,
				   api_user=None, profile: dict | None = None) -> int | None:
	"""把账号直接写进 metapi 的 accounts 表。

	为什么不走 POST /api/accounts：session 模式下 metapi 必定调
	adapter.verifyToken() 做一次纯 HTTP 验证，超时 10 秒且**硬编码**
	（manualAccountCreationService.ts:15，不是环境变量），`skipModelFetch`
	对 session 分支无效。撞上 WAF 就 throw、账号根本不会创建 —— 用户看到的
	就是「Token verification timed out (10s)」。

	而调用方在写入前已经用 CloakBrowser 拿真 WAF cookie + 真实接口验证过这份
	session 了，没有理由让 metapi 再用更弱的方式验一遍并因此失败。

	表里除 site_id / access_token 外都有默认值。余额按 metapi 自己的口径存
	（原始 quota / 500000，不做 round，保持它 6 位小数的精度）。
	"""
	balance = used = total = 0.0
	if profile:
		raw_q = profile.get('quota')
		raw_u = profile.get('used_quota', profile.get('used'))
		if isinstance(raw_q, (int, float)):
			balance = raw_q / 500000
		if isinstance(raw_u, (int, float)):
			used = raw_u / 500000
		total = balance + used

	extra: dict = {'credentialMode': 'session'}
	if api_user:
		try:
			extra['platformUserId'] = int(api_user)
		except (TypeError, ValueError):
			extra['platformUserId'] = api_user

	stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
	conn = sqlite3.connect(str(METAPI_DB), timeout=30)
	try:
		conn.execute('PRAGMA busy_timeout = 30000')
		cur = conn.execute(
			'INSERT INTO accounts (site_id, username, access_token, status, '
			'checkin_enabled, balance, balance_used, quota, extra_config, '
			'created_at, updated_at, sort_order) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
			(site_id, username, cookie_header, 'active',
			 # metapi 内置签到不可靠、已全局关闭；签到由本桥接负责，
			 # 桥接是按 accounts.status 判断的，不看 checkin_enabled
			 0, balance, used, total,
			 json.dumps(extra, ensure_ascii=False), stamp, stamp, 0),
		)
		conn.commit()
		new_id = cur.lastrowid
	except Exception as exc:
		print(f'[FAILED] 写入 accounts 失败: {exc}')
		return None
	finally:
		conn.close()

	# 直接写库绕过了 metapi 的写入路径，它的 accounts 列表走 adminSnapshotStore 缓存，
	# 不刷新的话面板上看不到这条新账号。GET /api/accounts?refresh=1 是它自带的
	# 强制重建开关（accounts.ts:487 的 forceRefresh）。
	auth = read_env_file(METAPI_ENV, 'AUTH_TOKEN')
	if auth:
		status, _ = metapi_api('/api/accounts?refresh=1', auth=auth)
		print(f'[METAPI] 刷新 accounts 快照 → HTTP {status}')
	return new_id


def sync_env_snapshot(accounts: list[dict]) -> None:
	"""把 metapi 的账号同步进 .env，保证原生 run.sh 作为 fallback 始终可用。

	metapi 是唯一维护入口；.env 只是它的快照。有密码就写密码（更持久），
	否则写 session cookie。
	"""
	env_path = CHECKIN_DIR / '.env'
	snapshot = []
	for acct in accounts:
		item: dict = {'name': acct['username']}
		if acct.get('password'):
			item['email'] = acct['username']
			item['password'] = acct['password']
		else:
			item['cookies'] = acct['cookies']
		if acct.get('api_user'):
			item['api_user'] = str(acct['api_user'])
		if acct['provider'] != 'anyrouter':
			item['provider'] = acct['provider']
		snapshot.append(item)

	line = 'ANYROUTER_ACCOUNTS=' + json.dumps(snapshot, ensure_ascii=False, separators=(',', ':'))
	lines = env_path.read_text(encoding='utf-8').splitlines()
	for i, existing in enumerate(lines):
		if existing.startswith('ANYROUTER_ACCOUNTS='):
			if existing == line:
				return  # 无变化，不重写文件
			lines[i] = line
			break
	else:
		lines.append(line)
	env_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
	os.chmod(env_path, 0o600)
	print(f'[SYNC] .env fallback 快照已更新（{len(snapshot)} 个账号）')


def clear_browser_profile(account_name: str, provider: str) -> None:
	"""清掉某账号的持久化 profile，使下次强制走完整登录。

	上游在 /api/user/self 验证失败后直接返回、不 fallback 重登（checkin.py:206-214），
	profile 里一旦存了失效 session，is_logged_in() 会误判已登录并跳过登录表单，
	该账号就永久卡死。
	"""
	base = Path(os.getenv('CHECKIN_BROWSER_PROFILE_DIR', '.browser_profiles'))
	target = base / provider / account_name
	if target.is_dir():
		import shutil

		shutil.rmtree(target, ignore_errors=True)
		print(f'[SELF-HEAL] 已清除 profile，下次强制完整登录: {target}')


# ---------------------------------------------------------------- 签到两条路


def build_account_cfg(acct: dict, provider_name: str, index: int, use_password: bool):
	cfg: dict = {'name': acct['username'], 'provider': provider_name}
	if use_password:
		cfg['email'] = acct['username']
		cfg['password'] = acct['password']
	else:
		cfg['cookies'] = acct['cookies']
	if acct.get('api_user'):
		cfg['api_user'] = str(acct['api_user'])
	return AccountConfig.from_dict(cfg, index)


async def attempt_with_session(acct: dict, provider_name, provider_config, index: int):
	"""用 metapi 里存的 session 签到。

	只让浏览器（无头）打开登录页拿 WAF cookie，不提交登录表单，因此不触发
	/login 的限流规则。返回 (ok, before, after, cookies)，session 不可用则 None。
	"""
	if not acct.get('cookies'):
		return None
	user_cookies = parse_cookies(acct['cookies'])
	if not user_cookies or 'session' not in user_cookies:
		return None

	name = acct['username']
	print(f'[TRY] {name}: 用已存 session 签到（不提交登录表单，不碰限流）')
	all_cookies = await prepare_cookies(name, provider_config, user_cookies)
	if not all_cookies:
		print(f'[WARN] {name}: 拿不到 WAF cookie')
		return None

	api_user = acct.get('api_user')
	ok, before, after = run_check_in_requests(
		all_cookies, build_account_cfg(acct, provider_name, index, use_password=False),
		name, provider_config,
		api_user_override=str(api_user) if api_user else None,
		use_proxy=provider_config.use_proxy,
	)
	if not (after and after.get('success')):
		print(f'[INFO] {name}: session 已失效（读不到用户信息）')
		return None
	return ok, before, after, all_cookies


async def attempt_with_password(acct: dict, provider_name, provider_config, index: int):
	"""提交登录表单拿新 session 并签到。会碰 /login，仅在 session 失效时调用。"""
	name = acct['username']
	print(f'[TRY] {name}: session 不可用，用密码重新登录续期')
	login = await login_with_credentials(
		name, provider_config, provider_name, acct['username'], acct['password']
	)
	if not login or not login.cookies:
		print(f'[FAILED] {name}: 浏览器登录失败')
		clear_browser_profile(name, provider_name)
		return None

	all_cookies = dict(login.cookies)
	print(f'[SUCCESS] {name}: 登录成功，拿到 {len(all_cookies)} 个 cookie')
	api_user = login.api_user or acct.get('api_user')
	ok, before, after = run_check_in_requests(
		all_cookies, build_account_cfg(acct, provider_name, index, use_password=True),
		name, provider_config,
		api_user_override=str(api_user) if api_user else None,
		use_proxy=provider_config.use_proxy,
	)
	# 把登录期间拿到的 api_user 带回主流程，供写回 metapi
	acct['_login_api_user'] = api_user
	return ok, before, after, all_cookies


# ---------------------------------------------------------------- 主流程


async def main() -> int:
	auth = read_env_file(METAPI_ENV, 'AUTH_TOKEN')
	secret = read_env_file(METAPI_ENV, 'ACCOUNT_CREDENTIAL_SECRET') or auth
	if not auth or not secret:
		print('[FAILED] 读不到 metapi 的 AUTH_TOKEN / ACCOUNT_CREDENTIAL_SECRET')
		return 1

	accounts = load_accounts_from_metapi(secret)
	if not accounts:
		print('[FAILED] metapi 里没有可签到的账号')
		return 1
	with_pw = sum(1 for a in accounts if a.get('password'))
	with_ck = sum(1 for a in accounts if a.get('cookies'))
	print(f'[INFO] 从 metapi 读到 {len(accounts)} 个账号'
		  f'（有密码 {with_pw} 个，有 session {with_ck} 个）')

	sync_env_snapshot(accounts)
	app_config = AppConfig.load_from_env()
	cooldown_state = load_cooldown_state()

	# 按 provider 分组：退避和限流都是站点级的，一个站被封不该拖累另一个站
	grouped: dict[str, list[dict]] = {}
	for acct in accounts:
		grouped.setdefault(acct['provider'], []).append(acct)

	notify_lines: list[str] = []
	total_attempted = total_success = 0

	for provider_name, group in grouped.items():
		print(f'\n=== provider: {provider_name}（{len(group)} 个账号）===')
		provider_config = app_config.get_provider(provider_name)
		if not provider_config:
			print(f'[FAILED] 签到脚本里没有 provider={provider_name} 的配置，跳过')
			continue

		# 登录相关状态：本组内共享，惰性求值 —— 只有真需要登录时才去探限流
		login_blocked: str | None = None
		login_probed = False
		login_failed_any = False
		login_used = False

		for index, acct in enumerate(group):
			name = acct['username']
			total_attempted += 1
			print(f'\n[PROCESSING] {name} (metapi id={acct["id"]})')

			# 阶段 1：已存 session 优先（零登录请求）
			outcome = await attempt_with_session(acct, provider_name, provider_config, index)
			via = 'session'

			# 阶段 2：session 不行且有密码 → 登录续期
			if outcome is None and acct.get('password'):
				reason = cooldown_reason(provider_name, cooldown_state)
				if reason:
					print(f'[SKIP] {name}: 需要重新登录，但 {reason}')
					write_checkin_log(acct['id'], 'failed', f'需重新登录但处于退避中：{reason}')
					notify_lines.append(f'[SKIP] {name}: 需重登，退避中')
					continue
				if not login_probed:
					login_blocked = is_login_rate_limited(provider_config.domain)
					login_probed = True
				if login_blocked:
					print(f'[SKIP] {name}: 需要重新登录，但 /login 已被封禁（{login_blocked}）')
					write_checkin_log(acct['id'], 'failed', f'需重新登录但登录被封禁：{login_blocked}')
					notify_lines.append(f'[SKIP] {name}: 需重登，登录被封禁')
					login_failed_any = True
					continue
				login_used = True
				outcome = await attempt_with_password(acct, provider_name, provider_config, index)
				via = 'password'
				if outcome is None:
					login_failed_any = True

			if outcome is None:
				hint = 'session 已过期，需重新导入（该账号无密码）' if not acct.get('password') \
					else '登录失败'
				print(f'[FAILED] {name}: {hint}')
				write_checkin_log(acct['id'], 'failed', hint)
				notify_lines.append(f'[FAIL] {name}: {hint}')
				continue

			ok, before, after, all_cookies = outcome

			# 新鲜 cookies 写回 metapi：它存的 accessToken 含会过期的 WAF cookies，
			# 刷新后面板余额刷新与 /v1 网关才能继续工作
			cookie_header = '; '.join(f'{k}={v}' for k, v in all_cookies.items())
			st, _ = metapi_api(f'/api/accounts/{acct["id"]}',
							   {'accessToken': cookie_header}, method='PUT', auth=auth)
			print(f'[METAPI] 更新 accessToken → HTTP {st}（凭据来源: {via}）')

			# 登录才能拿到 api_user；存下来后续 session 模式才不会 401
			if via == 'password' and acct.get('_login_api_user'):
				save_api_user(acct['id'], acct['_login_api_user'], auth)

			reward = None
			if before and after and before.get('success') and after.get('success'):
				reward = round(after['quota'] - before['quota'], 2)
			msg = ('签到成功' + (f'，获得 ${reward}' if reward else '（余额无变化，今日已签到）')) if ok \
				else '签到失败'
			write_checkin_log(acct['id'], 'success' if ok else 'failed', f'{msg}｜凭据={via}')

			line = f'{"[OK]" if ok else "[FAIL]"} {name}: 余额 ${after["quota"]:.2f}'
			if reward:
				line += f'（+${reward}）'
			notify_lines.append(line)
			if ok:
				total_success += 1

		# 退避只反映登录结果：session 模式不碰 /login，不该影响它
		if login_used or login_failed_any:
			update_cooldown(provider_name, login_failed_any, cooldown_state)

	save_cooldown_state(cooldown_state)

	if total_success:
		st, _ = metapi_api('/api/accounts?refresh=1', auth=auth)
		print(f'\n[METAPI] 触发余额刷新 → HTTP {st}')

	print(f'\n[STATS] 成功 {total_success}/{total_attempted}')

	if total_attempted and total_success < total_attempted:
		body = f'成功 {total_success}/{total_attempted}\n\n' + '\n'.join(notify_lines)
		if total_success == 0:
			body += ('\n\n本轮全失败。若提示需重登/被封禁，是 ESA 的 http_auto_ratelimit '
					 '限流，已按站点自动退避，下轮延后重试，无需干预。'
					 '若提示 session 过期且该账号无密码（OAuth 注册），需跑 add_account.sh 重新导入。')
		notify.push_message('AnyRouter 签到（metapi 桥接）', body, msg_type='text')
		print('[NOTIFY] 有失败，已推送通知')
	else:
		print('[INFO] 全部成功，跳过通知')

	return 0 if total_success > 0 else 1


if __name__ == '__main__':
	import asyncio

	try:
		sys.exit(asyncio.run(main()))
	except KeyboardInterrupt:
		sys.exit(1)
	except Exception as exc:
		print(f'[FAILED] 桥接脚本异常: {exc}')
		import traceback

		traceback.print_exc()
		sys.exit(1)
