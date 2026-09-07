"""把采集完成的对标案例幂等同步到固定飞书案例表。"""
from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from .feishu import (
    FeishuBitableClient,
    FeishuBitableError,
    FeishuTableConfig,
    app_credentials,
    first_value,
    plain_text,
)
from .markdown import (
    clean_metadata_text,
    clean_topics,
    paragraphize_transcript,
    seconds_to_time,
    strip_topics_from_text,
)
from .profile_data import author_info, feed_info

CHINA_TIMEZONE = timezone(timedelta(hours=8))
EXPECTED_FIELD_TYPES = {
    "案例": 1,
    "案例 ID": 1,
    "采集时间": 5,
    "原始链接": 15,
    "平台": 3,
    "作者": 1,
    "书名": 1,
    "标题/描述": 1,
    "话题": 4,
    "点赞": 2,
    "收藏": 2,
    "评论": 2,
    "转发": 2,
    "时长": 1,
    "清洗逐字稿": 1,
}


class ReferenceBitableError(RuntimeError):
    """表示对标案例表字段或记录同步失败。"""


@dataclass(frozen=True, slots=True)
class ReferenceBitableRecord:
    """描述一条待写入采集表的对标案例。"""

    case_id: str
    collected_at: str
    source_url: str
    platform: str
    author: str
    book_name: str
    title: str
    topics: tuple[str, ...]
    like_count: int | None
    favorite_count: int | None
    comment_count: int | None
    forward_count: int | None
    duration: str
    clean_transcript: str

    def to_fields(self) -> dict[str, Any]:
        """转换为飞书字段值；按用户要求不写“类目”。"""
        fields: dict[str, Any] = {
            "案例": self._primary_value(),
            "案例 ID": self.case_id.strip(),
            "采集时间": _timestamp_milliseconds(self.collected_at),
            "平台": self.platform.strip(),
            "作者": self.author.strip(),
            "书名": self.book_name.strip(),
            "标题/描述": self.title.strip(),
            "话题": [item.strip() for item in self.topics if item.strip()],
            "时长": self.duration.strip(),
            "清洗逐字稿": self.clean_transcript.strip(),
        }
        source_url = self.source_url.strip()
        if source_url:
            fields["原始链接"] = {"text": source_url, "link": source_url}
        metrics = {
            "点赞": self.like_count,
            "收藏": self.favorite_count,
            "评论": self.comment_count,
            "转发": self.forward_count,
        }
        fields.update({name: value for name, value in metrics.items() if value is not None})
        return fields

    def _primary_value(self) -> str:
        """生成方便人工浏览的主字段内容。"""
        title = self.book_name.strip() or self.title.strip() or self.case_id.strip()
        parts = (title, self.author.strip(), self.collected_at.strip())
        return "｜".join(part for part in parts if part)


class ReferenceBitableClient(FeishuBitableClient):
    """校验案例表结构并按案例 ID 幂等写入。"""

    def validate_schema(self) -> None:
        """确认所有需要写入的字段存在且类型正确。"""
        if getattr(self, "_schema_validated", False):
            return
        indexed = {
            str(field.get("field_name") or ""): field
            for field in self.list_fields()
            if isinstance(field, dict)
        }
        missing = [name for name in EXPECTED_FIELD_TYPES if name not in indexed]
        if missing:
            raise ReferenceBitableError(f"飞书案例表缺少字段：{'、'.join(missing)}")
        wrong_types = [
            f"{name}(应为{expected}，实际为{indexed[name].get('type')})"
            for name, expected in EXPECTED_FIELD_TYPES.items()
            if indexed[name].get("type") != expected
        ]
        if wrong_types:
            raise ReferenceBitableError(
                f"飞书案例表字段类型不匹配：{'、'.join(wrong_types)}"
            )
        self._schema_validated = True

    def sync(self, record: ReferenceBitableRecord) -> dict[str, str]:
        """新增案例，或更新案例 ID 相同的唯一记录。"""
        case_id = record.case_id.strip()
        if not case_id:
            raise ReferenceBitableError("案例 ID 不能为空。")
        self.validate_schema()
        record_index = getattr(self, "_record_ids_by_case", None)
        if record_index is None:
            record_index = {}
            for item in self.list_records():
                indexed_case_id = plain_text((item.get("fields") or {}).get("案例 ID"))
                record_id = str(item.get("record_id") or "")
                if indexed_case_id and record_id:
                    record_index.setdefault(indexed_case_id, []).append(record_id)
            self._record_ids_by_case = record_index
        existing_ids = record_index.get(case_id, [])
        if len(existing_ids) > 1:
            raise ReferenceBitableError(f"飞书中存在重复案例 ID：{case_id}")
        record_id = self.upsert(record.to_fields(), existing_ids[0] if existing_ids else "")
        record_index[case_id] = [record_id]
        return {
            "record_id": record_id,
            "case_id": case_id,
            "action": "update" if existing_ids else "create",
        }


