#!/usr/bin/env python3
"""签到运维后端 —— 以「注入」方式融合进 metapi 页面，不再有独立面板页。

架构（对外只暴露一个端口 4000）
------------------------------
    公网 :4000  →  nginx
                    ├─ /__checkin/*  →  本服务 127.0.0.1:4100
                    └─ /*            →  metapi 127.0.0.1:4001
                                        HTML 响应经 sub_filter 注入
                                        <script src="/__checkin/inject.js">

于是 metapi 页面上直接多出「签到运维」面板，而 **metapi 源码一行没改** ——
改它意味着 fork + 改 TypeScript + 重建 arm64 镜像，且每次上游更新都要 rebase。

认证复用 metapi 的登录态：它把令牌明文存在 localStorage 的 `auth_token`
（src/web/authSession.ts），注入脚本读同一个 key，所以你在 metapi 登录后
这里就直接可用，不需要再输一次令牌。

只用 Python 标准库。
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CHECKIN_DIR = Path('/opt/anyrouter-check-in')
sys.path.insert(0, str(CHECKIN_DIR))
os.chdir(CHECKIN_DIR)

import metapi_bridge as mb  # noqa: E402

PORT = int(os.environ.get('PANEL_PORT', '4100'))
PREFIX = '/__checkin'
IMPORT_STATE_FILE = Path('/tmp/panel_import_state.json')
IMPORT_LOG = Path('/tmp/panel_import.log')
SESSION_INPUT = Path('/tmp/panel_session_input.txt')
CHECKIN_LOG = Path('/tmp/panel_checkin.log')

AUTH = mb.read_env_file(mb.METAPI_ENV, 'AUTH_TOKEN') or ''

_jobs: dict[str, subprocess.Popen] = {}
_lock = threading.Lock()


# ------------------------------------------------------------------ 数据


def db_rows(sql: str, args=()) -> list[dict]:
	conn = sqlite3.connect(f'file:{mb.METAPI_DB}?mode=ro', uri=True, timeout=20)
	try:
		conn.row_factory = sqlite3.Row
		return [dict(r) for r in conn.execute(sql, args)]
	finally:
		conn.close()


def gather_status() -> dict:
	accounts = []
	for row in db_rows(
			'SELECT a.id, a.username, a.status, a.balance, a.balance_used, '
			'a.last_checkin_at, a.extra_config, a.access_token, s.name AS site_name, s.url '
			'FROM accounts a JOIN sites s ON a.site_id = s.id ORDER BY a.id'):
		provider = mb.resolve_provider(row['url'], None)
		if not provider:
			continue
		try:
			extra = json.loads(row['extra_config'] or '{}')
		except Exception:
			extra = {}
		accounts.append({
			'id': row['id'], 'username': row['username'], 'site': row['site_name'],
			'provider': provider, 'status': row['status'],
			'balance': row['balance'], 'used': row['balance_used'],
			'last_checkin': row['last_checkin_at'],
			'has_password': bool((extra.get('autoRelogin') or {}).get('passwordCipher')),
			'has_session': 'session=' in (row['access_token'] or ''),
			'api_user': extra.get('platformUserId'),
		})

	cooldown = {}
	try:
		if mb.COOLDOWN_FILE.exists():
			raw = json.loads(mb.COOLDOWN_FILE.read_text(encoding='utf-8'))
			for prov, entry in raw.items():
				if isinstance(entry, dict):
					cooldown[prov] = mb.cooldown_reason(prov, raw)
	except Exception:
		pass

	timer = subprocess.run(
		['systemctl', 'list-timers', 'anyrouter-checkin.timer', '--no-pager'],
		capture_output=True, text=True).stdout.splitlines()

	with _lock:
		running = {k: (p.poll() is None) for k, p in _jobs.items()}

	return {
		'accounts': accounts,
		'sites': [{'id': r['id'], 'name': r['name'], 'url': r['url']}
				  for r in db_rows('SELECT id, name, url FROM sites ORDER BY id')
				  if mb.resolve_provider(r['url'], None)],
		'logs': db_rows('SELECT account_id, status, message, created_at FROM checkin_logs '
						'ORDER BY id DESC LIMIT 15'),
		'cooldown': cooldown,
		'next_run': timer[1].strip() if len(timer) > 1 else '',
		'running': running,
	}


def start_job(key: str, argv: list[str], log: Path, env_extra: dict | None = None) -> dict:
	with _lock:
		old = _jobs.get(key)
		if old and old.poll() is None:
			return {'ok': False, 'message': '该任务正在运行中'}
		env = {**os.environ, 'PATH': '/root/.local/bin:' + os.environ.get('PATH', ''),
			   'PYTHONUNBUFFERED': '1', 'PYTHONIOENCODING': 'utf-8'}
		if env_extra:
			env.update(env_extra)
		fh = log.open('w', encoding='utf-8')
		proc = subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT,
								stdin=subprocess.DEVNULL, env=env, cwd=str(CHECKIN_DIR),
								start_new_session=True)
		_jobs[key] = proc
		return {'ok': True, 'pid': proc.pid}


def tail(path: Path, lines: int = 200) -> str:
	if not path.exists():
		return ''
	try:
		return '\n'.join(path.read_text(encoding='utf-8', errors='ignore').splitlines()[-lines:])
	except Exception:
		return ''


def read_state(path: Path) -> dict:
	try:
		if path.exists():
			return json.loads(path.read_text(encoding='utf-8'))
	except Exception:
		pass
	return {}


# ------------------------------------------------------------------ 注入脚本


INJECT_JS = r'''(function(){
if (window.__checkinPanelLoaded) return;
window.__checkinPanelLoaded = true;

var P = '/__checkin';
function token(){ return (localStorage.getItem('auth_token')||'').trim(); }
async function api(path, opts){
  var t = token();
  if(!t) throw new Error('no-token');
  var r = await fetch(P+path, Object.assign({headers:{
    'Authorization':'Bearer '+t, 'Content-Type':'application/json'}}, opts||{}));
  if(!r.ok) throw new Error('http-'+r.status);
  return r.json();
}
function esc(s){ var d=document.createElement('div'); d.textContent=(s==null?'':s); return d.innerHTML; }
function mask(u){ if(!u) return ''; var i=u.indexOf('@');
  return i>0 ? u.slice(0,2)+'***'+u.slice(i) : u; }

// Shadow DOM 隔离样式，避免和 metapi 自己的 CSS 互相影响
var host = document.createElement('div');
host.id = '__checkin_host';
host.style.cssText = 'position:fixed;right:18px;bottom:18px;z-index:2147483000';
document.body.appendChild(host);
var sr = host.attachShadow({mode:'open'});
sr.innerHTML = `
<style>
:host{all:initial}
*{box-sizing:border-box;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.fab{background:#4c8dff;color:#fff;border:0;border-radius:24px;padding:11px 18px;font-size:13px;
cursor:pointer;box-shadow:0 6px 22px rgba(0,0,0,.35);font-weight:600}
.fab:hover{filter:brightness(1.1)}
.panel{display:none;position:fixed;right:18px;bottom:70px;width:min(680px,calc(100vw - 36px));
max-height:calc(100vh - 110px);overflow:auto;background:#181b22;color:#e6e8ee;
border:1px solid #2b313d;border-radius:12px;padding:16px;box-shadow:0 18px 50px rgba(0,0,0,.5);
font-size:13px;line-height:1.6}
.panel.open{display:block}
h3{margin:0 0 4px;font-size:14px}
h4{margin:16px 0 8px;font-size:12px;color:#9aa3b2;text-transform:uppercase;letter-spacing:.06em}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #262b36}
th{color:#9aa3b2;font-weight:500}
button{background:#4c8dff;color:#fff;border:0;border-radius:6px;padding:7px 12px;font-size:12px;cursor:pointer}
button.ghost{background:transparent;border:1px solid #333a47;color:#e6e8ee}
button:disabled{opacity:.45;cursor:not-allowed}
input,select{background:#0d1017;color:#e6e8ee;border:1px solid #2b313d;border-radius:6px;
padding:7px 9px;font-size:12px;font-family:inherit}
pre{background:#0a0c10;border:1px solid #262b36;border-radius:6px;padding:10px;overflow:auto;
max-height:200px;font-size:11px;margin:8px 0 0;font-family:ui-monospace,Menlo,monospace;white-space:pre-wrap}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.ok{color:#3fb950}.bad{color:#f85149}.warn{color:#d29922}.dim{color:#9aa3b2}
.badge{display:inline-block;padding:0 6px;border-radius:20px;font-size:10px;border:1px solid #333a47}
.close{float:right;background:transparent;border:0;color:#9aa3b2;font-size:18px;cursor:pointer;padding:0 4px}
.hint{font-size:11px;color:#9aa3b2;margin:6px 0}
</style>
<button class="fab" id="fab">签到运维</button>
<div class="panel" id="panel">
  <button class="close" id="close">×</button>
  <h3>签到运维</h3>
  <div class="dim" id="meta"></div>

  <h4>账号</h4>
  <table id="accts"></table>

  <h4>签到</h4>
  <div class="row"><button id="btnCk">立即签到</button>
    <button class="ghost" id="btnCkLog">刷新输出</button>
    <span class="dim" id="ckState"></span></div>
  <pre id="ckLog"></pre>

  <h4>导入账号（session）</h4>
  <div class="hint">在你自己浏览器里登录该站点 → F12 → Application → Cookies →
    复制 <b>session</b> 的值粘到这里。WAF 挑战由服务器上的真浏览器解决，
    用户名和 api_user 会自动读出来。</div>
  <div class="row">
    <select id="isite"></select>
    <input id="isess" type="password" placeholder="粘贴 session" style="flex:1;min-width:180px">
    <input id="iuser" placeholder="api_user（可空）" style="width:120px">
    <button id="btnImp">导入</button>
    <span class="dim" id="impState"></span>
  </div>
  <pre id="impLog"></pre>

  <h4>签到日志</h4>
  <table id="logs"></table>
</div>`;

var $ = function(id){ return sr.getElementById(id); };
var open = false;
$('fab').onclick = function(){ open=!open; $('panel').classList.toggle('open', open);
  if(open) refresh(); };
$('close').onclick = function(){ open=false; $('panel').classList.remove('open'); };

async function refresh(){
  var s;
  try{ s = await api('/status'); }
  catch(e){ $('meta').textContent = (e.message==='no-token')
    ? '请先在 metapi 登录（本面板复用同一个令牌）' : '读取状态失败：'+e.message; return; }
  var total = s.accounts.reduce(function(a,x){ return a+(x.balance||0); },0);
  var cd = Object.keys(s.cooldown||{}).length
    ? Object.entries(s.cooldown).map(function(kv){ return '<span class="warn">'+esc(kv[0])+' 退避中</span>'; }).join(' ')
    : '<span class="ok">无退避</span>';
  $('meta').innerHTML = s.accounts.length+' 个账号 · 总余额 $'+total.toFixed(2)+' · '+cd+
    (s.next_run ? '<br><span class="dim">'+esc(s.next_run)+'</span>' : '');

  $('accts').innerHTML = '<tr><th>ID</th><th>账号</th><th>站点</th><th>余额</th><th>凭据</th><th>上次签到</th></tr>'+
    s.accounts.map(function(a){ return '<tr><td>'+a.id+'</td><td>'+esc(mask(a.username))+
      '</td><td>'+esc(a.site)+'</td><td>$'+(a.balance||0).toFixed(2)+'</td><td>'+
      (a.has_password?'<span class="badge ok">密码</span> ':'')+
      (a.has_session?'<span class="badge">session</span>':'')+'</td><td class="dim">'+
      esc(a.last_checkin||'—')+'</td></tr>'; }).join('');

  $('logs').innerHTML = '<tr><th>时间</th><th>账号</th><th>结果</th><th>信息</th></tr>'+
    s.logs.map(function(l){ return '<tr><td class="dim">'+esc(l.created_at)+'</td><td>'+
      l.account_id+'</td><td class="'+(l.status==='success'?'ok':'bad')+'">'+esc(l.status)+
      '</td><td>'+esc(l.message)+'</td></tr>'; }).join('');

  var sel = $('isite');
  if(sel.options.length !== s.sites.length){
    sel.innerHTML = s.sites.map(function(x){ return '<option value="'+x.id+'">'+
      esc(x.name)+'</option>'; }).join('');
  }
  $('btnCk').disabled = !!(s.running||{}).checkin;
  $('btnImp').disabled = !!(s.running||{}).import;
  $('ckState').textContent = (s.running||{}).checkin ? '运行中…' : '';
}

$('btnCk').onclick = async function(){ await api('/checkin', {method:'POST'});
  $('ckState').textContent='已启动…'; setTimeout(loadCkLog, 1500); };
$('btnCkLog').onclick = loadCkLog;
async function loadCkLog(){ var r = await api('/checkin/log');
  $('ckLog').textContent = r.log || '(暂无输出)'; $('ckLog').scrollTop = 1e6; }

$('btnImp').onclick = async function(){
  var sess = $('isess').value.trim();
  if(!sess){ $('impState').textContent='请先粘贴 session'; return; }
  $('impState').textContent='提交中…';
  await api('/import', {method:'POST', body:JSON.stringify({
    site_id: Number($('isite').value), session: sess,
    api_user: $('iuser').value.trim() || null })});
  $('isess').value = '';
};

setInterval(function(){
  if(!open || !token()) return;
  api('/import/status').then(function(r){
    var st = r.state||{};
    $('impState').textContent = st.state ? (st.state+' — '+(st.message||'')) : '';
    if(r.log){ $('impLog').textContent = r.log; $('impLog').scrollTop = 1e6; }
    if(st.state==='done') refresh();
  }).catch(function(){});
}, 3000);
setInterval(function(){ if(open && token()) refresh(); }, 10000);
})();'''


# ------------------------------------------------------------------ HTTP


class Handler(BaseHTTPRequestHandler):
	server_version = 'CheckinPanel/2.0'

	def log_message(self, *a):
		pass

	def _authed(self) -> bool:
		got = (self.headers.get('Authorization') or '').removeprefix('Bearer ').strip()
		return bool(AUTH) and got == AUTH

	def _send(self, code: int, body: bytes, ctype: str) -> None:
		self.send_response(code)
		self.send_header('Content-Type', ctype)
		self.send_header('Content-Length', str(len(body)))
		self.send_header('Cache-Control', 'no-store')
		self.end_headers()
		self.wfile.write(body)

	def _json(self, obj, code: int = 200) -> None:
		self._send(code, json.dumps(obj, ensure_ascii=False).encode(),
				   'application/json; charset=utf-8')

	def _path(self) -> str:
		p = self.path.split('?', 1)[0]
		return p[len(PREFIX):] if p.startswith(PREFIX) else p

	def do_GET(self):
		path = self._path()
		# inject.js 不鉴权：它本身不含任何数据，只是 UI 代码，数据全走鉴权 API
		if path == '/inject.js':
			self._send(200, INJECT_JS.encode(), 'application/javascript; charset=utf-8')
			return
		if not self._authed():
			self._json({'error': 'unauthorized'}, 401)
			return
		if path == '/status':
			self._json(gather_status())
		elif path == '/checkin/log':
			self._json({'log': tail(CHECKIN_LOG)})
		elif path == '/import/status':
			self._json({'state': read_state(IMPORT_STATE_FILE), 'log': tail(IMPORT_LOG, 60)})
		else:
			self._json({'error': 'not found'}, 404)

	def do_POST(self):
		if not self._authed():
			self._json({'error': 'unauthorized'}, 401)
			return
		path = self._path()
		length = int(self.headers.get('Content-Length') or 0)
		try:
			payload = json.loads(self.rfile.read(length) or b'{}')
		except Exception:
			payload = {}

		if path.startswith('/ext/'):
			# metapi 的 AnyRouterAdapter 转发过来的 RPC：用真浏览器过 WAF。
			# 这些调用要起浏览器，耗时 20-60 秒，metapi 侧的超时已相应放大。
			import ext_api
			self._json(ext_api.handle(path[len('/ext/'):], payload))
		elif path == '/checkin':
			self._json(start_job('checkin', [str(CHECKIN_DIR / 'run_bridge.sh')], CHECKIN_LOG))
		elif path == '/import':
			site_id = int(payload.get('site_id') or 0)
			session_value = (payload.get('session') or '').strip()
			if not site_id or not session_value:
				self._json({'ok': False, 'message': '缺少 site_id 或 session'}, 400)
				return
			# session 经文件传给子进程，不走命令行参数（否则会出现在 ps 输出里）
			SESSION_INPUT.write_text(session_value, encoding='utf-8')
			SESSION_INPUT.chmod(0o600)
			IMPORT_STATE_FILE.write_text(json.dumps({'state': 'starting'}), encoding='utf-8')
			# 有头 Chromium 需要 X 显示：新版阿里云 WAF 挑战（aliyun_waf_aa/bb）
			# 必须执行 JS 才能过，纯 HTTP 过不去
			argv = ['xvfb-run', '-a', '--server-args=-screen 0 1440x900x24',
					'/root/.local/bin/uv', 'run', '--with', 'cryptography', 'python',
					'import_session.py', '--site-id', str(site_id),
					'--session-file', str(SESSION_INPUT)]
			if payload.get('api_user'):
				argv += ['--api-user', str(payload['api_user'])]
			self._json(start_job('import', argv, IMPORT_LOG,
								 {'IMPORT_SESSION_STATE_FILE': str(IMPORT_STATE_FILE)}))
		else:
			self._json({'error': 'not found'}, 404)


def bridge_ips() -> list[str]:
	"""docker 网桥地址。metapi 是容器，只绑回环它连不上。"""
	out = []
	try:
		res = subprocess.run(['ip', '-4', '-o', 'addr', 'show'],
							 capture_output=True, text=True, timeout=10)
		for line in res.stdout.splitlines():
			parts = line.split()
			if len(parts) >= 4 and parts[1].startswith(('docker', 'br-')):
				out.append(parts[3].split('/')[0])
	except Exception:
		pass
	return sorted(set(out))


if __name__ == '__main__':
	if not AUTH:
		print('读不到 metapi 的 AUTH_TOKEN，无法鉴权，退出')
		sys.exit(1)

	addrs = ['127.0.0.1'] + bridge_ips()
	servers = []
	for addr in addrs:
		try:
			srv = ThreadingHTTPServer((addr, PORT), Handler)
		except OSError as exc:
			print(f'  跳过 {addr}:{PORT} ({exc})')
			continue
		threading.Thread(target=srv.serve_forever, daemon=True).start()
		servers.append(addr)
	if not servers:
		print('没有可用监听地址，退出')
		sys.exit(1)
	print(f'签到运维后端 {PREFIX}/* 监听: {", ".join(servers)}:{PORT}')
	print('  （不绑 0.0.0.0：该端口在云安全列表里是放行的，绑通配等于对公网开放）')
	try:
		threading.Event().wait()
	except KeyboardInterrupt:
		pass
