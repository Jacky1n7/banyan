"""部署路径解析。

所有部署相关路径集中在这里，不再散落在各脚本里写死 `/opt/...` —— 那种写法只在
作者那台机器上成立，别人按自己的目录部署会直接撞墙。

默认值与原硬编码值等价，且每项都能用环境变量覆盖，所以已有部署行为不变：

| 环境变量 | 默认值 | 含义 |
|---|---|---|
| `CHECKIN_DIR` | 本文件所在目录 | 上游 `anyrouter-check-in` 的 clone 目录，本目录的文件就放在其中 |
| `METAPI_DIR` | `/opt/metapi` | metapi 部署目录（读它的 `.env` 和 `data/hub.db`） |
| `MIHOMO_DIR` | `/opt/mihomo` | mihomo 配置目录 |
| `UV_BIN` | 自动探测 | uv 可执行文件路径 |

`CHECKIN_DIR` 默认由 `__file__` 推导而不是写死绝对路径：本目录的文件本来就该被
放进上游 clone 目录里运行，所以无论 clone 到哪儿，这个推导都成立。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _env_path(name: str, default: Path) -> Path:
	raw = os.environ.get(name, '').strip()
	return Path(raw).expanduser() if raw else default


#: 上游 anyrouter-check-in 的 clone 目录（本目录文件被复制进去）
CHECKIN_DIR = _env_path('CHECKIN_DIR', Path(__file__).resolve().parent)

#: metapi 部署目录
METAPI_DIR = _env_path('METAPI_DIR', Path('/opt/metapi'))
METAPI_ENV = METAPI_DIR / '.env'
METAPI_DB = METAPI_DIR / 'data' / 'hub.db'

#: mihomo 配置目录（仅代理相关脚本使用）
MIHOMO_DIR = _env_path('MIHOMO_DIR', Path('/opt/mihomo'))
MIHOMO_CONFIG = MIHOMO_DIR / 'config.yaml'
SUB_UPDATER = MIHOMO_DIR / 'update_sub.py'


def uv_bin() -> str:
	"""定位 uv 可执行文件。

	systemd 的最小 PATH 不含 `~/.local/bin`，而 uv 官方安装器的默认落点正是那里，
	所以不能只依赖 PATH 查找，否则服务起不来。
	"""
	explicit = os.environ.get('UV_BIN', '').strip()
	if explicit:
		return explicit
	found = shutil.which('uv')
	if found:
		return found
	fallback = Path.home() / '.local' / 'bin' / 'uv'
	return str(fallback) if fallback.exists() else 'uv'


def uv_env() -> dict:
	"""给 uv 子进程用的环境变量，确保 PATH 里有 uv 所在的目录。

	systemd 的最小 PATH 不含 `~/.local/bin`，uv 装在那儿时子进程会找不到它。
	"""
	env = dict(os.environ)
	bin_dir = str(Path.home() / '.local' / 'bin')
	if bin_dir not in env.get('PATH', '').split(os.pathsep):
		env['PATH'] = bin_dir + os.pathsep + env.get('PATH', '')
	return env


def ensure_importable() -> None:
	"""把 CHECKIN_DIR 加入 sys.path，让 `import checkin` / `import utils.*` 可用。"""
	if str(CHECKIN_DIR) not in sys.path:
		sys.path.insert(0, str(CHECKIN_DIR))


def bootstrap() -> None:
	"""`ensure_importable()` + 切到 CHECKIN_DIR。

	上游模块按 CWD 解析相对路径（`.env`、`.browser_profiles/` 等），所以调用方
	必须在 import 上游模块之前先把 CWD 切过去。
	"""
	ensure_importable()
	os.chdir(CHECKIN_DIR)