def build_reference_record_from_markdown(path: Path | str) -> ReferenceBitableRecord:
    """从本地监控 Markdown 重建飞书记录，供失败后的下轮重试。"""

    markdown_path = Path(path)
    try:
        text = markdown_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ReferenceBitableError(f"无法读取本地案例：{markdown_path}") from exc
    fields = _markdown_table_fields(text)
    case_id = fields.get("案例 ID", "").strip()
    collected_at = (
        fields.get("发现时间")
        or fields.get("采集时间")
        or fields.get("数据更新时间")
        or ""
    ).strip()
    if not case_id or not collected_at:
        raise ReferenceBitableError(
            f"本地案例缺少案例 ID 或发现时间：{markdown_path}"
        )
    platform = ""
    for candidate in ("wechat_channels", "douyin"):
        if case_id.startswith(f"{candidate}_"):
            platform = candidate
            break
    if not platform:
        raise ReferenceBitableError(f"本地案例 ID 平台无效：{case_id}")
    return ReferenceBitableRecord(
        case_id=case_id,
        collected_at=collected_at,
        source_url=fields.get("原始链接", ""),
        platform=platform,
        author=fields.get("作者", ""),
        book_name="",
        title=fields.get("标题/描述", ""),
        topics=(),
        like_count=_markdown_count(fields.get("点赞")),
        favorite_count=_markdown_count(fields.get("收藏")),
        comment_count=_markdown_count(fields.get("评论")),
        forward_count=_markdown_count(fields.get("转发")),
        duration=fields.get("时长", ""),
        clean_transcript=_markdown_section(text, "清洗逐字稿"),
    )


def sync_markdown_case(path: Path | str) -> dict[str, str]:
    """把本地案例幂等同步到飞书，用于刷新和失败重试。"""

    return sync_collected_case(build_reference_record_from_markdown(path))


def build_case_id(profile: dict[str, Any], source_url: str) -> str:
    """根据平台和视频对象 ID 生成稳定案例 ID。"""
    platform = str(profile.get("platform") or "video").strip().lower() or "video"
    object_id = str(feed_info(profile).get("objectId") or "").strip()
    if not object_id:
        normalized = source_url.strip().rstrip("/").lower()
        object_id = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return f"{platform}_{object_id}"


def build_reference_record(
    *,
    profile: dict[str, Any],
    source_url: str,
    collected_at: str,
    book_name: str,
    transcript: str,
    segments: list[dict[str, Any]] | None,
    duration: float | None,
) -> ReferenceBitableRecord:
    """从采集内存数据构造案例记录，不反向解析 Markdown。"""
    feed = feed_info(profile)
    author = author_info(profile)
    topics = clean_topics(feed.get("topics") or [])
    title = strip_topics_from_text(feed.get("description"), topics)
    title = title or strip_topics_from_text(feed.get("title"), topics)
    paragraphs = paragraphize_transcript(transcript, segments)
    return ReferenceBitableRecord(
        case_id=build_case_id(profile, source_url),
        collected_at=collected_at,
        source_url=source_url,
        platform=str(profile.get("platform") or ""),
        author=clean_metadata_text(author.get("nickname") or author.get("username") or ""),
        book_name=clean_metadata_text(book_name),
        title=title,
        topics=tuple(topics),
        like_count=_optional_count(feed, "likeCount", "likeCountFmt"),
        favorite_count=_optional_count(feed, "favCount", "favCountFmt"),
        comment_count=_optional_count(feed, "commentCount", "commentCountFmt"),
        forward_count=_optional_count(feed, "forwardCount", "forwardCountFmt"),
        duration=seconds_to_time(duration) if duration else "",
        clean_transcript="\n\n".join(paragraphs),
    )


