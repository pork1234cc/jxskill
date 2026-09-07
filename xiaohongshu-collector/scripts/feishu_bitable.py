"""小红书采集结果写入飞书多维表格的最小客户端。"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass


OPEN_API_BASE = "https://open.feishu.cn/open-apis"
BATCH_SIZE = 100
FIELD_TYPE_TEXT = 1
FIELD_TYPE_NUMBER = 2
FIELD_TYPE_SINGLE_SELECT = 3
TASK_NUMBER_FIELDS = frozenset({"采集数量", "最低点赞数", "最低收藏数", "最低评论数"})
NOTE_NUMBER_FIELDS = frozenset({"点赞", "收藏", "评论", "转发"})


def resolve_project_root() -> Path:
    """以调用命令时的工作目录作为当前运营项目根目录。"""
    return Path.cwd()


PROJECT_ROOT = resolve_project_root()
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"

TASK_FIELDS = (
    "任务ID",
    "搜索关键词",
    "排序依据",
    "笔记类型",
    "发布时间",
    "采集数量",
    "最低点赞数",
    "最低收藏数",
    "最低评论数",
    "采集状态",
)
TASK_SINGLE_SELECT_OPTIONS = {
    "排序依据": frozenset({"综合", "最新", "最多点赞", "最多评论", "最多收藏"}),
    "笔记类型": frozenset({"不限", "视频", "图文"}),
    "发布时间": frozenset({"不限", "一天内", "一周内", "半年内"}),
    "采集状态": frozenset({"未采集", "采集中", "成功", "部分成功", "失败"}),
}
TASK_MUTABLE_FIELDS = frozenset({"任务ID", "采集状态"})
NOTE_FIELDS = (
    "Note ID",
    "作者昵称",
    "用户ID",
    "标题",
    "正文",
    "话题",
    "点赞",
    "收藏",
    "评论",
    "转发",
    "原文链接",
    "来源关键词",
    "首次任务ID",
)
COMMENT_FIELDS = (
    "评论ID",
    "NOTE ID",
    "用户ID",
    "评论内容",
    "点赞",
    "用户昵称",
)


@dataclass(frozen=True)
class FeishuConfig:
    """飞书应用、任务表、笔记表及预留的可选评论表配置。"""

    app_id: str
    app_secret: str
    app_token: str
    task_table_id: str
    note_table_id: str
    comment_table_id: str = ""

    @classmethod
    def from_env(cls, env_path: Path = DEFAULT_ENV_PATH) -> "FeishuConfig":
        names = {
            "app_id": ("FEISHU_APP_ID",),
            "app_secret": ("FEISHU_APP_SECRET",),
            "app_token": ("NOTE_TOKEN",),
            "task_table_id": ("NOTE_WORK",),
            "note_table_id": ("NOTE_CONTENT",),
        }
        comment_candidates = ("NOTE_COMMENT",)
        values = dict(load_env_values(env_path))
        for candidates in (*names.values(), comment_candidates):
            for name in candidates:
                environment_value = os.environ.get(name, "").strip()
                if environment_value:
                    values[name] = environment_value
        resolved = {
            field_name: next((values[name] for name in candidates if values.get(name)), "")
            for field_name, candidates in names.items()
        }
        missing = [candidates[0] for field_name, candidates in names.items() if not resolved[field_name]]
        if missing:
            raise ValueError(f"飞书配置缺失：{', '.join(missing)}")
        comment_table_id = next((values[name] for name in comment_candidates if values.get(name)), "")
        return cls(**resolved, comment_table_id=comment_table_id)


@dataclass(frozen=True)
class UpsertResult:
    """一次批量写入的新增和更新数量。"""

    created: int
    updated: int


def load_env_values(env_path: Path) -> dict[str, str]:
    """读取 UTF-8 KEY=VALUE 配置，不覆盖进程环境。"""
    if not env_path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip('"\'')
    return values


def validate_fields(fields: dict[str, Any], allowed: Iterable[str], *, require_all: bool) -> None:
    """阻止未确认字段进入飞书。"""
    allowed_set = set(allowed)
    unexpected = set(fields) - allowed_set
    if unexpected:
        raise ValueError(f"包含未确认的飞书字段：{', '.join(sorted(unexpected))}")
    missing = allowed_set - set(fields)
    if require_all and missing:
        raise ValueError(f"缺少飞书字段：{', '.join(sorted(missing))}")


def validate_task_fields(fields: dict[str, Any]) -> None:
    """校验任务表字段、单选值和非负筛选阈值。"""
    validate_fields(fields, TASK_FIELDS, require_all=True)
    for field_name, options in TASK_SINGLE_SELECT_OPTIONS.items():
        if fields[field_name] not in options:
            raise ValueError(f"{field_name} 只支持：{', '.join(sorted(options))}")
    for field_name in ("最低点赞数", "最低收藏数", "最低评论数"):
        value = fields[field_name]
        if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0):
            raise ValueError(f"{field_name} 必须为空或非负数字")
    collect_count = fields["采集数量"]
    if not isinstance(collect_count, (int, float)) or isinstance(collect_count, bool) or collect_count <= 0:
        raise ValueError("采集数量必须是大于 0 的数字")


def merge_keywords(existing: Any, incoming: Any) -> str:
    """合并来源关键词，保持首次出现顺序。"""
    keywords: list[str] = []
    for value in (existing, incoming):
        for keyword in str(value or "").split(","):
            cleaned = keyword.strip()
            if cleaned and cleaned not in keywords:
                keywords.append(cleaned)
    return ", ".join(keywords)


def chunks(items: list[dict[str, Any]], size: int = BATCH_SIZE) -> Iterable[list[dict[str, Any]]]:
    """将飞书批量请求拆为固定大小。"""
    for start in range(0, len(items), size):
        yield items[start : start + size]


class FeishuBitableClient:
    """只写已锁定三表字段的飞书多维表格客户端。"""

    def __init__(self, config: FeishuConfig) -> None:
        self.config = config
        self._tenant_access_token = ""

    def ensure_collection_fields(self) -> None:
        """先校验两张表，再只创建缺失字段；配置冲突或请求失败时抛出 ValueError。"""
        if self.config.task_table_id == self.config.note_table_id:
            raise ValueError("NOTE_WORK 和 NOTE_CONTENT 必须配置为不同的数据表")
        tables = (
            (self.config.task_table_id, "采集任务表", TASK_FIELDS, TASK_NUMBER_FIELDS, TASK_SINGLE_SELECT_OPTIONS),
            (self.config.note_table_id, "笔记详情表", NOTE_FIELDS, NOTE_NUMBER_FIELDS, {}),
        )
        missing = [
            (table_id, label, self._missing_fields(table_id, label, names, numbers, selects))
            for table_id, label, names, numbers, selects in tables
        ]
        for table_id, label, definitions in missing:
            for definition in definitions:
                try:
                    self._request("POST", self._fields_path(table_id), definition)
                except ValueError as exc:
                    raise ValueError(f"{label}创建字段“{definition['field_name']}”失败：{exc}") from exc

    def _missing_fields(
        self,
        table_id: str,
        label: str,
        names: Iterable[str],
        numbers: frozenset[str],
        selects: dict[str, frozenset[str]],
    ) -> list[dict[str, Any]]:
        """读取已有字段并验证类型和选项，返回待创建定义，不修改已有字段。"""
        indexed = {field["field_name"]: field for field in self.list_fields(table_id)}
        missing = []
        for name in names:
            field_type = FIELD_TYPE_NUMBER if name in numbers else FIELD_TYPE_TEXT
            definition: dict[str, Any] = {"field_name": name, "type": field_type}
            if name in selects:
                definition["type"] = FIELD_TYPE_SINGLE_SELECT
                definition["property"] = {"options": [{"name": value} for value in sorted(selects[name])]}
            elif name in numbers:
                definition["property"] = {"formatter": "0"}
            current = indexed.get(name)
            if current is None:
                missing.append(definition)
                continue
            if current.get("type") != definition["type"]:
                raise ValueError(f"{label}字段“{name}”类型不符：需要 {definition['type']}，实际 {current.get('type')}")
            if name in selects:
                properties = current.get("property") or {}
                options = properties.get("options") or []
                existing = {option.get("name") for option in options if isinstance(option, dict)}
                absent = selects[name] - existing
                if absent:
                    raise ValueError(f"{label}字段“{name}”缺少单选选项：{', '.join(sorted(absent))}")
        return missing

    def list_fields(self, table_id: str) -> list[dict[str, Any]]:
        """分页读取字段；响应或分页异常时停止，避免把未读到的字段误判为缺失。"""
        fields: list[dict[str, Any]] = []
        page_token = ""
        seen_tokens: set[str] = set()
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            data = self._request("GET", self._fields_path(table_id), params=params)
            items = data.get("items")
            if not isinstance(items, list) or any(
                not isinstance(item, dict) or not item.get("field_name") or "type" not in item
                for item in items
            ):
                raise ValueError("飞书字段列表响应无效，已停止初始化")
            fields.extend(items)
            if not data.get("has_more"):
                return fields
            next_token = str(data.get("page_token") or "")
            if not next_token or next_token in seen_tokens:
                raise ValueError("飞书字段列表分页异常，已停止初始化")
            seen_tokens.add(next_token)
            page_token = next_token

    def _fields_path(self, table_id: str) -> str:
        """生成字段接口路径，不使用记录接口修改表结构。"""
        app_token = quote(self.config.app_token, safe="")
        return f"/bitable/v1/apps/{app_token}/tables/{quote(table_id, safe='')}/fields"

    def create_task(self, fields: dict[str, Any]) -> str:
        """新增一条完整采集任务记录。"""
        validate_task_fields(fields)
        data = self._request("POST", self._records_path(self.config.task_table_id), {"fields": fields})
        record = data.get("record") if isinstance(data.get("record"), dict) else {}
        return str(record.get("record_id") or "")

    def update_task_status(self, record_id: str, status: str) -> None:
        """只更新任务表的单选采集状态。"""
        self.update_task_fields(record_id, {"采集状态": status})

    def update_task_fields(self, record_id: str, fields: dict[str, Any]) -> None:
        """只更新已有任务的任务 ID 和采集状态。"""
        if not record_id:
            raise ValueError("record_id 不能为空")
        if not fields or set(fields) - TASK_MUTABLE_FIELDS:
            raise ValueError("任务更新只支持任务ID和采集状态")
        task_id = fields.get("任务ID")
        if "任务ID" in fields and not str(task_id or "").strip():
            raise ValueError("任务ID不能为空")
        status = fields.get("采集状态")
        if "采集状态" in fields and status not in TASK_SINGLE_SELECT_OPTIONS["采集状态"]:
            raise ValueError("采集状态值无效")
        path = f"{self._records_path(self.config.task_table_id)}/{quote(record_id, safe='')}"
        self._request("PUT", path, {"fields": fields})

    def list_pending_tasks(self) -> list[dict[str, Any]]:
        """按飞书返回顺序读取采集状态为未采集的任务。"""
        records = self.list_records(self.config.task_table_id)
        return [
            record
            for record in records
            if isinstance(record.get("fields"), dict) and record["fields"].get("采集状态") == "未采集"
        ]

    def upsert_notes(self, records: list[dict[str, Any]]) -> UpsertResult:
        """按 Note ID 新增或更新笔记，保留首次任务ID。"""
        for fields in records:
            validate_fields(fields, NOTE_FIELDS, require_all=True)
        return self._upsert(
            self.config.note_table_id,
            "Note ID",
            records,
            preserve_first_task=True,
        )

    def upsert_comments(self, records: list[dict[str, Any]]) -> UpsertResult:
        """按评论ID新增或更新一级评论。"""
        for fields in records:
            validate_fields(fields, COMMENT_FIELDS, require_all=True)
        return self._upsert(self.config.comment_table_id, "评论ID", records)

    def list_records(self, table_id: str) -> list[dict[str, Any]]:
        """分页读取一张表，用于批量构建去重索引。"""
        records: list[dict[str, Any]] = []
        page_token = ""
        seen_tokens: set[str] = set()
        while True:
            params: dict[str, Any] = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token
            data = self._request("GET", self._records_path(table_id), params=params)
            items = data.get("items") if isinstance(data.get("items"), list) else []
            records.extend(item for item in items if isinstance(item, dict))
            next_token = str(data.get("page_token") or "")
            if not data.get("has_more") or not next_token or next_token in seen_tokens:
                break
            seen_tokens.add(next_token)
            page_token = next_token
        return records

    def _upsert(
        self,
        table_id: str,
        key_field: str,
        records: list[dict[str, Any]],
        *,
        preserve_first_task: bool = False,
    ) -> UpsertResult:
        existing = self._index_records(table_id, key_field)
        creates: list[dict[str, Any]] = []
        updates: list[dict[str, Any]] = []
        for fields in records:
            key = str(fields.get(key_field) or "").strip()
            if not key:
                raise ValueError(f"{key_field} 不能为空")
            current = existing.get(key)
            if current is None:
                creates.append({"fields": fields})
                continue
            update_fields = self._update_fields(fields, current.get("fields") or {}, preserve_first_task)
            updates.append({"record_id": current["record_id"], "fields": update_fields})
        self._batch_create(table_id, creates)
        self._batch_update(table_id, updates)
        return UpsertResult(len(creates), len(updates))

    def _index_records(self, table_id: str, key_field: str) -> dict[str, dict[str, Any]]:
        indexed: dict[str, dict[str, Any]] = {}
        for record in self.list_records(table_id):
            fields = record.get("fields") if isinstance(record.get("fields"), dict) else {}
            key = str(fields.get(key_field) or "").strip()
            record_id = str(record.get("record_id") or "")
            if key and record_id:
                indexed[key] = {"record_id": record_id, "fields": fields}
        return indexed

    @staticmethod
    def _update_fields(
        incoming: dict[str, Any],
        existing: dict[str, Any],
        preserve_first_task: bool,
    ) -> dict[str, Any]:
        fields = dict(incoming)
        if preserve_first_task:
            fields.pop("首次任务ID", None)
            fields["来源关键词"] = merge_keywords(existing.get("来源关键词"), incoming.get("来源关键词"))
        return fields

    def _batch_create(self, table_id: str, records: list[dict[str, Any]]) -> None:
        path = f"{self._records_path(table_id)}/batch_create"
        for batch in chunks(records):
            self._request("POST", path, {"records": batch})

    def _batch_update(self, table_id: str, records: list[dict[str, Any]]) -> None:
        path = f"{self._records_path(table_id)}/batch_update"
        for batch in chunks(records):
            self._request("POST", path, {"records": batch})

    def _records_path(self, table_id: str) -> str:
        app_token = quote(self.config.app_token, safe="")
        return f"/bitable/v1/apps/{app_token}/tables/{quote(table_id, safe='')}/records"

    def _get_tenant_access_token(self) -> str:
        if self._tenant_access_token:
            return self._tenant_access_token
        data = self._request(
            "POST",
            "/auth/v3/tenant_access_token/internal",
            {"app_id": self.config.app_id, "app_secret": self.config.app_secret},
            authenticated=False,
        )
        token = str(data.get("tenant_access_token") or "")
        if not token:
            raise ValueError("飞书响应中缺少 tenant_access_token")
        self._tenant_access_token = token
        return token

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        params: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        query = f"?{urlencode(params)}" if params else ""
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self._get_tenant_access_token()}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = Request(f"{OPEN_API_BASE}{path}{query}", data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=30) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"飞书请求失败：{exc}") from exc
        if not isinstance(result, dict) or result.get("code") != 0:
            message = result.get("msg") if isinstance(result, dict) else "无效响应"
            raise ValueError(f"飞书请求失败：{message}")
        data = result.get("data")
        return data if isinstance(data, dict) else result
