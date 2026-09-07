"""把一次小红书本地采集结果同步到已锁定的飞书三表。"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import collector as core
import feishu_bitable as bitable

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass


SORT_LABELS = {
    "general": "综合",
    "latest": "最新",
    "likes": "最多点赞",
    "comments": "最多评论",
    "collects": "最多收藏",
}
NOTE_TYPE_LABELS = {"all": "不限", "video": "视频", "image": "图文"}
SORT_VALUES = {label: value for value, label in SORT_LABELS.items()}
NOTE_TYPE_VALUES = {label: value for value, label in NOTE_TYPE_LABELS.items()}


@dataclass(frozen=True)
class FeishuSyncResult:
    """一次飞书同步的任务 ID 与批量写入统计。"""

    task_id: str
    notes: bitable.UpsertResult
    comments: bitable.UpsertResult
    status: str


@dataclass(frozen=True)
class PendingTaskPlan:
    """一条飞书未采集任务及其按关键词拆分后的配置。"""

    record_id: str
    task_id: str
    configs: tuple[core.CollectorConfig, ...]


@dataclass(frozen=True)
class TaskNoteRecord:
    """带命中关键词的一篇任务级笔记。"""

    keyword: str
    record: core.LocalNoteRecord


def make_task_id(now: datetime | None = None) -> str:
    """生成便于检索且不易碰撞的采集任务 ID。"""
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return f"xhs-{timestamp:%Y%m%d%H%M%S}-{uuid4().hex[:8]}"


def task_fields(config: core.CollectorConfig, task_id: str) -> dict[str, object]:
    """把采集配置转换为锁定的任务表字段。"""
    return {
        "任务ID": task_id,
        "搜索关键词": config.keyword,
        "排序依据": SORT_LABELS[config.sort],
        "笔记类型": NOTE_TYPE_LABELS[config.note_type],
        "发布时间": config.time_filter,
        "采集数量": config.collect_count,
        "最低点赞数": config.min_likes or 0,
        "最低收藏数": config.min_collects or 0,
        "最低评论数": config.min_comments or 0,
        "采集状态": "采集中",
    }


def split_keywords(value: Any) -> tuple[str, ...]:
    """按半角井号拆分关键词，清理空值并保持首次出现顺序。"""
    keywords: list[str] = []
    for part in str(value or "").split("#"):
        keyword = part.strip()
        if keyword and keyword not in keywords:
            keywords.append(keyword)
    return tuple(keywords)


def _task_integer(fields: dict[str, Any], field_name: str, *, positive: bool) -> int:
    """读取飞书数字字段并拒绝小数、布尔值和越界值。"""
    value = fields.get(field_name)
    if value in (None, "") and not positive:
        return 0
    if isinstance(value, bool):
        raise ValueError(f"{field_name} 必须是整数")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是整数") from exc
    if not number.is_integer() or (positive and number <= 0) or (not positive and number < 0):
        boundary = "大于 0" if positive else "非负"
        raise ValueError(f"{field_name} 必须是{boundary}整数")
    return int(number)


def pending_task_plan(record: dict[str, Any]) -> PendingTaskPlan:
    """把一条飞书未采集记录转换为按关键词顺序执行的任务计划。"""
    record_id = str(record.get("record_id") or "").strip()
    fields = record.get("fields") if isinstance(record.get("fields"), dict) else {}
    if not record_id:
        raise ValueError("飞书采集任务缺少 record_id")
    if fields.get("采集状态") != "未采集":
        raise ValueError("只允许执行采集状态为未采集的任务")
    keywords = split_keywords(fields.get("搜索关键词"))
    if not keywords:
        raise ValueError("搜索关键词不能为空")
    sort = SORT_VALUES.get(str(fields.get("排序依据") or ""))
    note_type = NOTE_TYPE_VALUES.get(str(fields.get("笔记类型") or ""))
    if sort is None or note_type is None:
        raise ValueError("排序依据或笔记类型不是已支持的单选值")
    common = {
        "collect_count": _task_integer(fields, "采集数量", positive=True),
        "note_type": note_type,
        "time_filter": str(fields.get("发布时间") or "不限"),
        "min_likes": _task_integer(fields, "最低点赞数", positive=False),
        "min_collects": _task_integer(fields, "最低收藏数", positive=False),
        "min_comments": _task_integer(fields, "最低评论数", positive=False),
        "sort": sort,
    }
    configs = tuple(core.CollectorConfig(keyword=keyword, **common) for keyword in keywords)
    task_id = str(fields.get("任务ID") or "").strip() or make_task_id()
    return PendingTaskPlan(record_id, task_id, configs)


def note_fields(
    record: core.LocalNoteRecord,
    keyword: str,
    task_id: str,
) -> dict[str, object]:
    """把一篇合格笔记转换为锁定的笔记详情字段。"""
    note = record.note
    return {
        "Note ID": note.note_id,
        "作者昵称": note.author_nickname,
        "用户ID": note.user_id,
        "标题": note.title,
        "正文": note.body,
        "话题": ", ".join(note.topics),
        "点赞": note.likes or 0,
        "收藏": note.collects or 0,
        "评论": note.comments or 0,
        "转发": note.shares or 0,
        "原文链接": note.source_url,
        "来源关键词": keyword,
        "首次任务ID": task_id,
    }


def comment_fields(record: core.LocalNoteRecord) -> list[dict[str, object]]:
    """把一篇笔记的一级评论转换为锁定的评论表字段。"""
    fields: list[dict[str, object]] = []
    for comment in record.comments:
        user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
        fields.append(
            {
                "评论ID": core.comment_identity(comment),
                "NOTE ID": record.note.note_id,
                "用户ID": str(core.find_first_alias(user, ("user_id", "id")) or ""),
                "评论内容": str(comment.get("content") or "").strip(),
                "点赞": core.parse_count(comment.get("like_count")) or 0,
                "用户昵称": str(core.find_first_alias(user, ("nickname", "name")) or ""),
            }
        )
    return fields


class FeishuSyncSession:
    """跟踪一条飞书采集任务，并在采集完成后批量同步。"""

    def __init__(
        self,
        client: bitable.FeishuBitableClient,
        task_id: str,
        task_record_id: str,
        default_keyword: str = "",
    ) -> None:
        self.client = client
        self.task_id = task_id
        self.task_record_id = task_record_id
        self.default_keyword = default_keyword

    @classmethod
    def start(
        cls,
        client: bitable.FeishuBitableClient,
        config: core.CollectorConfig,
        *,
        task_id: str | None = None,
    ) -> "FeishuSyncSession":
        resolved_task_id = task_id or make_task_id()
        record_id = client.create_task(task_fields(config, resolved_task_id))
        if not record_id:
            raise ValueError("飞书创建采集任务后未返回 record_id")
        return cls(client, resolved_task_id, record_id, config.keyword)

    @classmethod
    def attach(
        cls,
        client: bitable.FeishuBitableClient,
        plan: PendingTaskPlan,
    ) -> "FeishuSyncSession":
        """领取已有未采集任务，不创建重复任务记录。"""
        client.update_task_fields(
            plan.record_id,
            {"任务ID": plan.task_id, "采集状态": "采集中"},
        )
        return cls(client, plan.task_id, plan.record_id)

    def finish(
        self,
        records: Iterable[core.LocalNoteRecord],
        *,
        has_local_errors: bool,
    ) -> FeishuSyncResult:
        """同步单关键词即时采集结果并结束任务。"""
        collected = tuple(TaskNoteRecord(self.default_keyword, record) for record in records)
        return self.finish_task(collected, has_local_errors=has_local_errors)

    def finish_task(
        self,
        records: Iterable[TaskNoteRecord],
        *,
        has_local_errors: bool,
    ) -> FeishuSyncResult:
        """按 Note ID 合并多关键词来源，再同步一次笔记和评论。"""
        merged: dict[str, tuple[core.LocalNoteRecord, list[str]]] = {}
        for item in records:
            note_id = item.record.note.note_id
            if note_id not in merged:
                merged[note_id] = (item.record, [])
            keywords = merged[note_id][1]
            if item.keyword and item.keyword not in keywords:
                keywords.append(item.keyword)
        notes = [
            note_fields(record, ", ".join(keywords), self.task_id)
            for record, keywords in merged.values()
        ]
        status = "部分成功" if has_local_errors else "成功"
        try:
            note_result = self.client.upsert_notes(notes) if notes else bitable.UpsertResult(0, 0)
            comment_result = bitable.UpsertResult(0, 0)
            self.client.update_task_status(self.task_record_id, status)
        except ValueError:
            self._best_effort_status("部分成功")
            raise
        return FeishuSyncResult(self.task_id, note_result, comment_result, status)

    def fail(self) -> None:
        """采集主流程失败时，尽力把已创建任务标记为失败。"""
        self._best_effort_status("失败")

    def _best_effort_status(self, status: str) -> None:
        try:
            self.client.update_task_status(self.task_record_id, status)
        except ValueError:
            pass
