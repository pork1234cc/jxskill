"""articlemonitor 的独立命令行入口。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .account_store import account_bitable_client_from_env
from .case_store import reference_bitable_client_from_env
from .config import apply_environment, load_config, merged_environment
from .filtering import DEFAULT_WINDOW_HOURS, FilterCondition, MonitorFilter
from .local_store import LocalAccountStore
from .monitor import ReferenceMonitor
from .project import (
    account_registry_path,
    ensure_project_directories,
    environment_root,
    monitor_output_root,
    project_root,
    require_project_path,
)
from .tikhub import TikHubClient


def configure_console_encoding() -> None:
    """尽量把命令行中文输入输出切换为 UTF-8。"""

    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        return


def build_parser() -> argparse.ArgumentParser:
    """建立账号登记、监控和单条文案提取的命令解析器。"""

    parser = argparse.ArgumentParser(
        description="登记、监控对标账号，或手动提取单条视频文案。"
    )
    subparsers = parser.add_subparsers(dest="command")

    record = subparsers.add_parser("record", help="只把对标账号登记到当前账号源")
    record.add_argument("link", help="该账号下任意一条抖音或微信视频号作品链接")
    add_common_options(record)

    extract = subparsers.add_parser("extract", help="手动提取单条视频的口播文案")
    extract.add_argument("link", help="抖音或微信视频号的公开视频链接")
    add_common_options(extract)

    monitor = subparsers.add_parser("monitor", help="监控全部已登记对标账号")
    add_common_options(monitor)
    monitor.add_argument(
        "--filter",
        action="append",
        default=[],
        metavar="字段:运算符:阈值",
        help="重复添加结构化指标条件，例如 like_count:gte:500",
    )
    monitor.add_argument(
        "--filter-logic",
        choices=("all", "any"),
        default="all",
        help="多个指标条件使用且（all）或或（any），默认 all",
    )
    monitor.add_argument(
        "--window-hours",
        type=float,
        default=DEFAULT_WINDOW_HOURS,
        help="监控时间窗口小时数，默认 72",
    )
    return parser


def add_common_options(parser: argparse.ArgumentParser) -> None:
    """添加项目内配置文件和 JSON 输出选项。"""

    parser.add_argument("--env-file", default="", help="Skill 内配置或已声明工作区的 .env")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON 结果")


def resolve_env_path(raw_path: str) -> Path | None:
    """允许 Skill 内配置及明确声明的共用 .env，拒绝其他外部文件。"""

    if not raw_path.strip():
        return None
    candidate = Path(raw_path)
    resolved = (
        candidate.resolve() if candidate.is_absolute() else (project_root() / candidate).resolve()
    )
    shared_env = (environment_root() / ".env").resolve()
    path = resolved if resolved == shared_env else require_project_path(raw_path, "配置文件")
    if not path.is_file():
        raise ValueError(f"配置文件不存在: {path}")
    return path


def parse_filter_condition(raw_value: str) -> FilterCondition:
    """解析 `字段:运算符:阈值` 格式的单个筛选条件。"""

    parts = [part.strip() for part in raw_value.split(":")]
    if len(parts) != 3 or not all(parts):
        raise ValueError(
            "筛选条件必须使用“字段:运算符:阈值”格式，"
            "例如 like_count:gte:500。"
        )
    field, filter_operator, raw_threshold = parts
    try:
        threshold = float(raw_threshold)
    except ValueError as exc:
        raise ValueError(f"筛选阈值必须是数字：{raw_threshold}") from exc
    return FilterCondition(field, filter_operator, threshold)


def build_monitor_filter(args: argparse.Namespace) -> MonitorFilter:
    """从监控命令参数构造已经校验的筛选规则。"""

    raw_conditions = list(getattr(args, "filter", []))
    logic = str(getattr(args, "filter_logic", "all"))
    if not raw_conditions and logic != "all":
        raise ValueError("使用 --filter-logic 时必须提供至少一个 --filter。")
    return MonitorFilter(
        conditions=tuple(parse_filter_condition(item) for item in raw_conditions),
        logic=logic,
        window_hours=float(
            getattr(args, "window_hours", DEFAULT_WINDOW_HOURS)
        ),
    )


def run_command(args: argparse.Namespace) -> dict[str, Any]:
    """执行账号登记、监控或手动提取并返回可序列化结果。"""

    monitor_filter = (
        build_monitor_filter(args) if args.command == "monitor" else None
    )
    ensure_project_directories()
    env_path = resolve_env_path(args.env_file)
    values = merged_environment(env_path)
    apply_environment(values)
    config = load_config(env_path)
    use_feishu = config.storage_backend == "feishu"
    client = TikHubClient(config)
    if args.command == "extract":
        if use_feishu:
            reference_bitable_client_from_env(values).validate_schema()
        monitor = ReferenceMonitor(
            client,
            monitor_output_root(),
            sync_to_feishu=use_feishu,
        )
        return monitor.extract_post(args.link)

    account_store = (
        account_bitable_client_from_env(values)
        if use_feishu
        else LocalAccountStore(account_registry_path())
    )
    if use_feishu and args.command == "monitor":
        reference_bitable_client_from_env(values).validate_schema()
    monitor_options: dict[str, Any] = {
        "account_store": account_store,
        "sync_to_feishu": use_feishu,
    }
    if monitor_filter is not None:
        monitor_options["monitor_filter"] = monitor_filter
    monitor = ReferenceMonitor(
        client,
        monitor_output_root(),
        **monitor_options,
    )
    if args.command == "record":
        return monitor.record_account(args.link)
    return monitor.monitor_all()


def print_result(payload: dict[str, Any], *, as_json: bool) -> None:
    """按用户选择输出 JSON 或简短中文汇总。"""

    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if not payload.get("ok"):
        print(f"[失败] {payload.get('error') or payload.get('summary')}", file=sys.stderr)
        return
    if "account_action" in payload:
        account = payload.get("account") or {}
        name = account.get("nickname") or account.get("username") or ""
        print(f"[成功] 已登记 {name}（{payload.get('account_action')}）")
        return
    if "action" in payload and payload.get("markdown"):
        action = "已存在" if payload.get("action") == "existing" else "已提取"
        print(f"[成功] 文案{action}：{payload.get('markdown')}")
        return
    summary = payload.get("summary") or {}
    print(
        f"[成功] 新增 {summary.get('added', 0)}，更新 {summary.get('updated', 0)}，"
        f"跳过 {summary.get('skipped', 0)}，失败 {summary.get('failed', 0)}"
    )


def main() -> int:
    """解析参数、隔离可预期错误并返回进程退出码。"""

    configure_console_encoding()
    parser = build_parser()
    args = parser.parse_args()
    if args.command not in {"record", "monitor", "extract"}:
        parser.print_help()
        return 2
    try:
        payload = run_command(args)
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        payload = {"ok": False, "error": str(exc)}
    print_result(payload, as_json=args.json)
    return 0 if payload.get("ok") else 1
