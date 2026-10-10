#!/usr/bin/env python3
"""从节点信息生成 mihomo 配置并起服务。

为什么需要手动喂节点：机场的订阅站挂在 Cloudflare 后面，会把数据中心 IP 判定成
「代理/VPN」直接 403（换任何 UA、以及绕过转换服务直连原始订阅都一样）。所以这台
服务器拉不到订阅，只能由你在本地拿到节点后喂进来。

支持两种输入（都从文件读，不走命令行参数，避免出现在 ps 里）：
  * 客户端导出的 JSON（Shadowrocket / Quantumult 格式，单个或数组）
  * ss:// 链接，一行一个（sip002 格式）
  * clash 的 proxies YAML 片段（`- {name: ..., type: ss, ...}` 那几行）

用法: setup_proxy.py --nodes-file FILE [--port 7890]
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

from paths import MIHOMO_CONFIG as CONFIG
from paths import MIHOMO_DIR


def b64pad(raw: str) -> bytes:
	raw = raw.strip().replace('-', '+').replace('_', '/')
	return base64.b64decode(raw + '=' * (-len(raw) % 4))


def parse_ss(link: str) -> dict | None:
	"""解析 ss:// 链接（sip002 与旧式 base64 两种都支持）。"""
	link = link.strip()
	if not link.startswith('ss://'):
		return None
	body = link[5:]
	name = ''
	if '#' in body:
		body, frag = body.split('#', 1)
		name = urllib.parse.unquote(frag)
	body = body.split('?', 1)[0]  # 丢掉 plugin 等参数

	try:
		if '@' in body:
			# sip002: base64(method:password)@host:port
			userinfo, hostport = body.rsplit('@', 1)
			try:
				decoded = b64pad(userinfo).decode('utf-8')
			except Exception:
				decoded = urllib.parse.unquote(userinfo)
			method, password = decoded.split(':', 1)
		else:
			# 旧式: base64(method:password@host:port)
			decoded = b64pad(body).decode('utf-8')
			userinfo, hostport = decoded.rsplit('@', 1)
			method, password = userinfo.split(':', 1)
		host, port = hostport.rsplit(':', 1)
		return {'name': name or f'{host}:{port}', 'type': 'ss', 'server': host,
				'port': int(port), 'cipher': method, 'password': password, 'udp': True}
	except Exception as exc:
		print(f'  [WARN] 解析失败（{type(exc).__name__}）: {link[:40]}...')
		return None


def parse_json_nodes(text: str) -> list[dict]:
	"""解析客户端导出的 JSON 节点（Shadowrocket / Quantumult 那种格式）。

	字段名和 clash 不一样，需要映射：host→server、peer→sni（Trojan 的 SNI）、
	allowInsecure→skip-cert-verify、title→name、type 大写→小写。
	支持单个对象或数组。
	"""
	try:
		data = json.loads(text)
	except json.JSONDecodeError:
		return []
	items = data if isinstance(data, list) else [data]
	out = []
	for item in items:
		if not isinstance(item, dict):
			continue
		kind = str(item.get('type') or '').lower()
		server = item.get('host') or item.get('server') or item.get('address')
		port = item.get('port')
		if not server or not port:
			continue
		node: dict = {
			'name': item.get('title') or item.get('name') or f'{server}:{port}',
			'server': str(server),
			'port': int(port),
		}
		if kind == 'trojan':
			node['type'] = 'trojan'
			node['password'] = str(item.get('password') or '')
			# Trojan 的 SNI 在这类导出里叫 peer
			if item.get('peer'):
				node['sni'] = str(item['peer'])
		elif kind in ('ss', 'shadowsocks'):
			node['type'] = 'ss'
			node['cipher'] = str(item.get('method') or 'aes-256-gcm')
			node['password'] = str(item.get('password') or '')
		elif kind == 'vmess':
			node['type'] = 'vmess'
			node['uuid'] = str(item.get('uuid') or item.get('password') or '')
			node['alterId'] = int(item.get('alterId') or 0)
			node['cipher'] = 'auto'
			if item.get('peer'):
				node['servername'] = str(item['peer'])
		else:
			print(f"  [WARN] 暂不支持的节点类型: {item.get('type')} ({node['name']})")
			continue
		if item.get('udp'):
			node['udp'] = True
		if item.get('allowInsecure'):
			node['skip-cert-verify'] = True
		out.append(node)
	return out


def parse_yaml_nodes(text: str) -> list[dict]:
	"""从 clash proxies 片段里粗解析节点（只认扁平的 {k: v} 形式，够用）。"""
	out = []
	for line in text.splitlines():
		line = line.strip()
		if not line.startswith('-') or '{' not in line:
			continue
		inner = line[line.index('{') + 1: line.rindex('}')] if '}' in line else ''
		node: dict = {}
		for part in re.split(r',\s*(?=[A-Za-z_-]+\s*:)', inner):
			if ':' not in part:
				continue
			k, v = part.split(':', 1)
			k, v = k.strip(), v.strip().strip('"').strip("'")
			if v.isdigit():
				node[k] = int(v)
			elif v in ('true', 'false'):
				node[k] = (v == 'true')
			else:
				node[k] = v
		if node.get('server') and node.get('type'):
			node.setdefault('name', f"{node['server']}:{node.get('port')}")
			out.append(node)
	return out


