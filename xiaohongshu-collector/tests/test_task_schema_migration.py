import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "migrate_task_schema.py"
SPEC = importlib.util.spec_from_file_location("xiaohongshu_task_schema_migration", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ThresholdParsingTests(unittest.TestCase):
    def test_old_combined_filter_is_split_into_three_numbers(self):
        self.assertEqual(
            MODULE.parse_thresholds("点赞≥100 OR 收藏≥50 OR 评论≥10"),
            {"最低点赞数": 100, "最低收藏数": 50, "最低评论数": 10},
        )

    def test_missing_thresholds_become_empty_values(self):
        self.assertEqual(
            MODULE.parse_thresholds("收藏>=80"),
            {"最低点赞数": None, "最低收藏数": 80, "最低评论数": None},
        )


class FakeClient:
    def __init__(self):
        self.config = type("Config", (), {"app_token": "app", "task_table_id": "table"})()
        self.calls = []
        self.fields = [
            {"field_id": "sort", "field_name": "排序依据", "type": 1},
            {"field_id": "note", "field_name": "笔记类型", "type": 1},
            {"field_id": "time", "field_name": "发布时间", "type": 1},
            {"field_id": "status", "field_name": "采集状态", "type": 1},
            {"field_id": "old", "field_name": "筛选标准（点赞，收藏，评论）", "type": 1},
        ]

    def list_records(self, table_id):
        return [
            {
                "record_id": "record-1",
                "fields": {
                    "排序依据": "综合",
                    "笔记类型": "全部",
                    "发布时间": "不限",
                    "筛选标准（点赞，收藏，评论）": "点赞≥100 OR 收藏≥50 OR 评论≥10",
                },
            }
        ]

    def _records_path(self, table_id):
        return f"/bitable/v1/apps/app/tables/{table_id}/records"

    def _request(self, method, path, payload=None, *, params=None):
        self.calls.append((method, path, payload))
        if method == "GET":
            return {"items": list(self.fields)}
        if method == "POST" and path.endswith("/fields"):
            created = {
                "field_id": f"new-{len(self.fields)}",
                "field_name": payload["field_name"],
                "type": payload["type"],
            }
            self.fields.append(created)
            return {"field": created}
        return {}


class MigrationTests(unittest.TestCase):
    def test_exact_single_select_options_are_not_rewritten(self):
        client = FakeClient()
        indexed = {
            field_name: {
                "field_id": field_name,
                "field_name": field_name,
                "type": 3,
                "property": {"options": [{"name": name} for name in option_names]},
            }
            for field_name, option_names in MODULE.SINGLE_SELECT_FIELDS.items()
        }
        self.assertEqual(MODULE.update_single_selects(client, indexed, include_legacy=True), 0)
        self.assertEqual(client.calls, [])

    def test_old_field_is_deleted_only_after_backfill_and_select_updates(self):
        client = FakeClient()
        MODULE.migrate_task_schema(client)
        methods_and_paths = [(method, path) for method, path, _ in client.calls]
        record_update = next(index for index, item in enumerate(methods_and_paths) if "/records/record-1" in item[1])
        old_delete = next(index for index, item in enumerate(methods_and_paths) if item[0] == "DELETE")
        select_updates = [index for index, item in enumerate(methods_and_paths) if item[0] == "PUT" and "/fields/" in item[1]]
        self.assertTrue(select_updates)
        self.assertLess(record_update, old_delete)
        self.assertLess(max(select_updates), old_delete)
        select_payloads = [payload for method, path, payload in client.calls if method == "PUT" and "/fields/" in path]
        by_name = {payload["field_name"]: [option["name"] for option in payload["property"]["options"]] for payload in select_payloads}
        self.assertEqual(by_name["排序依据"], ["综合", "最新", "最多点赞", "最多评论", "最多收藏"])
        self.assertEqual(by_name["笔记类型"], ["不限", "视频", "图文"])
        self.assertEqual(by_name["采集状态"], ["未采集", "采集中", "成功", "部分成功", "失败"])
        record_payloads = [payload for method, path, payload in client.calls if method == "PUT" and "/records/" in path]
        self.assertIn({"fields": {"笔记类型": "不限"}}, record_payloads)


if __name__ == "__main__":
    unittest.main()