def sync_collected_case(record: ReferenceBitableRecord) -> dict[str, str]:
    """使用当前环境中的应用凭据同步一条采集案例。"""
    config = _reference_table_config(dict(os.environ))
    try:
        client = _cached_reference_client(
            config.app_id,
            config.app_secret,
            config.app_token,
            config.table_id,
        )
        return client.sync(record)
    except FeishuBitableError as exc:
        raise ReferenceBitableError(str(exc)) from exc


def reference_bitable_client_from_env(
    values: Mapping[str, str] | None = None,
) -> ReferenceBitableClient:
    """使用独立项目配置创建用户案例表客户端。"""

    source = dict(os.environ) if values is None else values
    config = _reference_table_config(source)
    try:
        return ReferenceBitableClient(config)
    except FeishuBitableError as exc:
        raise ReferenceBitableError(str(exc)) from exc


def _reference_table_config(values: Mapping[str, str]) -> FeishuTableConfig:
    """读取用户自己的飞书案例表配置并拒绝缺失值。"""

    app_id, app_secret, app_token = app_credentials(values)
    table_id = first_value(values, "FEISHU_REFERENCE_TABLE_ID")
    if not all((app_id, app_secret, app_token, table_id)):
        raise ReferenceBitableError(
            "飞书案例表配置不完整，需要 FEISHU_APP_ID、FEISHU_APP_SECRET、"
            "FEISHU_APP_TOKEN 和 FEISHU_REFERENCE_TABLE_ID。"
        )
    return FeishuTableConfig(app_id, app_secret, app_token, table_id)


@lru_cache(maxsize=4)
def _cached_reference_client(
    app_id: str,
    app_secret: str,
    app_token: str,
    table_id: str,
) -> ReferenceBitableClient:
    """按飞书配置复用客户端，避免同轮监控重复拉取表结构和记录。"""

    return ReferenceBitableClient(
        FeishuTableConfig(app_id, app_secret, app_token, table_id)
    )


def _timestamp_milliseconds(value: str) -> int:
    """把东八区采集时间转换成毫秒时间戳。"""
    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=CHINA_TIMEZONE
        )
    except ValueError as exc:
        raise ReferenceBitableError(f"采集时间格式无效：{value}") from exc
    return int(parsed.timestamp() * 1000)


def _optional_count(feed: dict[str, Any], raw_key: str, formatted_key: str) -> int | None:
    """读取可无损转换为整数的互动数据。"""
    value = feed.get(raw_key)
    if value in (None, ""):
        value = feed.get(formatted_key)
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _markdown_table_fields(text: str) -> dict[str, str]:
    """读取监控 Markdown 的两列表格，并还原转义的竖线。"""

    fields: dict[str, str] = {}
    for line in text.splitlines():
        protected = line.replace("\\|", "\0")
        parts = protected.split("|")
        if len(parts) != 4 or parts[0].strip() or parts[-1].strip():
            continue
        label = parts[1].replace("\0", "|").strip()
        value = parts[2].replace("\0", "|").strip()
        if label and label not in {"字段", "---"}:
            fields[label] = "" if value == "-" else value
    return fields


def _markdown_section(text: str, heading: str) -> str:
    """读取指定二级标题下、下一个二级标题前的正文。"""

    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$\n(.*?)(?=^##\s+|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text.replace("\r\n", "\n"))
    value = match.group(1).strip() if match else ""
    return "" if value == "-" else value


def _markdown_count(value: str | None) -> int | None:
    """把 Markdown 中可选的互动数转换为整数。"""

    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
