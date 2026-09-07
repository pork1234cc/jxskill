"""将小红书采集任务表迁移为单选条件和独立数字阈值。"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from feishu_bitable import FeishuBitableClient, FeishuConfig

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass


OLD_FILTER_FIELD = "筛选标准（点赞，收藏，评论）"
NUMBER_FIELDS = ("最低点赞数", "最低收藏数", "最低评论数")
SINGLE_SELECT_FIELDS = {
    "排序依据": ("综合", "最新", "最多点赞", "最多评论", "最多收藏"),
    "笔记类型": ("不限", "视频", "图文"),
    "发布时间": ("不限", "一天内", "一周内", "半年内"),
    "采集状态": ("未采集", "采集中", "成功", "部分成功", "失败"),
}
FIELD_TYPE_NUMBER = 2
FIELD_TYPE_SINGLE_SELECT = 3
THRESHOLD_PATTERN = re.compile(r"(点赞|收藏|评论)\s*(?:≥|>=)\s*(\d+)")
LEGACY_SELECT_VALUES = {
    "排序依据": {"最热": "最多点赞"},
    "笔记类型": {"全部": "不限"},
}


def parse_thresholds(value: Any) -> dict[str, int | None]:
    """把旧合并字段解析为三个独立阈值。"""
    parsed = {name: int(number) for name, number in THRESHOLD_PATTERN.findall(str(value or ""))}
    return {
        "最低点赞数": parsed.get("点赞"),
        "最低收藏数": parsed.get("收藏"),
        "最低评论数": parsed.get("评论"),
    }


def fields_path(client: FeishuBitableClient) -> str:
    app_token = quote(client.config.app_token, safe="")
    table_id = quote(client.config.task_table_id, safe="")
    return f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields"


def list_fields(client: FeishuBitableClient) -> list[dict[str, Any]]:
    """读取任务表全部字段。"""
    data = client._request("GET", fields_path(client), params={"page_size": 100})
    items = data.get("items") if isinstance(data.get("items"), list) else []
    return [item for item in items if isinstance(item, dict)]


def create_number_fields(client: FeishuBitableClient, indexed: dict[str, dict[str, Any]]) -> int:
    """先创建缺失的三个数字字段，确保旧字段仍可回退。"""
    created = 0
    for field_name in NUMBER_FIELDS:
        if field_name in indexed:
            continue
        client._request(
            "POST",
            fields_path(client),
            {"field_name": field_name, "type": FIELD_TYPE_NUMBER, "property": {"formatter": "0"}},
        )
        created += 1
    return created


def backfill_thresholds(client: FeishuBitableClient) -> int:
    """在删除旧字段前，把每条旧值写入三个数字字段。"""
    updated = 0
    records_path = client._records_path(client.config.task_table_id)
    for record in client.list_records(client.config.task_table_id):
        fields = record.get("fields") if isinstance(record.get("fields"), dict) else {}
        if OLD_FILTER_FIELD not in fields:
            continue
        record_id = str(record.get("record_id") or "")
        if not record_id:
            raise ValueError("任务记录缺少 record_id，迁移已停止")
        path = f"{records_path}/{quote(record_id, safe='')}"
        client._request("PUT", path, {"fields": parse_thresholds(fields.get(OLD_FILTER_FIELD))})
        updated += 1
    return updated


def update_single_selects(
    client: FeishuBitableClient,
    indexed: dict[str, dict[str, Any]],
    *,
    include_legacy: bool = False,
) -> int:
    """把排序、类型、发布时间和采集状态转换为指定单选字段。"""
    updated = 0
    for field_name, option_names in SINGLE_SELECT_FIELDS.items():
        field = indexed.get(field_name)
        if not field:
            raise ValueError(f"任务表缺少字段：{field_name}")
        field_id = str(field.get("field_id") or "")
        if not field_id:
            raise ValueError(f"{field_name} 缺少 field_id")
        property_value = field.get("property") if isinstance(field.get("property"), dict) else {}
        current_options = property_value.get("options") if isinstance(property_value.get("options"), list) else []
        current_names = [str(option.get("name") or "") for option in current_options if isinstance(option, dict)]
        if field.get("type") == FIELD_TYPE_SINGLE_SELECT and current_names == list(option_names):
            continue
        names = list(option_names)
        if include_legacy:
            for legacy_name in LEGACY_SELECT_VALUES.get(field_name, {}):
                if legacy_name not in names:
                    names.append(legacy_name)
        path = f"{fields_path(client)}/{quote(field_id, safe='')}"
        payload = {
            "field_name": field_name,
            "type": FIELD_TYPE_SINGLE_SELECT,
            "property": {"options": [{"name": name} for name in names]},
        }
        client._request("PUT", path, payload)
        updated += 1
    return updated


def normalize_legacy_select_values(client: FeishuBitableClient) -> int:
    """把旧单选标签改为用户确认的新标签。"""
    updated = 0
    records_path = client._records_path(client.config.task_table_id)
    for record in client.list_records(client.config.task_table_id):
        fields = record.get("fields") if isinstance(record.get("fields"), dict) else {}
        replacements: dict[str, str] = {}
        for field_name, mapping in LEGACY_SELECT_VALUES.items():
            current = fields.get(field_name)
            if current in mapping:
                replacements[field_name] = mapping[current]
        if not replacements:
            continue
        record_id = str(record.get("record_id") or "")
        if not record_id:
            raise ValueError("任务记录缺少 record_id，单选值迁移已停止")
        path = f"{records_path}/{quote(record_id, safe='')}"
        client._request("PUT", path, {"fields": replacements})
        updated += 1
    return updated


def delete_old_filter_field(client: FeishuBitableClient, indexed: dict[str, dict[str, Any]]) -> bool:
    """只在新字段回填和单选更新完成后删除旧合并字段。"""
    old_field = indexed.get(OLD_FILTER_FIELD)
    if not old_field:
        return False
    field_id = str(old_field.get("field_id") or "")
    if not field_id:
        raise ValueError(f"{OLD_FILTER_FIELD} 缺少 field_id")
    client._request("DELETE", f"{fields_path(client)}/{quote(field_id, safe='')}")
    return True


def migrate_task_schema(client: FeishuBitableClient) -> dict[str, int | bool]:
    """按创建、回填、改类型、删除的安全顺序迁移任务表。"""
    indexed = {field.get("field_name"): field for field in list_fields(client)}
    created = create_number_fields(client, indexed)
    backfilled = backfill_thresholds(client)
    select_updates = update_single_selects(client, indexed, include_legacy=True)
    normalized = normalize_legacy_select_values(client)
    select_updates += update_single_selects(client, indexed)
    deleted = delete_old_filter_field(client, indexed)
    return {
        "created_number_fields": created,
        "backfilled_records": backfilled,
        "updated_single_select_fields": select_updates,
        "normalized_select_records": normalized,
        "deleted_old_field": deleted,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="迁移小红书采集任务表字段")
    parser.add_argument("--apply", action="store_true", help="确认执行飞书表结构迁移")
    args = parser.parse_args(argv)
    if not args.apply:
        print("未执行迁移；确认后请传入 --apply。")
        return 0
    try:
        result = migrate_task_schema(FeishuBitableClient(FeishuConfig.from_env()))
    except ValueError as exc:
        print(f"迁移失败：{exc}", file=sys.stderr)
        return 1
    print(
        "迁移完成："
        f"新建数字字段 {result['created_number_fields']} 个，"
        f"回填记录 {result['backfilled_records']} 条，"
        f"更新单选字段 {result['updated_single_select_fields']} 个，"
        f"规范单选记录 {result['normalized_select_records']} 条，"
        f"删除旧字段：{'是' if result['deleted_old_field'] else '否'}。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
