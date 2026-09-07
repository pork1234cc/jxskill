"""小红书关键词采集的配置、标准化、准入判断和分页核心。"""

from __future__ import annotations

import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass


TEST_COLLECT_COUNT = 5
NORMAL_COLLECT_COUNT = 40
COMMENT_PAGE_LIMIT = 3
TIME_FILTER_OPTIONS = {"不限", "一天内", "一周内", "半年内"}
NOTE_TYPE_OPTIONS = {"all", "image", "video"}
SORT_OPTIONS = {"general", "latest", "likes", "comments", "collects"}
NOTE_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{24}$")
COUNT_PATTERN = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([万千wk]?)$", re.IGNORECASE)
UTC = timezone.utc
def resolve_project_root() -> Path:
    """以调用命令时的工作目录作为当前运营项目根目录。"""
    return Path.cwd()


PROJECT_ROOT = resolve_project_root()
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "projects" / "03小红书" / "01小红书素材"

SORT_API_VALUES = {
    "general": "general",
    "latest": "time_descending",
    "likes": "popularity_descending",
    "comments": "comment_descending",
    "collects": "collect_descending",
}
NOTE_TYPE_API_VALUES = {
    "all": "不限",
    "image": "普通笔记",
    "video": "视频笔记",
}


class GateDecision(str, Enum):
    """一层准入判断的结果。"""

    PASS = "pass"
    FAIL = "fail"


@dataclass(frozen=True)
class CollectorConfig:
    """MVP 已锁定的采集参数。"""

    keyword: str
    collect_count: int = NORMAL_COLLECT_COUNT
    note_type: str = "all"
    time_filter: str = "不限"
    min_likes: int | None = 100
    min_collects: int | None = 100
    min_comments: int | None = 20
    sort: str = "general"

    def __post_init__(self) -> None:
        object.__setattr__(self, "keyword", self.keyword.strip())
        self._validate()

    @classmethod
    def preset(cls, keyword: str, mode: str, **overrides: Any) -> "CollectorConfig":
        """创建测试或正常采集预设。"""
        if mode not in {"test", "normal"}:
            raise ValueError("mode 只支持 test 或 normal")
        collect_count = TEST_COLLECT_COUNT if mode == "test" else NORMAL_COLLECT_COUNT
        return cls(keyword=keyword, collect_count=collect_count, **overrides)

    def _validate(self) -> None:
        if not self.keyword:
            raise ValueError("搜索关键词不能为空")
        if self.collect_count <= 0:
            raise ValueError("collect_count 必须大于 0")
        if self.note_type not in NOTE_TYPE_OPTIONS:
            raise ValueError("note_type 只支持 all、image 或 video")
        if self.time_filter not in TIME_FILTER_OPTIONS:
            raise ValueError("time_filter 只支持不限、一天内、一周内或半年内")
        if self.sort not in SORT_OPTIONS:
            raise ValueError("sort 只支持 general、latest、likes、comments 或 collects")
        for name, value in self.metric_thresholds().items():
            if value is not None and value < 0:
                raise ValueError(f"{name} 不能小于 0")

    def metric_thresholds(self) -> dict[str, int | None]:
        """返回参与准入判断的互动指标。"""
        return {
            "likes": self.min_likes,
            "collects": self.min_collects,
            "comments": self.min_comments,
        }


@dataclass(frozen=True)
class NormalizedNote:
    """搜索卡片和详情共用的最小标准化字段。"""

    note_id: str
    note_type: str | None
    publish_time: datetime | None
    likes: int | None
    collects: int | None
    comments: int | None
    shares: int | None
    title: str
    body: str
    topics: tuple[str, ...]
    author_nickname: str
    user_id: str
    source_url: str
    raw: dict[str, Any] = field(compare=False, repr=False)


@dataclass(frozen=True)
class LocalNoteRecord:
    """一篇合格笔记和搜索卡片；评论字段预留给后续接入。"""

    note: NormalizedNote
    comments: tuple[dict[str, Any], ...] = ()
    search_raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class LocalWriteResult:
    """一次本地保存的路径和素材结果。"""

    run_dir: Path
    detail_path: Path
    comment_path: Path | None
    media_downloads: int
    errors: tuple[str, ...]