def yaml_quote(value) -> str:
	if isinstance(value, bool):
		return 'true' if value else 'false'
	if isinstance(value, int):
		return str(value)
	return json.dumps(str(value), ensure_ascii=False)


def docker_bridge_ips() -> list[str]:
	"""找出本机 docker 网桥地址，供容器内访问代理。

	为什么需要：metapi 是容器。代理只绑 127.0.0.1 的话它连不到（实测
	容器→网关:7890 不通），于是 metapi 所有出站都走宿主机那个被 anyrouter
	封掉的直连 IP —— 它的 token 验证和余额刷新就必然失败（面板里 anyrouter
	余额一直不动就是这个原因）。

	只绑网桥地址、不绑 0.0.0.0：7890 在甲骨文安全列表里是放行的，绑通配
	等于对公网开了个开放代理，会被滥用。
	"""
	ips = []
	try:
		res = subprocess.run(['ip', '-4', '-o', 'addr', 'show'],
							 capture_output=True, text=True, timeout=10)
		for line in res.stdout.splitlines():
			parts = line.split()
			if len(parts) < 4:
				continue
			iface, cidr = parts[1], parts[3]
			if iface.startswith(('docker', 'br-')):
				ips.append(cidr.split('/')[0])
	except Exception:
		pass
	return sorted(set(ips))


def build_config(nodes: list[dict], port: int) -> str:
	lines = [
		'# mihomo 配置（由 setup_proxy.py 生成）。',
		'# 只给签到用：mixed-port 绑回环，不做全局代理，',
		'# 签到脚本通过 .env 的 CHECKIN_PROXY_URL 指向它。',
		f'mixed-port: {port}',
		'bind-address: 127.0.0.1',
		'allow-lan: false',
		'mode: rule',
		"log-level: warning",
		'ipv6: false',
		'external-controller: 127.0.0.1:9090',
		'',
		'proxies:',
	]
	for node in nodes:
		parts = ', '.join(f'{k}: {yaml_quote(v)}' for k, v in node.items())
		lines.append(f'  - {{{parts}}}')
	bridges = docker_bridge_ips()
	if bridges:
		lines += ['', 'listeners:']
		for idx, ip in enumerate(bridges):
			# 同端口、不同监听地址，供容器（如 metapi）访问；不绑 0.0.0.0
			lines += [f'  - name: docker-in-{idx}', '    type: mixed',
					  f'    port: {port}', f'    listen: {ip}']

	names = ', '.join(yaml_quote(n['name']) for n in nodes)
	lines += [
		'',
		'proxy-groups:',
		'  - name: PROXY',
		# 必须用 select（手动固定），不能用 url-test 也不能用 fallback。
		#
		# acw_sc__v2 这类阿里云 WAF cookie、以及站点的 session，都是**绑出口 IP** 的。
		# url-test 会因延迟波动随时换节点；fallback 虽只在节点不可用时才切，但一旦切了
		# 出口 IP 就变，已登录的 session 立刻失效 —— 实测 05:04 用美国节点签到成功，
		# 05:33 自动切到香港节点后两个 agentrouter 账号同时报「读不到用户信息」。
		#
		# 所以固定单节点，宁可节点挂了手动换，也不要 IP 悄悄漂移。
		# 换节点：curl -X PUT -d '{"name":"节点名"}' http://127.0.0.1:9090/proxies/PROXY
		'    type: select',
		f'    proxies: [{names}]',
		'',
		'rules:',
		'  - MATCH,PROXY',
		'',
	]
	return '\n'.join(lines)


def main() -> int:
	argv = sys.argv[1:]
	nodes_file = None
	port = 7890
	i = 0
	while i < len(argv):
		if argv[i] == '--nodes-file' and i + 1 < len(argv):
			nodes_file = argv[i + 1]; i += 2
		elif argv[i] == '--port' and i + 1 < len(argv):
			port = int(argv[i + 1]); i += 2
		else:
			i += 1
	if not nodes_file:
		print(__doc__)
		return 1

	text = Path(nodes_file).read_text(encoding='utf-8')
	nodes = parse_json_nodes(text)
	if not nodes:
		nodes = [n for n in (parse_ss(ln) for ln in text.splitlines() if ln.strip()) if n]
	if not nodes:
		nodes = parse_yaml_nodes(text)
	if not nodes:
		print('没解析出任何节点。请确认输入是 ss:// 链接（一行一个）或 clash proxies 片段。')
		return 1

	print(f'解析到 {len(nodes)} 个节点：')
	for n in nodes:
		print(f"  - {n['name']}  ({n['type']} {n['server']}:{n.get('port')})")

	CONFIG.write_text(build_config(nodes, port), encoding='utf-8')
	CONFIG.chmod(0o600)
	print(f'\n配置已写入 {CONFIG}')
	bridges = docker_bridge_ips()
	print('  额外监听（供容器访问）:', bridges or '无 docker 网桥')

	check = subprocess.run([str(MIHOMO_DIR / 'mihomo'), '-t', '-f', str(CONFIG)],
						   capture_output=True, text=True)
	print('配置自检:', (check.stdout + check.stderr).strip()[:300] or 'OK')
	return 0 if check.returncode == 0 else 1


if __name__ == '__main__':
	sys.exit(main())
