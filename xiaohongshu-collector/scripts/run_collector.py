"""执行小红书关键词采集、本地保存和可选飞书同步。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import collector as core
import feishu_bitable as bitable
import feishu_sync

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass


DEFAULT_API_BASE = "https://api.tikhub.dev/api/v1/xiaohongshu/app_v2"
SEARCH_PAGE_SIZE = 20
HANDLED_ERRORS = (HTTPError, URLError, OSError, UnicodeError, json.JSONDecodeError, ValueError)


@dataclass(frozen=True)
class RunResult:
    """一次真实采集的本地结果与统计。"""

    records: tuple[core.LocalNoteRecord, ...]
    local: core.LocalWriteResult
    scanned: int
    qualified: int
    search_filtered: int
    detail_filtered: int
    request_attempts: int
    stop_reason: str
    errors: tuple[str, ...]
    reused_ids: tuple[str, ...] = ()


class TikHubClient:
    """不自动重试的 TikHub App V2 客户端。"""

    def __init__(self, token: str, api_base: str = DEFAULT_API_BASE) -> None:
        if not token.strip():
            raise ValueError("TIKHUB_API_KEY 不能为空")
        self.token = token.strip()
        self.api_base = api_base.rstrip("/")
        self.request_attempts = 0

    def request(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        """执行一次计费请求并校验外层响应。"""
        self.request_attempts += 1
        url = f"{self.api_base}/{endpoint}?{urlencode(params)}"
        request = Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "User-Agent": "jiuxiansheng-xhs-collector/3.0",
            },
        )
        with urlopen(request, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("code") != 200:
            message = payload.get("message_zh") if isinstance(payload, dict) else "无效响应"
            raise ValueError(f"TikHub 请求失败：{message or '未知错误'}")
        return payload


class XiaohongshuRunner:
    """串联搜索、详情 Gate、素材下载和本地保存。"""

    def __init__(
        self,
        config: core.CollectorConfig,
        client: TikHubClient,
        output_root: Path = core.DEFAULT_OUTPUT_ROOT,
        shared_records: dict[str, core.LocalNoteRecord] | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.initial_request_attempts = client.request_attempts
        self.output_root = output_root
        self.state = core.CollectionState(config)
        self.records: list[core.LocalNoteRecord] = []
        self.errors: list[str] = []
        self.shared_records = shared_records if shared_records is not None else {}
        self.reused_ids: list[str] = []

    def run(self) -> RunResult:
        """执行采集并返回本地结果。"""
        self._search_until_stop()
        local = core.LocalResultWriter(self.config, self.output_root).write(self.records)
        self.errors.extend(local.errors)
        reason = self.state.stop_reason or "SEARCH_EXHAUSTED"
        return RunResult(
            tuple(self.records),
            local,
            self.state.scanned_count,
            len(self.state.qualified_ids),
            self.state.search_filtered,
            self.state.detail_filtered,
            self.client.request_attempts - self.initial_request_attempts,
            reason,
            tuple(self.errors),
            tuple(self.reused_ids),
        )

    def _search_until_stop(self) -> None:
        page, search_id, session_id = 1, "", ""
        while not self.state.should_stop:
            payload = self.client.request("search_notes", core.build_search_params(self.config, page, search_id, session_id))
            cards = extract_search_notes(payload)
            new_count = self._process_search_cards(cards)
            if self.state.should_stop or new_count == 0:
                break
            search_id = str(core.find_first_alias(payload.get("data"), ("search_id",)) or "")
            session_id = str(core.find_first_alias(payload.get("data"), ("search_session_id",)) or "")
            if not search_id or not session_id:
                break
            page += 1

    def _process_search_cards(self, cards: list[dict[str, Any]]) -> int:
        new_count = 0
        for card in cards:
            registered = self.state.register_search(card)
            if registered is None:
                continue
            new_count += 1
            decision, search_note = registered
            if decision is core.GateDecision.FAIL:
                continue
            if search_note.note_id in self.shared_records:
                self.state.qualified_ids.add(search_note.note_id)
                self.reused_ids.append(search_note.note_id)
                continue
            self._collect_candidate(search_note, card)
        return new_count

    def _collect_candidate(
        self,
        search_note: core.NormalizedNote,
        search_raw: dict[str, Any],
    ) -> None:
        note_id = search_note.note_id
        try:
            payload = self.client.request("get_image_note_detail", {"note_id": note_id})
            detail_raw = extract_note_detail(payload)
        except HANDLED_ERRORS as exc:
            self.errors.append(f"{note_id} 详情失败：{exc}")
            return
        decision, detail_note = self.state.register_detail(detail_raw)
        if decision is not core.GateDecision.PASS:
            return
        detail_note = replace(
            detail_note,
            likes=search_note.likes,
            collects=search_note.collects,
            comments=search_note.comments,
        )
        record = core.LocalNoteRecord(detail_note, (), search_raw)
        self.records.append(record)
        self.shared_records[note_id] = record


def extract_search_notes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """从 App V2 搜索响应提取唯一笔记卡片。"""
    notes: list[dict[str, Any]] = []
    seen: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            candidate = value.get("note") if value.get("model_type") == "note" else None
            if not isinstance(candidate, dict) and "note_card" in value:
                candidate = value.get("note_card") or value
            if isinstance(candidate, dict):
                note_id = str(candidate.get("note_id") or candidate.get("id") or value.get("id") or "")
                if core.NOTE_ID_PATTERN.fullmatch(note_id) and note_id not in seen:
                    seen.add(note_id)
                    notes.append(candidate)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload.get("data"))
    return notes


def extract_note_detail(payload: dict[str, Any]) -> dict[str, Any]:
    """从详情响应提取唯一笔记对象。"""
    note_list = core.find_first_alias(payload.get("data"), ("note_list",))
    if not isinstance(note_list, list) or not note_list or not isinstance(note_list[0], dict):
        raise ValueError("笔记详情响应中缺少 note_list")
    return note_list[0]


def load_token(env_path: Path = core.PROJECT_ROOT / ".env") -> str:
    """从环境变量或项目 .env 读取 TikHub 令牌。"""
    import os

    token = os.environ.get("TIKHUB_API_KEY", "").strip()
    if token:
        return token
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^\s*TIKHUB_API_KEY\s*=\s*(.*?)\s*$", line)
            if match and match.group(1).strip().strip('"\''):
                return match.group(1).strip().strip('"\'')
    raise ValueError("未找到 TIKHUB_API_KEY")


def optional_int(value: str) -> int | None:
    """解析命令行整数或 none。"""
    return None if value.strip().lower() in {"none", "null"} else int(value)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="采集小红书搜索卡片中符合条件的笔记")
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect", help="按指定关键词即时采集")
    collect.add_argument("keyword", help="搜索关键词")
    collect.add_argument("--count", type=int, default=core.NORMAL_COLLECT_COUNT, help="读取的搜索卡片数")
    collect.add_argument("--note-type", choices=sorted(core.NOTE_TYPE_OPTIONS), default="all")
    collect.add_argument("--time-filter", choices=sorted(core.TIME_FILTER_OPTIONS), default="不限")
    collect.add_argument("--min-likes", type=optional_int, default=100)
    collect.add_argument("--min-collects", type=optional_int, default=100)
    collect.add_argument("--min-comments", type=optional_int, default=20)
    collect.add_argument("--sort", choices=sorted(core.SORT_OPTIONS), default="general")
    _add_runtime_arguments(collect)
    tasks = commands.add_parser("tasks", help="依次执行飞书任务表中的未采集任务")
    _add_runtime_arguments(tasks)
    return parser.parse_args(argv)


def _add_runtime_arguments(parser: argparse.ArgumentParser) -> None:
    """给即时采集和任务采集添加共同运行参数。"""
    parser.add_argument("--output-dir", type=Path, default=core.DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)


def config_from_args(args: argparse.Namespace) -> core.CollectorConfig:
    """把即时采集命令转换为核心配置。"""
    if args.command != "collect":
        raise ValueError("只有 collect 命令可以直接生成关键词配置")
    return core.CollectorConfig(
        keyword=args.keyword,
        collect_count=args.count,
        note_type=args.note_type,
        time_filter=args.time_filter,
        min_likes=args.min_likes,
        min_collects=args.min_collects,
        min_comments=args.min_comments,
        sort=args.sort,
    )


def prepare_feishu_session(
    config: core.CollectorConfig,
) -> tuple[feishu_sync.FeishuSyncSession | None, str]:
    """配置完整时先补齐字段再创建任务；初始化失败则停止，仅缺配置时使用本地模式。"""
    try:
        feishu_config = bitable.FeishuConfig.from_env()
    except ValueError as exc:
        return None, f"未写入飞书：{exc}；本次仅保存本地结果。"
    client = bitable.FeishuBitableClient(feishu_config)
    client.ensure_collection_fields()
    try:
        return feishu_sync.FeishuSyncSession.start(client, config), ""
    except ValueError as exc:
        return None, f"飞书任务创建失败：{exc}；本次仍继续保存本地结果。"


def maximum_request_attempts(collect_count: int) -> int:
    """按每页 20 张卡片和每篇一次详情请求计算 TikHub 请求上限。"""
    search_requests = (collect_count + SEARCH_PAGE_SIZE - 1) // SEARCH_PAGE_SIZE
    return search_requests + collect_count


def print_run_start(config: core.CollectorConfig) -> None:
    """输出本关键词的扫描和请求成本边界。"""
    maximum = maximum_request_attempts(config.collect_count)
    print(
        f"即将采集关键词“{config.keyword}”：读取前 {config.collect_count} 条搜索卡片，"
        f"TikHub 请求理论上限 {maximum} 次。",
        flush=True,
    )


def print_run_result(result: RunResult) -> None:
    """输出单关键词采集结果和实际请求次数。"""
    print(f"采集完成：{result.local.run_dir}", flush=True)
    print(
        f"扫描 {result.scanned} 条，符合 {result.qualified} 篇，"
        f"TikHub 请求 {result.request_attempts} 次，素材 {result.local.media_downloads} 个。",
        flush=True,
    )
    if result.errors:
        print(f"其中 {len(result.errors)} 项异常：", file=sys.stderr, flush=True)
        for error in result.errors:
            print(f"- {error}", file=sys.stderr, flush=True)


def run_direct(args: argparse.Namespace) -> int:
    """执行一次用户直接指定关键词的即时采集。"""
    session: feishu_sync.FeishuSyncSession | None = None
    try:
        config = config_from_args(args)
        print_run_start(config)
        session, notice = prepare_feishu_session(config)
        if notice:
            print(notice, flush=True)
        client = TikHubClient(load_token(), args.api_base)
        result = XiaohongshuRunner(config, client, args.output_dir).run()
    except HANDLED_ERRORS as exc:
        if session is not None:
            session.fail()
        print(f"执行失败：{exc}", file=sys.stderr, flush=True)
        return 1
    print_run_result(result)
    return finish_direct_session(session, result)


def finish_direct_session(session: feishu_sync.FeishuSyncSession | None, result: RunResult) -> int:
    """把即时采集结果写入飞书并返回退出码。"""
    if session is None:
        return 0
    try:
        synced = session.finish(result.records, has_local_errors=bool(result.errors))
    except ValueError as exc:
        print(f"本地采集已保存，但飞书写入失败：{exc}", file=sys.stderr, flush=True)
        return 1
    print_sync_result(synced)
    return 0


def print_sync_result(result: feishu_sync.FeishuSyncResult) -> None:
    """输出一次飞书同步的任务和记录统计。"""
    print(
        f"飞书同步完成：任务ID {result.task_id}，"
        f"笔记新增 {result.notes.created}/更新 {result.notes.updated}。",
        flush=True,
    )


def task_records(
    config: core.CollectorConfig,
    result: RunResult,
    shared_records: dict[str, core.LocalNoteRecord],
) -> list[feishu_sync.TaskNoteRecord]:
    """合并本关键词新采集和任务缓存复用的飞书记录。"""
    records = [feishu_sync.TaskNoteRecord(config.keyword, record) for record in result.records]
    records.extend(
        feishu_sync.TaskNoteRecord(config.keyword, shared_records[note_id])
        for note_id in result.reused_ids
        if note_id in shared_records
    )
    return records


def run_task_plan(
    plan: feishu_sync.PendingTaskPlan,
    feishu_client: bitable.FeishuBitableClient,
    token: str,
    args: argparse.Namespace,
) -> tuple[bool, int]:
    """顺序执行一条多关键词任务，并返回是否至少完成一个关键词及请求数。"""
    session = feishu_sync.FeishuSyncSession.attach(feishu_client, plan)
    shared_records: dict[str, core.LocalNoteRecord] = {}
    collected: list[feishu_sync.TaskNoteRecord] = []
    errors: list[str] = []
    completed = 0
    client = TikHubClient(token, args.api_base)
    for config in plan.configs:
        print_run_start(config)
        before = client.request_attempts
        try:
            result = XiaohongshuRunner(config, client, args.output_dir, shared_records).run()
        except HANDLED_ERRORS as exc:
            errors.append(f"{config.keyword} 失败：{exc}")
            print(errors[-1], file=sys.stderr, flush=True)
            continue
        completed += 1
        print_run_result(result)
        collected.extend(task_records(config, result, shared_records))
        errors.extend(f"{config.keyword}：{error}" for error in result.errors)
        print(f"关键词“{config.keyword}”本轮请求 {client.request_attempts - before} 次。", flush=True)
    if completed == 0:
        session.fail()
        return False, client.request_attempts
    synced = session.finish_task(collected, has_local_errors=bool(errors))
    print_sync_result(synced)
    return True, client.request_attempts


def run_pending_tasks(args: argparse.Namespace) -> int:
    """读取飞书全部未采集任务并按表中顺序逐条执行。"""
    try:
        feishu_client = bitable.FeishuBitableClient(bitable.FeishuConfig.from_env())
        feishu_client.ensure_collection_fields()
        pending = feishu_client.list_pending_tasks()
    except ValueError as exc:
        print(f"读取采集任务失败：{exc}", file=sys.stderr, flush=True)
        return 1
    if not pending:
        print("没有采集状态为“未采集”的任务。", flush=True)
        return 0
    try:
        token = load_token()
    except ValueError as exc:
        print(f"读取采集任务失败：{exc}", file=sys.stderr, flush=True)
        return 1
    failed = 0
    total_requests = 0
    for record in pending:
        try:
            plan = feishu_sync.pending_task_plan(record)
            completed, requests = run_task_plan(plan, feishu_client, token, args)
            total_requests += requests
            failed += int(not completed)
        except ValueError as exc:
            failed += 1
            record_id = str(record.get("record_id") or "")
            if record_id:
                try:
                    feishu_client.update_task_status(record_id, "失败")
                except ValueError:
                    pass
            print(f"任务执行失败：{exc}", file=sys.stderr, flush=True)
    print(f"任务队列完成：任务 {len(pending)} 条，TikHub 请求 {total_requests} 次。", flush=True)
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run_direct(args) if args.command == "collect" else run_pending_tasks(args)


if __name__ == "__main__":
    raise SystemExit(main())
