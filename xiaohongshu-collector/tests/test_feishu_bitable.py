import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "feishu_bitable.py"
SPEC = importlib.util.spec_from_file_location("xiaohongshu_feishu_bitable", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def config():
    return MODULE.FeishuConfig("app-id", "secret", "app-token", "task-table", "note-table", "comment-table")


def task_fields():
    return {
        "任务ID": "task-1",
        "搜索关键词": "AI公众号",
        "排序依据": "综合",
        "笔记类型": "不限",
        "发布时间": "半年内",
        "采集数量": 5,
        "最低点赞数": 100,
        "最低收藏数": 100,
        "最低评论数": 20,
        "采集状态": "未采集",
    }


def note_fields(note_id="note-1", keyword="AI公众号", task_id="task-1"):
    return {
        "Note ID": note_id,
        "作者昵称": "作者",
        "用户ID": "user-1",
        "标题": "标题",
        "正文": "正文",
        "话题": "AI, 公众号",
        "点赞": 100,
        "收藏": 80,
        "评论": 30,
        "转发": 10,
        "原文链接": "https://www.xiaohongshu.com/explore/note-1",
        "来源关键词": keyword,
        "首次任务ID": task_id,
    }


def comment_fields(comment_id="comment-1"):
    return {
        "评论ID": comment_id,
        "NOTE ID": "note-1",
        "用户ID": "comment-user",
        "评论内容": "评论正文",
        "点赞": 5,
        "用户昵称": "评论者",
    }


class ProjectRootTests(unittest.TestCase):
    def test_project_root_uses_runtime_working_directory(self) -> None:
        workspace = Path("fixtures/xiaohongshu-workbench")

        with patch.object(MODULE.Path, "cwd", return_value=workspace):
            self.assertEqual(MODULE.resolve_project_root(), workspace)


class SchemaTests(unittest.TestCase):
    def test_locked_field_names_are_exact(self):
        self.assertEqual(set(task_fields()), set(MODULE.TASK_FIELDS))
        self.assertEqual(set(note_fields()), set(MODULE.NOTE_FIELDS))
        self.assertEqual(set(comment_fields()), set(MODULE.COMMENT_FIELDS))

    def test_unconfirmed_or_missing_fields_are_rejected(self):
        with self.assertRaises(ValueError):
            MODULE.validate_fields({**task_fields(), "额外字段": "禁止"}, MODULE.TASK_FIELDS, require_all=True)
        incomplete = task_fields()
        incomplete.pop("采集状态")
        with self.assertRaises(ValueError):
            MODULE.validate_fields(incomplete, MODULE.TASK_FIELDS, require_all=True)

    def test_task_single_select_values_follow_xiaohongshu_options(self):
        MODULE.validate_task_fields(task_fields())
        invalid = task_fields()
        invalid["发布时间"] = "30天"
        with self.assertRaisesRegex(ValueError, "发布时间"):
            MODULE.validate_task_fields(invalid)
        invalid = task_fields()
        invalid["排序依据"] = "最热"
        with self.assertRaisesRegex(ValueError, "排序依据"):
            MODULE.validate_task_fields(invalid)

    def test_collect_count_must_be_positive(self):
        invalid = task_fields()
        invalid["采集数量"] = 0
        with self.assertRaisesRegex(ValueError, "采集数量"):
            MODULE.validate_task_fields(invalid)

    def test_missing_environment_configuration_is_reported(self):
        with (
            patch.dict(MODULE.os.environ, {}, clear=True),
            patch.object(MODULE, "load_env_values", return_value={"FEISHU_APP_ID": "id"}),
        ):
            with self.assertRaisesRegex(ValueError, "NOTE_TOKEN, NOTE_WORK, NOTE_CONTENT"):
                MODULE.FeishuConfig.from_env(Path("unused.env"))

    def test_comment_table_configuration_is_not_required(self):
        values = {
            "FEISHU_APP_ID": "app-id",
            "FEISHU_APP_SECRET": "secret",
            "NOTE_TOKEN": "app-token",
            "NOTE_WORK": "task-table",
            "NOTE_CONTENT": "note-table",
        }
        with patch.dict(MODULE.os.environ, {}, clear=True):
            with patch.object(MODULE, "load_env_values", return_value=values):
                result = MODULE.FeishuConfig.from_env(Path("unused.env"))
        self.assertEqual(result.comment_table_id, "")

    def test_note_configuration_is_loaded_from_env_file(self):
        values = {
            "FEISHU_APP_ID": "app-id",
            "FEISHU_APP_SECRET": "secret",
            "NOTE_TOKEN": "app-token",
            "NOTE_WORK": "task-table",
            "NOTE_CONTENT": "note-table",
            "NOTE_COMMENT": "comment-table",
        }
        with (
            patch.dict(MODULE.os.environ, {}, clear=True),
            patch.object(MODULE, "load_env_values", return_value=values),
        ):
            result = MODULE.FeishuConfig.from_env(Path("unused.env"))
        self.assertEqual(result, config())

    def test_process_environment_overrides_env_file(self):
        values = {
            "FEISHU_APP_ID": "app-id",
            "FEISHU_APP_SECRET": "secret",
            "NOTE_TOKEN": "app-token",
            "NOTE_WORK": "task-table",
            "NOTE_CONTENT": "note-table",
            "NOTE_COMMENT": "comment-table",
        }

        with patch.dict(MODULE.os.environ, values, clear=True):
            with patch.object(MODULE, "load_env_values", return_value=dict.fromkeys(values, "file-value")):
                result = MODULE.FeishuConfig.from_env(Path("unused.env"))

        self.assertEqual(result, config())


class FieldInitializationTests(unittest.TestCase):
    """验证首次建字段、重复执行和失败恢复时的远端行为。"""

    def setUp(self):
        self.client = MODULE.FeishuBitableClient(config())
        self.tables = {"task-table": [], "note-table": []}
        self.writes = []

    def request(self, method, path, payload=None, **kwargs):
        """用内存表模拟字段接口，不连接飞书。"""
        table_id = path.split("/")[-2]
        self.assertTrue(path.endswith("/fields"))
        self.assertIn(table_id, self.tables)
        if method == "GET":
            return {"items": list(self.tables[table_id]), "has_more": False}
        self.assertEqual(method, "POST")
        self.writes.append((table_id, payload))
        self.tables[table_id].append(dict(payload))
        return {"field": payload}

    def test_empty_tables_create_all_fields_and_second_run_is_read_only(self):
        with patch.object(self.client, "_request", side_effect=self.request):
            self.client.ensure_collection_fields()
            self.assertEqual(len(self.writes), 23)
            self.writes.clear()
            self.client.ensure_collection_fields()
        self.assertEqual(self.writes, [])
        task = {field["field_name"]: field for field in self.tables["task-table"]}
        note = {field["field_name"]: field for field in self.tables["note-table"]}
        self.assertEqual(set(task), set(task_fields()))
        self.assertEqual(set(note), set(note_fields()))
        self.assertEqual(task["搜索关键词"]["type"], 1)
        self.assertEqual(task["采集数量"]["type"], 2)
        self.assertEqual(task["采集状态"]["type"], 3)
        self.assertEqual(
            {option["name"] for option in task["采集状态"]["property"]["options"]},
            {"未采集", "采集中", "成功", "部分成功", "失败"},
        )
        for name in ("点赞", "收藏", "评论", "转发"):
            self.assertEqual(note[name]["type"], 2)
        for name in ("Note ID", "用户ID", "原文链接", "正文"):
            self.assertEqual(note[name]["type"], 1)

    def test_existing_and_unrelated_fields_are_preserved(self):
        existing = {"field_id": "fld-title", "field_name": "标题", "type": 1}
        unrelated = {"field_id": "fld-custom", "field_name": "自用备注", "type": 1}
        self.tables["note-table"] = [existing, unrelated]
        with patch.object(self.client, "_request", side_effect=self.request):
            self.client.ensure_collection_fields()
        self.assertEqual(self.tables["note-table"][:2], [existing, unrelated])
        self.assertEqual(len(self.writes), 22)

    def test_type_conflict_in_second_table_stops_before_any_creation(self):
        self.tables["note-table"] = [{"field_name": "点赞", "type": 1}]
        with patch.object(self.client, "_request", side_effect=self.request):
            with self.assertRaisesRegex(ValueError, "笔记详情表.*点赞.*类型"):
                self.client.ensure_collection_fields()
        self.assertEqual(self.writes, [])

    def test_incomplete_existing_select_options_are_reported_without_changes(self):
        self.tables["task-table"] = [{
            "field_name": "采集状态", "type": 3,
            "property": {"options": [{"name": "未采集", "id": "opt-1"}]},
        }]
        with patch.object(self.client, "_request", side_effect=self.request):
            with self.assertRaisesRegex(ValueError, "采集状态.*缺少单选选项"):
                self.client.ensure_collection_fields()
        self.assertEqual(self.writes, [])

    def test_partial_creation_can_resume_without_duplicates(self):
        def fail_after_three(method, path, payload=None, **kwargs):
            if method == "POST" and len(self.writes) == 3:
                raise ValueError("无字段写入权限")
            return self.request(method, path, payload, **kwargs)

        with patch.object(self.client, "_request", side_effect=fail_after_three):
            with self.assertRaisesRegex(ValueError, "无字段写入权限"):
                self.client.ensure_collection_fields()
        with patch.object(self.client, "_request", side_effect=self.request):
            self.client.ensure_collection_fields()
        self.assertEqual(len(self.writes), 23)

    def test_list_fields_reads_all_pages(self):
        first = {"field_name": "自用备注", "type": 1}
        second = {"field_name": "标题", "type": 1}
        responses = [
            {"items": [first], "has_more": True, "page_token": "next"},
            {"items": [second], "has_more": False},
        ]
        with patch.object(self.client, "_request", side_effect=responses) as request:
            self.assertEqual(self.client.list_fields("note-table"), [first, second])
        self.assertEqual(request.call_args.kwargs["params"]["page_token"], "next")

    def test_invalid_field_list_or_pagination_is_rejected(self):
        for response in ({}, {"items": [None]}, {"items": [], "has_more": True}):
            with self.subTest(response=response):
                with patch.object(self.client, "_request", return_value=response) as request:
                    with self.assertRaises(ValueError):
                        self.client.ensure_collection_fields()
                self.assertTrue(all(call.args[0] == "GET" for call in request.call_args_list))


class ClientTests(unittest.TestCase):
    def test_create_task_sends_only_locked_fields(self):
        client = MODULE.FeishuBitableClient(config())
        with patch.object(client, "_request", return_value={"record": {"record_id": "record-1"}}) as request:
            record_id = client.create_task(task_fields())
        self.assertEqual(record_id, "record-1")
        self.assertEqual(request.call_args.args[2], {"fields": task_fields()})

    def test_list_records_follows_page_tokens(self):
        client = MODULE.FeishuBitableClient(config())
        responses = [
            {"items": [{"record_id": "r1", "fields": {}}], "has_more": True, "page_token": "next"},
            {"items": [{"record_id": "r2", "fields": {}}], "has_more": False},
        ]
        with patch.object(client, "_request", side_effect=responses) as request:
            records = client.list_records("note-table")
        self.assertEqual([record["record_id"] for record in records], ["r1", "r2"])
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[1].kwargs["params"]["page_token"], "next")

    def test_list_pending_tasks_preserves_table_order(self):
        client = MODULE.FeishuBitableClient(config())
        records = [
            {"record_id": "r1", "fields": {"采集状态": "成功"}},
            {"record_id": "r2", "fields": {"采集状态": "未采集", "搜索关键词": "AI工具"}},
            {"record_id": "r3", "fields": {"采集状态": "未采集", "搜索关键词": "AI编程"}},
        ]
        with patch.object(client, "list_records", return_value=records):
            pending = client.list_pending_tasks()
        self.assertEqual([record["record_id"] for record in pending], ["r2", "r3"])

    def test_update_task_fields_updates_existing_record_without_creating(self):
        client = MODULE.FeishuBitableClient(config())
        with patch.object(client, "_request", return_value={}) as request:
            client.update_task_fields("record-1", {"任务ID": "xhs-task-1", "采集状态": "采集中"})
        self.assertEqual(
            request.call_args.args[2],
            {"fields": {"任务ID": "xhs-task-1", "采集状态": "采集中"}},
        )

    def test_upsert_notes_updates_existing_and_preserves_first_task(self):
        client = MODULE.FeishuBitableClient(config())
        existing = [
            {
                "record_id": "record-old",
                "fields": {"Note ID": "note-1", "来源关键词": "旧关键词", "首次任务ID": "task-old"},
            }
        ]
        incoming = [note_fields(keyword="新关键词", task_id="task-new"), note_fields("note-2")]
        with (
            patch.object(client, "list_records", return_value=existing),
            patch.object(client, "_batch_create") as create,
            patch.object(client, "_batch_update") as update,
        ):
            result = client.upsert_notes(incoming)
        update_fields = update.call_args.args[1][0]["fields"]
        self.assertEqual(result, MODULE.UpsertResult(created=1, updated=1))
        self.assertEqual(update_fields["来源关键词"], "旧关键词, 新关键词")
        self.assertNotIn("首次任务ID", update_fields)
        self.assertEqual(create.call_args.args[1][0]["fields"]["Note ID"], "note-2")

    def test_upsert_comments_uses_comment_id(self):
        client = MODULE.FeishuBitableClient(config())
        existing = [{"record_id": "comment-record", "fields": {"评论ID": "comment-1"}}]
        with (
            patch.object(client, "list_records", return_value=existing),
            patch.object(client, "_batch_create") as create,
            patch.object(client, "_batch_update") as update,
        ):
            result = client.upsert_comments([comment_fields("comment-1"), comment_fields("comment-2")])
        self.assertEqual(result, MODULE.UpsertResult(created=1, updated=1))
        self.assertEqual(create.call_args.args[1][0]["fields"]["评论ID"], "comment-2")
        self.assertEqual(update.call_args.args[1][0]["record_id"], "comment-record")


if __name__ == "__main__":
    unittest.main()
