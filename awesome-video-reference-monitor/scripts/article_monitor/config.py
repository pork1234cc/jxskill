"""读取独立项目的 TikHub 和运行配置。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .project import environment_root

DEFAULT_TIKHUB_BASE_URL = "https://api.tikhub.dev"
DEFAULT_STORAGE_BACKEND = "local"
STORAGE_BACKENDS = frozenset({"local", "feishu"})


@dataclass(frozen=True, slots=True)
class MonitorConfig:
    """保存对标账号登记和监控所需的 TikHub 配置。"""

    tikhub_api_key: str = ""
    tikhub_base_url: str = DEFAULT_TIKHUB_BASE_URL
    storage_backend: str = DEFAULT_STORAGE_BACKEND


def read_env_file(path: Path) -> dict[str, str]:
    """读取 UTF-8 dotenv 文件，不修改进程环境。"""

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        normalized_key = key.strip()
        if normalized_key:
            values[normalized_key] = value.strip().strip('"').strip("'")
    return values


def merged_environment(
    env_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """合并已声明工作区或独立 Skill 的 `.env`，进程环境优先。"""

    file_path = env_path.resolve() if env_path else environment_root() / ".env"
    values = read_env_file(file_path) if file_path.is_file() else {}
    values.update(dict(os.environ if environ is None else environ))
    return values


def load_config(
    env_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> MonitorConfig:
    """从独立项目配置加载 TikHub 客户端参数。"""

    values = merged_environment(env_path, environ)
    storage_backend = str(
        values.get("ARTICLEMONITOR_STORAGE_BACKEND", DEFAULT_STORAGE_BACKEND)
    ).strip().lower()
    if storage_backend not in STORAGE_BACKENDS:
        allowed = "、".join(sorted(STORAGE_BACKENDS))
        raise ValueError(
            "ARTICLEMONITOR_STORAGE_BACKEND 只能是 "
            f"{allowed}，当前值为：{storage_backend or '空'}"
        )
    return MonitorConfig(
        tikhub_api_key=str(values.get("TIKHUB_API_KEY", "")).strip(),
        tikhub_base_url=str(
            values.get("TIKHUB_BASE_URL", DEFAULT_TIKHUB_BASE_URL)
        ).strip().rstrip("/"),
        storage_backend=storage_backend,
    )


def apply_environment(values: Mapping[str, str]) -> None:
    """把 dotenv 配置补充到进程环境，保留显式进程变量优先级。"""

    for key, value in values.items():
        if key and key not in os.environ:
            os.environ[key] = str(value)


def require_tikhub_config(config: MonitorConfig) -> None:
    """校验账号登记和监控所需的 TikHub 配置。"""

    if not config.tikhub_api_key:
        raise RuntimeError("缺少 TIKHUB_API_KEY，请在项目 .env 中配置 TikHub API Key。")
    if not config.tikhub_base_url:
        raise RuntimeError("缺少 TIKHUB_BASE_URL，请配置 TikHub API 基础地址。")