@dataclass
class CollectionState:
    """跟踪已读取搜索卡片、唯一笔记和最终合格笔记。"""

    config: CollectorConfig
    scanned_count: int = 0
    scanned_ids: set[str] = field(default_factory=set)
    qualified_ids: set[str] = field(default_factory=set)
    search_filtered: int = 0
    detail_filtered: int = 0

    @property
    def should_stop(self) -> bool:
        return self.scan_limit_reached

    @property
    def scan_limit_reached(self) -> bool:
        return self.scanned_count >= self.config.collect_count

    @property
    def stop_reason(self) -> str | None:
        if self.scan_limit_reached:
            return "COLLECT_COUNT_REACHED"
        return None

    def register_search(
        self,
        raw_note: dict[str, Any],
        now: datetime | None = None,
    ) -> tuple[GateDecision, NormalizedNote] | None:
        """登记一张搜索卡片；重复或缺失 Note ID 只计数并跳过。"""
        if self.should_stop:
            return None
        self.scanned_count += 1
        note = normalize_note(raw_note)
        if not note.note_id or note.note_id in self.scanned_ids:
            self.search_filtered += 1
            return GateDecision.FAIL, note
        self.scanned_ids.add(note.note_id)
        decision = evaluate_search_note(note, self.config)
        if decision is GateDecision.FAIL:
            self.search_filtered += 1
        return decision, note

    def register_detail(
        self,
        raw_note: dict[str, Any],
        now: datetime | None = None,
    ) -> tuple[GateDecision, NormalizedNote]:
        """登记已通过搜索 Gate 的详情，不重复执行筛选。"""
        note = normalize_note(raw_note)
        if not note.note_id or note.note_id not in self.scanned_ids:
            self.detail_filtered += 1
            return GateDecision.FAIL, note
        self.qualified_ids.add(note.note_id)
        return GateDecision.PASS, note


def find_first_alias(payload: Any, aliases: Iterable[str]) -> Any:
    """递归查找第一个命中的字段别名。"""
    alias_set = set(aliases)
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in alias_set and value is not None:
                return value
        for value in payload.values():
            found = find_first_alias(value, alias_set)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = find_first_alias(value, alias_set)
            if found is not None:
                return found
    return None


def parse_count(value: Any) -> int | None:
    """解析整数及带万、千、w、k后缀的互动数。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value).strip().replace(",", "")
    match = COUNT_PATTERN.fullmatch(text)
    if not match:
        return None
    multiplier = {"": 1, "万": 10_000, "千": 1_000, "w": 10_000, "k": 1_000}
    return int(float(match.group(1)) * multiplier[match.group(2).lower()])


def parse_publish_time(value: Any) -> datetime | None:
    """解析秒、毫秒时间戳或 ISO 时间。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return _parse_iso_time(str(value))
    if timestamp > 10_000_000_000:
        timestamp /= 1000
    try:
        return datetime.fromtimestamp(timestamp, UTC)
    except (OSError, OverflowError, ValueError):
        return None


def _parse_iso_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def normalize_note(raw_note: dict[str, Any]) -> NormalizedNote:
    """将搜索卡片或详情转换为统一字段。"""
    note_id = str(find_first_alias(raw_note, ("note_id", "id")) or "").strip()
    author = find_first_alias(raw_note, ("user", "user_info", "author"))
    author = author if isinstance(author, dict) else {}
    return NormalizedNote(
        note_id=note_id if NOTE_ID_PATTERN.fullmatch(note_id) else "",
        note_type=normalize_note_type(find_first_alias(raw_note, ("type", "note_type"))),
        publish_time=parse_publish_time(
            find_first_alias(raw_note, ("publish_time", "create_time", "timestamp", "time"))
        ),
        likes=parse_count(find_first_alias(raw_note, ("liked_count", "like_count", "likes"))),
        collects=parse_count(
            find_first_alias(raw_note, ("collected_count", "collect_count", "collects"))
        ),
        comments=parse_count(
            find_first_alias(raw_note, ("comments_count", "comment_count", "comments"))
        ),
        shares=parse_count(find_first_alias(raw_note, ("shared_count", "share_count", "shares"))),
        title=str(find_first_alias(raw_note, ("title",)) or "").strip() or "未命名笔记",
        body=str(find_first_alias(raw_note, ("desc", "description")) or "").strip(),
        topics=tuple(topic_names(raw_note)),
        author_nickname=str(find_first_alias(author, ("nickname", "name")) or "").strip(),
        user_id=str(find_first_alias(author, ("user_id", "id")) or "").strip(),
        source_url=note_source_url(raw_note, note_id),
        raw=raw_note,
    )


def topic_names(raw_note: dict[str, Any]) -> list[str]:
    """合并 topics 和 hash_tag 中的话题并去重。"""
    names: list[str] = []
    for field_name in ("topics", "hash_tag"):
        topics = find_first_alias(raw_note, (field_name,))
        if not isinstance(topics, list):
            continue
        for topic in topics:
            name = str(topic.get("name") or "").strip() if isinstance(topic, dict) else ""
            if name and name not in names:
                names.append(name)
    return names


def note_source_url(raw_note: dict[str, Any], note_id: str) -> str:
    """优先读取原文链接，否则按合法 Note ID 生成标准链接。"""
    source_url = str(find_first_alias(raw_note, ("share_url", "note_url", "url")) or "").strip()
    if source_url:
        return source_url
    return f"https://www.xiaohongshu.com/explore/{note_id}" if note_id else ""


