"""项目根目录、业务目录和路径安全边界。"""

from __future__ import annotations

import json
from pathlib import Path


def project_root() -> Path:
    """返回随当前源码确定的独立项目根目录。"""

    return Path(__file__).resolve().parents[2]


def environment_root() -> Path:
    """返回显式声明当前 Skill 的工作区，否则使用独立 Skill 根目录。

    只检查紧邻父目录，不向上搜索，也不改变业务文件写入边界。
    工作区声明损坏时抛出 ValueError，不静默切换配置来源。
    """
    root = project_root()
    marker = root.parent / "skills-workspace.json"
    if not marker.is_file():
        return root
    try:
        workspace = json.loads(marker.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError("工作区 skills-workspace.json 不是有效 JSON") from exc
    skills = workspace.get("skills") if isinstance(workspace, dict) else None
    if not isinstance(skills, list) or not all(isinstance(name, str) for name in skills):
        raise ValueError("工作区 skills-workspace.json 的 skills 必须是名称列表")
    return root.parent if root.name in skills else root


def require_project_path(path: Path | str, label: str) -> Path:
    """解析项目内路径，并拒绝项目目录之外的目标。

    参数：
        path: 绝对路径或相对项目根的路径。
        label: 错误信息中的业务名称。
    返回：
        已解析的绝对路径。
    异常：
        ValueError: 目标位于项目根之外。
    """

    root = project_root()
    value = Path(path)
    resolved = value.resolve() if value.is_absolute() else (root / value).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"{label}必须位于 articlemonitor 项目内: {resolved}")
    return resolved


def data_directories() -> tuple[Path, ...]:
    """返回项目必须维护的正式业务目录。"""

    root = project_root()
    return (
        root / "1-对标账号",
        root / "2-素材库",
        root / "3-对标案例" / "文案",
    )


def ensure_project_directories() -> None:
    """创建正式业务目录和项目内临时目录。"""

    for directory in (*data_directories(), project_root() / ".tmp"):
        directory.mkdir(parents=True, exist_ok=True)


def monitor_output_root() -> Path:
    """返回监控流程使用的对标案例根目录。"""

    return require_project_path("3-对标案例", "监控输出目录")


def account_registry_path() -> Path:
    """返回本地对标账号 Markdown 清单路径。"""

    return require_project_path("1-对标账号/accounts.md", "账号清单")


def temporary_root() -> Path:
    """返回项目内临时目录并确保它存在。"""

    target = require_project_path(".tmp", "临时目录")
    target.mkdir(parents=True, exist_ok=True)
    return target
