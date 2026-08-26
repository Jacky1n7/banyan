#!/usr/bin/env python3
"""从机场订阅更新 mihomo 的节点，保留本机特有的配置。

为什么需要这个
--------------
节点原本是手动喂进 config.yaml 的（见 setup_proxy.py），因为**上一个**机场的订阅站
挂在 Cloudflare 后面，会把服务器的数据中心 IP 判成代理/VPN 而 403。现在这个订阅
(cat.cn-ping.com) 直连就能拉，所以可以自动化。

2026-08-26 的故障说明手动维护的代价：机场把 dd1d1.catcat321.com 那条 A 记录删掉
(NXDOMAIN)，6 个节点共用它、于是同时失效。订阅里其实早就写着新域名 dd2d1，
而且端口和 SNI 也都换了（美国节点 42054/usj1 → 25137/usj2，那正是它连不上的原因）。

只取「直连」节点
----------------
订阅里 103 个节点，IEPL 73 / 中转 20 / 直连 6。只用直连，原因是这些站的 WAF cookie
和 session 绑出口 IP，链路越简单越可预期；IEPL/中转多一跳且出口不稳。
（另一个机场的 IEPL 实测完全不通 —— 入口是上海移动，从美国机房 100% 丢包。）

安全
----
订阅 URL 含 token，从 .sub_url 读（600 权限），不进命令行参数、不落日志。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

import yaml

MIHOMO_DIR = Path('/opt/mihomo')
CONFIG = MIHOMO_DIR / 'config.yaml'
SUB_URL_FILE = MIHOMO_DIR / '.sub_url'
UA = 'clash-verge/v2.0.0'
KEEP_KEYS = ('name', 'type', 'server', 'port', 'password', 'sni', 'udp',
             'skip-cert-verify', 'network', 'alpn', 'cipher', 'uuid')


def fetch_subscription() -> str:
    url = SUB_URL_FILE.read_text(encoding='utf-8').strip()
    if not url:
        raise SystemExit('[FAILED] .sub_url 是空的')
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        info = r.headers.get('subscription-userinfo') or ''
        text = r.read().decode('utf-8', 'replace')
    if info:
        nums = {k: int(v) for k, v in re.findall(r'(\w+)=(\d+)', info)}
        used = (nums.get('upload', 0) + nums.get('download', 0)) / 1e9
        total = nums.get('total', 0) / 1e9
        if total:
            print(f'[INFO] 流量 {used:.2f}/{total:.2f} GB（剩余 {total - used:.2f} GB）')
    return text


def direct_nodes(text: str) -> list[dict]:
    data = yaml.safe_load(text)
    out = []
    for p in (data.get('proxies') or []):
        if '直连' not in str(p.get('name', '')):
            continue
        node = {k: p[k] for k in KEEP_KEYS if k in p}
        if node.get('server') and node.get('port'):
            out.append(node)
    return out


def yaml_quote(v) -> str:
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, int):
        return str(v)
    return '"%s"' % str(v).replace('\\', '\\\\').replace('"', '\\"')


def rebuild(nodes: list[dict], old: str) -> str:
    """用新节点替换 proxies 段，其余原样保留。

    listeners 段是给 docker 网桥的（metapi 在容器里，要能连到 7890），
    proxy-groups 是 select 固定节点 —— 都必须保留，所以只换 proxies。
    """
    m = re.search(r'^proxies:.*?^(?=[a-z-]+:)', old, re.M | re.S)
    if not m:
        raise SystemExit('[FAILED] 现有配置里找不到 proxies 段')
    lines = ['proxies:']
    for n in nodes:
        lines.append('  - {%s}' % ', '.join(f'{k}: {yaml_quote(v)}' for k, v in n.items()))
    new_proxies = '\n'.join(lines) + '\n\n'

    out = old[:m.start()] + new_proxies + old[m.end():]
    # select 组的候选列表也要跟着换，否则会引用到已消失的节点名
    names = ', '.join(yaml_quote(n['name']) for n in nodes)
    out = re.sub(r'(\n    type: select\n    proxies: )\[[^\]]*\]',
                 lambda mm: mm.group(1) + '[' + names + ']', out, count=1)
    return out


def current_node() -> str | None:
    try:
        with urllib.request.urlopen('http://127.0.0.1:9090/proxies/PROXY', timeout=10) as r:
            import json
            return json.load(r).get('now')
    except Exception:
        return None


def select_node(name: str) -> bool:
    import json
    req = urllib.request.Request('http://127.0.0.1:9090/proxies/PROXY',
                                data=json.dumps({'name': name}).encode(),
                                method='PUT',
                                headers={'Content-Type': 'application/json'})
    try:
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception:
        return False


def probe(timeout: int = 25) -> str | None:
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({'https': 'http://127.0.0.1:7890',
                                         'http': 'http://127.0.0.1:7890'}))
        return opener.open('https://api.ipify.org', timeout=timeout).read().decode().strip()
    except Exception:
        return None


def main() -> int:
    text = fetch_subscription()
    nodes = direct_nodes(text)
    if not nodes:
        print('[FAILED] 订阅里没有「直连」节点，不敢动现有配置')
        return 1
    print(f'[INFO] 订阅里 {len(nodes)} 个直连节点，域名: '
          + ', '.join(sorted({n["server"] for n in nodes})))

    old = CONFIG.read_text(encoding='utf-8')
    new = rebuild(nodes, old)
    if new == old:
        print('[INFO] 节点无变化，不重启')
        return 0

    # 记下当前节点，重启后尽量选回同一个（出口 IP 换了会让已登录 session 失效）
    was = current_node()
    backup = CONFIG.with_suffix('.yaml.bak.autoupdate')
    shutil.copy2(CONFIG, backup)
    CONFIG.write_text(new, encoding='utf-8')
    print(f'[INFO] 配置已更新（备份 {backup.name}），重启 mihomo…')
    subprocess.run(['systemctl', 'restart', 'mihomo'], check=False)

    import time
    time.sleep(8)
    if was and any(n['name'] == was for n in nodes):
        select_node(was)
        time.sleep(2)
        print(f'[INFO] 已选回原节点 {was}')

    ip = probe()
    if ip:
        print(f'[SUCCESS] 代理可用，出口 {ip}')
        return 0

    # 原节点不通就换一个能用的
    for n in nodes:
        if n['name'] == was:
            continue
        select_node(n['name'])
        time.sleep(2)
        ip = probe()
        if ip:
            print(f'[SUCCESS] 原节点不通，已切到 {n["name"]}，出口 {ip}')
            return 0

    print('[FAILED] 新配置下没有任何节点可用，回滚')
    shutil.copy2(backup, CONFIG)
    subprocess.run(['systemctl', 'restart', 'mihomo'], check=False)
    time.sleep(8)
    print('[INFO] 已回滚，出口 ' + (probe() or '仍不通'))
    return 1


if __name__ == '__main__':
    sys.exit(main())