def normalize_note_type(value: Any) -> str | None:
    """统一图文和视频类型。"""
    text = str(value or "").strip().lower()
    if text in {"normal", "image", "图文", "普通笔记"}:
        return "image"
    if text in {"video", "视频", "视频笔记"}:
        return "video"
    return None


def evaluate_search_note(note: NormalizedNote, config: CollectorConfig) -> GateDecision:
    """只使用搜索卡片中的类型和互动指标决定是否进入详情采集。"""
    checks = (_type_decision(note, config), _metric_decision(note, config))
    return GateDecision.PASS if all(check is GateDecision.PASS for check in checks) else GateDecision.FAIL


def _type_decision(note: NormalizedNote, config: CollectorConfig) -> GateDecision:
    if config.note_type == "all":
        return GateDecision.PASS
    if note.note_type is None:
        return GateDecision.FAIL
    return GateDecision.PASS if note.note_type == config.note_type else GateDecision.FAIL


def _metric_decision(note: NormalizedNote, config: CollectorConfig) -> GateDecision:
    thresholds = {key: value for key, value in config.metric_thresholds().items() if value is not None and value > 0}
    if not thresholds:
        return GateDecision.PASS
    values = {"likes": note.likes, "collects": note.collects, "comments": note.comments}
    if any(values[key] is None for key in thresholds):
        return GateDecision.FAIL
    return (
        GateDecision.PASS
        if all(values[key] is not None and values[key] >= thresholds[key] for key in thresholds)
        else GateDecision.FAIL
    )


def build_search_params(
    config: CollectorConfig,
    page: int,
    search_id: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """生成 TikHub 搜索参数。"""
    if page <= 0:
        raise ValueError("page 必须从 1 开始")
    return {
        "keyword": config.keyword,
        "page": page,
        "sort_type": SORT_API_VALUES[config.sort],
        "note_type": NOTE_TYPE_API_VALUES[config.note_type],
        "time_filter": config.time_filter,
        "search_id": search_id,
        "search_session_id": session_id,
        "source": "explore_feed",
        "ai_mode": 0,
    }


def extract_top_level_comments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """只返回当前响应中的一级评论列表。"""
    comments = find_first_alias(payload, ("comments",))
    if not isinstance(comments, list):
        return []
    return [comment for comment in comments if isinstance(comment, dict)]


def comment_identity(comment: dict[str, Any]) -> str:
    """优先使用评论ID，缺失时生成当前运行内稳定的去重键。"""
    comment_id = find_first_alias(comment, ("comment_id", "id"))
    if comment_id:
        return str(comment_id)
    user_id = find_first_alias(comment.get("user") or {}, ("user_id", "id"))
    return "|".join(
        [str(user_id or ""), str(comment.get("time") or ""), str(comment.get("content") or "")]
    )


def merge_comment_pages(
    pages: Iterable[dict[str, Any]],
    comments_per_note: int,
) -> list[dict[str, Any]]:
    """合并最多三页一级评论，并按配置条数截断。"""
    if comments_per_note < 0:
        raise ValueError("comments_per_note 不能小于 0")
    if comments_per_note == 0:
        return []
    comments: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page_number, payload in enumerate(pages, start=1):
        if page_number > COMMENT_PAGE_LIMIT:
            break
        for comment in extract_top_level_comments(payload):
            identity = comment_identity(comment)
            if identity in seen:
                continue
            seen.add(identity)
            comments.append(comment)
            if len(comments) >= comments_per_note:
                return comments
    return comments


def safe_name(value: str, limit: int = 60) -> str:
    """生成 Windows 可用的目录或文件名。"""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" ._")
    return re.sub(r"\s+", "-", cleaned)[:limit] or "未命名关键词"


def image_urls(raw_note: dict[str, Any]) -> list[str]:
    """提取图文笔记原图地址并去重。"""
    images = find_first_alias(raw_note, ("images_list", "images"))
    if not isinstance(images, list):
        return []
    urls: list[str] = []
    for image in images:
        if not isinstance(image, dict):
            continue
        url = str(image.get("original") or image.get("url") or "").strip()
        if _is_http_url(url) and url not in urls:
            urls.append(url)
    return urls


