"""对标账号登记、72 小时作品扫描与本地文案更新。"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path
from typing import Any, Protocol

from .case_store import sync_markdown_case
from .filtering import DEFAULT_WINDOW_HOURS, MonitorFilter
from .markdown import (
    paragraphize_transcript,
    render_segment_lines,
    safe_filename,
    seconds_to_time,
    table_escape,
    transcript_source_text,
)
from .tikhub import MonitorAccount, MonitorPost, PostPage, TikHubClient
from .workflow import collect_profile

CHINA_TIMEZONE = timezone(timedelta(hours=8))
MONITOR_WINDOW = timedelta(hours=DEFAULT_WINDOW_HOURS)
MAX_PAGES_PER_ACCOUNT = 5
MONITOR_TITLE_LIMIT = 20
PLATFORM_LABELS = {"douyin": "抖音", "wechat_channels": "微信视频号"}


class MonitorAccountStore(Protocol):
    """描述当前账号源提供的登记和只读账号能力。"""

    def sync_account(
        self,
        account: MonitorAccount,
        share_url: str,
        recorded_at: datetime,
    ) -> dict[str, str]:
        """新增或更新一个对标账号。"""

    def list_monitor_accounts(self) -> list[MonitorAccount]:
        """读取全部可监控账号。"""


@dataclass(slots=True)
class MonitorSummary:
    """保存单账号或整轮监控的计数与错误。"""

    added: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def merge(self, other: MonitorSummary) -> None:
        """把另一个账号的结果合并到当前汇总。"""

        self.added += other.added
        self.updated += other.updated
        self.skipped += other.skipped
        self.failed += other.failed
        self.errors.extend(other.errors)

    def to_dict(self) -> dict[str, Any]:
        """转换为可直接输出 JSON 的结构。"""

        return {
            "added": self.added,
            "updated": self.updated,
            "skipped": self.skipped,
            "failed": self.failed,
            "errors": list(self.errors),
        }


class ReferenceMonitor:
    """按当前账号源顺序串行处理 TikHub 作品列表。"""

    def __init__(
        self,
        client: TikHubClient,
        output_dir: Path | str,
        *,
        now_provider: Callable[[], datetime] | None = None,
        collect_func: Callable[..., dict[str, Any]] | None = None,
        account_store: MonitorAccountStore | None = None,
        sync_to_feishu: bool = False,
        case_sync_func: Callable[[Path], dict[str, str]] | None = None,
        monitor_filter: MonitorFilter | None = None,
    ) -> None:
        """配置客户端、本地目录、时钟、采集核心和账号源。"""

        self.client = client
        self.output_dir = Path(output_dir)
        self.assets_root = self.output_dir.parent / "2-素材库"
        self.now_provider = now_provider or (lambda: datetime.now(CHINA_TIMEZONE))
        self.collect_func = collect_func or collect_profile
        self.account_store = account_store
        self.sync_to_feishu = sync_to_feishu
        self.case_sync_func = case_sync_func or sync_markdown_case
        self.monitor_filter = monitor_filter or MonitorFilter()

    def record_account(self, share_url: str) -> dict[str, Any]:
        """解析账号并只写当前账号源，不读取或处理作品列表。"""

        if self.account_store is None:
            raise RuntimeError("记录对标缺少账号存储。")
        account = self.client.fetch_account(share_url)
        saved = self.account_store.sync_account(
            account,
            share_url,
            ensure_china_time(self.now_provider()),
        )
        return {
            "ok": True,
            "account_action": saved["action"],
            "account_id": saved["account_id"],
            "account": account_to_dict(account),
        }

    def extract_post(self, share_url: str) -> dict[str, Any]:
        """手动提取单条作品文案，并按案例 ID 避免重复 ASR。

        参数：
            share_url: 抖音或微信视频号的公开作品链接。
        返回：
            包含新增或已存在状态、案例 ID 和 Markdown 路径的结果。
        异常：
            RuntimeError: 作品不是视频，或媒体、解密、ASR 处理失败。
        """

        post = self.client.fetch_post(share_url)
        case_index = build_case_index(
            self.output_dir / "文案",
            self.assets_root,
        )
        existing_path = case_index.get(post.case_id)
        if existing_path is not None:
            feishu_result = (
                self.case_sync_func(existing_path)
                if self.sync_to_feishu
                else {"record_id": "", "action": "skipped"}
            )
            return {
                "ok": True,
                "action": "existing",
                "platform": post.platform,
                "source_url": share_url,
                "markdown": str(existing_path.resolve()),
                "case_id": post.case_id,
                "feishu_record_id": feishu_result["record_id"],
                "feishu_action": feishu_result["action"],
                "warnings": [],
            }
        if not post.is_video:
            raise RuntimeError("手动提取只支持视频作品。")
        ready_post = self._ensure_media(post)
        result = dict(
            self.collect_func(
                ready_post.to_profile(),
                share_url,
                output_dir=self.output_dir,
                markdown_dir=self.output_dir / "文案",
                sync_to_feishu=self.sync_to_feishu,
            )
        )
        result["action"] = "create"
        return result

    def monitor_all(self) -> dict[str, Any]:
        """读取全部账号，隔离单账号失败并返回整轮汇总。"""

        if self.account_store is None:
            raise RuntimeError("监控对标缺少账号存储。")
        accounts = self.account_store.list_monitor_accounts()
        total = MonitorSummary()
        account_results: list[dict[str, Any]] = []
        for account in accounts:
            try:
                summary = self.monitor_account(account)
            except Exception as exc:  # noqa: BLE001 - 单账号失败必须隔离并继续后续账号
                summary = MonitorSummary(failed=1, errors=[str(exc)])
            total.merge(summary)
            account_results.append(
                {"account": account_to_dict(account), "summary": summary.to_dict()}
            )
        return {
            "ok": total.failed == 0,
            "accounts": len(accounts),
            "filter": self.monitor_filter.to_dict(),
            "summary": total.to_dict(),
            "results": account_results,
        }

    def monitor_account(self, account: MonitorAccount) -> MonitorSummary:
        """获取单账号窗口内作品，新增文案或刷新已有平台数据。"""

        now = ensure_china_time(self.now_provider())
        posts = self._recent_posts(account, now)
        case_index = build_case_index(
            self.output_dir / "文案",
            self.assets_root,
        )
        summary = MonitorSummary()
        for post in posts:
            try:
                self._process_post(post, now, case_index, summary)
            except Exception as exc:  # noqa: BLE001 - 单作品失败必须保留重试并继续本账号
                summary.failed += 1
                summary.errors.append(f"{post.case_id}: {exc}")
        return summary

    def _recent_posts(self, account: MonitorAccount, now: datetime) -> list[MonitorPost]:
        """按停止规则读取最多五页，并按作品 ID 去重。"""

        posts = self._scan_channel(account, now, channel="normal")
        has_recent = any(
            is_in_window(post.create_time, now, self.monitor_filter.window)
            for post in posts
        )
        if account.platform == "douyin" and posts and not has_recent:
            posts = self._scan_channel(account, now, channel="lite")
        deduplicated: dict[str, MonitorPost] = {}
        for post in posts:
            if post.video_id and post.video_id not in deduplicated:
                deduplicated[post.video_id] = post
        return list(deduplicated.values())

    def _scan_channel(
        self,
        account: MonitorAccount,
        now: datetime,
        *,
        channel: str,
    ) -> list[MonitorPost]:
        """扫描一个平台渠道，旧作品乱序时只额外读取一页。"""

        cursor = ""
        posts: list[MonitorPost] = []
        read_extra_page = False
        for _page_number in range(MAX_PAGES_PER_ACCOUNT):
            page = self.client.fetch_posts(account, cursor=cursor, channel=channel)
            posts.extend(page.posts)
            all_old = page_is_all_old(page, now, self.monitor_filter.window)
            ordered = page_is_time_ordered(page)
            if all_old and ordered:
                break
            if all_old and not ordered:
                if read_extra_page:
                    break
                read_extra_page = True
            elif read_extra_page:
                break
            if not page.has_more or not page.cursor or page.cursor == cursor:
                break
            cursor = page.cursor
        return posts

    def _process_post(
        self,
        post: MonitorPost,
        now: datetime,
        case_index: dict[str, Path],
        summary: MonitorSummary,
    ) -> None:
        """处理单条作品，并确保失败作品不会写入成功索引。"""

        if not is_in_window(post.create_time, now, self.monitor_filter.window):
            summary.skipped += 1
            return
        existing_path = case_index.get(post.case_id)
        if existing_path:
            updated_at = format_datetime(now)
            refresh_monitor_metrics(existing_path, post, updated_at)
            if self.sync_to_feishu:
                self.case_sync_func(existing_path)
            summary.updated += 1
            return
        if not post.is_video:
            summary.skipped += 1
            return
        if not self.monitor_filter.matches(post):
            summary.skipped += 1
            return
        ready_post = self._ensure_media(post)
        result = self._collect_new(ready_post)
        case_index[post.case_id] = Path(result["markdown"])
        summary.added += 1

    def _ensure_media(self, post: MonitorPost) -> MonitorPost:
        """补查抖音缺失媒体；视频号缺少成对密钥时直接失败重试。"""

        if post.platform == "douyin" and not post.media_url:
            account = MonitorAccount(post.platform, post.nickname, post.username, "")
            post = self.client.fetch_douyin_post(post.video_id, account)
        if not post.media_url:
            raise RuntimeError("作品列表和详情均缺少可下载的视频地址。")
        if post.platform == "wechat_channels" and not post.decode_key:
            raise RuntimeError("视频号作品缺少同次响应的 decode_key。")
        return post

    def _collect_new(self, post: MonitorPost) -> dict[str, Any]:
        """调用共享采集核心，按账号和二十字标题保存待筛选素材。"""

        def renderer(**kwargs: Any) -> str:
            """把共享采集结果渲染为带监控字段的 Markdown。"""

            return render_monitor_markdown(post=post, **kwargs)

        material_dir = account_material_dir(self.assets_root, post)
        filename_stem = material_filename_stem(material_dir, post)
        return self.collect_func(
            post.to_profile(),
            post.source_url,
            output_dir=self.output_dir,
            markdown_dir=material_dir,
            filename_stem=filename_stem,
            sync_to_feishu=self.sync_to_feishu,
            markdown_renderer=renderer,
        )


def ensure_china_time(value: datetime) -> datetime:
    """把传入时刻统一转换为北京时间。"""

    if value.tzinfo is None:
        return value.replace(tzinfo=CHINA_TIMEZONE)
    return value.astimezone(CHINA_TIMEZONE)


def is_in_window(
    create_time: int,
    now: datetime,
    window: timedelta = MONITOR_WINDOW,
) -> bool:
    """判断发布时间是否落在含边界的指定时间窗口内。"""

    if create_time <= 0:
        return False
    current_timestamp = int(now.timestamp())
    cutoff_timestamp = current_timestamp - int(window.total_seconds())
    return cutoff_timestamp <= create_time <= current_timestamp


def meets_material_threshold(post: MonitorPost) -> bool:
    """判断新素材是否同时满足转发绝对值和转发点赞比门槛。

    点赞为零或异常负值时不执行除法；只要转发达到最低值即视为通过。
    """

    return MonitorFilter().matches(post)


def page_is_all_old(
    page: PostPage,
    now: datetime,
    window: timedelta = MONITOR_WINDOW,
) -> bool:
    """判断整页有效发布时间是否都早于窗口截止点。"""

    times = [post.create_time for post in page.posts if post.create_time > 0]
    cutoff = int((now - window).timestamp())
    return bool(times) and len(times) == len(page.posts) and all(item < cutoff for item in times)


def page_is_time_ordered(page: PostPage) -> bool:
    """判断一页作品是否按发布时间从新到旧排列。"""

    times = [post.create_time for post in page.posts if post.create_time > 0]
    return all(left >= right for left, right in pairwise(times))


def build_case_index(*directories: Path) -> dict[str, Path]:
    """递归扫描素材库和对标案例目录，避免重复采集。"""

    result: dict[str, Path] = {}
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in directory.rglob("*.md"):
            case_id = case_id_from_markdown(path)
            if case_id and case_id not in result:
                result[case_id] = path
    return result


def case_id_from_markdown(path: Path) -> str:
    """从 Markdown 元数据读取案例 ID，并兼容旧稳定文件名。"""

    pattern = re.compile(r"^\|\s*案例 ID\s*\|\s*(.*?)\s*\|\s*$", re.MULTILINE)
    try:
        match = pattern.search(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError):
        return ""
    if match:
        return match.group(1).replace("\\|", "|").strip()
    return path.stem if re.match(r"^(douyin|wechat_channels)_", path.stem) else ""


def account_material_dir(assets_root: Path, post: MonitorPost) -> Path:
    """返回以账号中文名称命名的素材目录。"""

    fallback = post.username or post.platform or "对标账号"
    account_name = safe_filename(post.nickname, fallback, limit=50)
    return assets_root / account_name


def material_filename_stem(material_dir: Path, post: MonitorPost) -> str:
    """生成二十字标题文件名，重名时追加作品 ID 后六位。"""

    base = monitor_title(post)
    candidates = [base, f"{base}_{post.video_id[-6:]}", f"{base}_{post.video_id}"]
    for candidate in candidates:
        path = material_dir / f"{candidate}.md"
        if not path.exists() or case_id_from_markdown(path) == post.case_id:
            return candidate
    raise RuntimeError(f"无法为案例生成不冲突的素材文件名：{post.case_id}")


def refresh_monitor_metrics(path: Path, post: MonitorPost, updated_at: str) -> None:
    """只更新四项互动数据和数据更新时间，不触碰逐字稿。"""

    updates = {
        "数据更新时间": updated_at,
        "点赞": post.like_count,
        "收藏": post.fav_count,
        "评论": post.comment_count,
        "转发": post.forward_count,
    }
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    seen: set[str] = set()
    for index, line in enumerate(lines):
        for label, value in updates.items():
            if line.startswith(f"| {label} |"):
                lines[index] = f"| {label} | {monitor_table_value(value)} |"
                seen.add(label)
                break
    missing = [label for label in updates if label not in seen]
    if missing:
        insert_at = next((i + 1 for i, line in enumerate(lines) if line == "|---|---|"), 0)
        rows = [f"| {label} | {monitor_table_value(updates[label])} |" for label in missing]
        lines[insert_at:insert_at] = rows
    atomic_write_text(path, "\n".join(lines) + "\n")


def render_monitor_markdown(
    *,
    post: MonitorPost,
    collected_at: str,
    case_id: str,
    transcript: str,
    segments: list[dict[str, Any]] | None = None,
    duration: float | None = None,
    **_unused: Any,
) -> str:
    """生成只含允许平台数据与逐字稿的监控文案。"""

    status = "已提取" if transcript_source_text(transcript, segments) else "无有效口播"
    rows = monitor_info_rows(post, collected_at, case_id, status, duration)
    paragraphs = paragraphize_transcript(transcript, segments)
    lines = [
        f"# {monitor_title(post)}",
        "",
        "## 基础信息",
        "",
        "| 字段 | 内容 |",
        "|---|---|",
        *[f"| {label} | {monitor_table_value(value)} |" for label, value in rows],
        "",
        "## 清洗逐字稿",
        "",
        *(paragraphs or ["-"]),
        "",
        "## 分句逐字稿",
        "",
        *render_segment_lines(segments),
        "",
    ]
    return "\n".join(lines)


def monitor_info_rows(
    post: MonitorPost,
    discovered_at: str,
    case_id: str,
    status: str,
    duration: float | None,
) -> list[tuple[str, Any]]:
    """构造监控文案基础信息行，禁止加入播放数或阅读数。"""

    return [
        ("案例 ID", case_id),
        ("发现时间", discovered_at),
        ("发布时间", format_timestamp(post.create_time)),
        ("数据更新时间", discovered_at),
        ("平台", PLATFORM_LABELS.get(post.platform, post.platform)),
        ("作者", post.nickname),
        ("username", post.username),
        ("视频 ID", post.video_id),
        ("标题/描述", post.title),
        ("点赞", post.like_count),
        ("收藏", post.fav_count),
        ("评论", post.comment_count),
        ("转发", post.forward_count),
        ("采集状态", status),
        ("原始链接", post.source_url),
        ("时长", seconds_to_time(duration) if duration else ""),
    ]


def format_timestamp(value: int) -> str:
    """把 Unix 时间戳格式化为北京时间。"""

    if value <= 0:
        return ""
    return format_datetime(datetime.fromtimestamp(value, tz=CHINA_TIMEZONE))


def monitor_title(post: MonitorPost) -> str:
    """把平台标题或描述清理并截断为最多二十个字符。"""

    return safe_filename(post.title, post.case_id, limit=MONITOR_TITLE_LIMIT)


def monitor_table_value(value: Any) -> str:
    """转义监控表格值，同时保留有业务含义的数字零。"""

    return table_escape("" if value is None else str(value))


def format_datetime(value: datetime) -> str:
    """把时区时间格式化为本地 Markdown 时间文本。"""

    return ensure_china_time(value).strftime("%Y-%m-%d %H:%M:%S")


def account_to_dict(account: MonitorAccount) -> dict[str, str]:
    """转换账号为 CLI JSON 字段。"""

    return {
        "platform": account.platform,
        "nickname": account.nickname,
        "username": account.username,
        "api_user_id": account.api_user_id,
    }


def atomic_write_text(path: Path, text: str) -> None:
    """在目标目录内以临时文件替换方式写入 UTF-8 文本。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f"{path.name}.tmp")
    try:
        temporary_path.write_text(text, encoding="utf-8")
        temporary_path.replace(path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