def video_url(raw_note: dict[str, Any]) -> str:
    """从搜索卡片优先提取 H.264 视频地址。"""
    media = ((raw_note.get("video_info_v2") or {}).get("media") or {})
    streams = (media.get("stream") or {}).get("h264") or []
    for stream in streams:
        url = str(stream.get("master_url") or "") if isinstance(stream, dict) else ""
        if _is_http_url(url):
            return url
    opaque = ((media.get("video") or {}).get("opaque1") or {})
    for key in ("default_screencast_stream", "hd_screencast_stream"):
        url = str(opaque.get(key) or "")
        if _is_http_url(url):
            return url
    return ""


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def render_detail_section(note: NormalizedNote, position: int) -> str:
    """将笔记渲染为详情 Markdown 分节。"""
    topics = " ".join(f"#{name}" for name in note.topics) or "无"
    body = note.body or "（无正文）"
    return "\n".join(
        [
            f"## {position}. {note.title}",
            "",
            f"- Note ID：{note.note_id}",
            f"- 话题：{topics}",
            f"- 点赞：{_count_text(note.likes)}",
            f"- 收藏：{_count_text(note.collects)}",
            f"- 分享：{_count_text(note.shares)}",
            f"- 评论：{_count_text(note.comments)}",
            "",
            "### 正文",
            "",
            body,
        ]
    )


def render_comment_section(record: LocalNoteRecord, position: int) -> str:
    """将一篇笔记的一级评论渲染为 Markdown 分节。"""
    lines = [f"## {position}. {record.note.title}", "", f"- Note ID：{record.note.note_id}", ""]
    if not record.comments:
        return "\n".join(lines + ["（无一级评论）"])
    for index, comment in enumerate(record.comments, start=1):
        lines.extend(_comment_lines(comment, index))
    return "\n".join(lines).rstrip()


def _comment_lines(comment: dict[str, Any], position: int) -> list[str]:
    user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
    return [
        f"### 评论 {position}",
        "",
        f"- 评论ID：{comment_identity(comment)}",
        f"- 用户ID：{find_first_alias(user, ('user_id', 'id')) or ''}",
        f"- 用户昵称：{find_first_alias(user, ('nickname', 'name')) or ''}",
        f"- 点赞：{_count_text(parse_count(comment.get('like_count')))}",
        "",
        str(comment.get("content") or "").strip() or "（空评论）",
        "",
    ]


def _count_text(value: int | None) -> str:
    return str(value) if value is not None else "0"


def render_document(title: str, sections: Iterable[str]) -> str:
    """将多个分节合并为一份 Markdown。"""
    body = "\n\n---\n\n".join(sections) or "（未采集到内容）"
    return f"# {title}\n\n{body.rstrip()}\n"


def write_markdown(path: Path, content: str) -> None:
    """以 UTF-8 和 LF 写入 Markdown。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def download_url(url: str, target: Path, timeout: int = 60) -> None:
    """以流式方式下载图片或视频。"""
    request = Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": "https://www.xiaohongshu.com/"},
    )
    with urlopen(request, timeout=timeout) as response, target.open("wb") as output:
        shutil.copyfileobj(response, output)


class LocalResultWriter:
    """按关键词保存聚合 Markdown 和 Note ID 素材。"""

    def __init__(
        self,
        config: CollectorConfig,
        output_root: Path = DEFAULT_OUTPUT_ROOT,
        downloader: Callable[[str, Path], None] = download_url,
    ) -> None:
        self.config = config
        self.keyword_name = safe_name(config.keyword)
        self.run_dir = output_root / self.keyword_name
        self.details_dir = self.run_dir / "01笔记详情"
        self.media_dir = self.run_dir / "03笔记素材"
        self.downloader = downloader
        self.media_downloads = 0
        self.errors: list[str] = []

    def write(self, records: Iterable[LocalNoteRecord]) -> LocalWriteResult:
        """保存本次全部合格笔记。"""
        record_list = list(records)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        detail_path = self.details_dir / f"{self.keyword_name}.md"
        detail_sections = [render_detail_section(record.note, index) for index, record in enumerate(record_list, 1)]
        write_markdown(detail_path, render_document(f"{self.config.keyword}·小红书笔记详情", detail_sections))
        for record in record_list:
            self._write_media(record)
        return LocalWriteResult(
            self.run_dir,
            detail_path,
            None,
            self.media_downloads,
            tuple(self.errors),
        )

    def _write_media(self, record: LocalNoteRecord) -> None:
        note_dir = self.media_dir / record.note.note_id
        if record.note.note_type == "video":
            url = video_url(record.search_raw) or video_url(record.note.raw)
            if url:
                self._download_one(url, note_dir / "video.mp4", record.note.note_id)
            else:
                self.errors.append(f"{record.note.note_id} 未找到视频地址")
            return
        for position, url in enumerate(image_urls(record.note.raw), start=1):
            self._download_one(url, note_dir / f"{position:03d}.webp", record.note.note_id)

    def _download_one(self, url: str, target: Path, note_id: str) -> None:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            self.downloader(url, target)
            self.media_downloads += 1
        except (HTTPError, URLError, OSError) as exc:
            self.errors.append(f"{note_id} 素材下载失败：{exc}")
