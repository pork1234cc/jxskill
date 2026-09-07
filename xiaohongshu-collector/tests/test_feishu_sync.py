import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
SCRIPT_PATH = SCRIPTS_DIR / "feishu_sync.py"
SPEC = importlib.util.spec_from_file_location("xiaohongshu_feishu_sync", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def config():
    return MODULE.core.CollectorConfig(
        keyword="AI编程",
        collect_count=5,
        note_type="image",
        time_filter="一周内",
        min_likes=100,
        min_collects=None,
        min_comments=20,
        sort="likes",
    )


def record():
    note = MODULE.core.NormalizedNote(
        note_id="697c0eee000000000a03c301",
        note_type="image",
        publish_time=datetime(2026, 9, 1, tzinfo=timezone.utc),
        likes=300,
        collects=80,
        comments=30,
        shares=10,
        title="AI 编程实践",
        body="正文",
        topics=("AI", "编程"),
        author_nickname="作者",
        user_id="user-1",
        source_url="https://www.xiaohongshu.com/explore/697c0eee000000000a03c301",
        raw={},
    )
    comment = {
        "id": "comment-1",
        "content": "评论正文",
        "like_count": 5,
        "user": {"id": "comment-user", "nickname": "评论者"},
    }
    return MODULE.core.LocalNoteRecord(note, (comment,), {})


class FakeClient:
    def __init__(self):
        self.tasks = []
        self.notes = []
        self.comments = []
        self.statuses = []
        self.task_updates = []

    def create_task(self, fields):
        self.tasks.append(fields)
        return "task-record-1"

    def upsert_notes(self, fields):
        self.notes.extend(fields)
        return MODULE.bitable.UpsertResult(len(fields), 0)

    def upsert_comments(self, fields):
        self.comments.extend(fields)
        return MODULE.bitable.UpsertResult(len(fields), 0)

    def update_task_status(self, record_id, status):
        self.statuses.append((record_id, status))

    def update_task_fields(self, record_id, fields):
        self.task_updates.append((record_id, fields))


class FieldMappingTests(unittest.TestCase):
    def test_task_fields_use_locked_labels(self):
        fields = MODULE.task_fields(config(), "xhs-task-1")

        self.assertEqual(set(fields), set(MODULE.bitable.TASK_FIELDS))
        self.assertEqual(fields["排序依据"], "最多点赞")
        self.assertEqual(fields["笔记类型"], "图文")
        self.assertEqual(fields["发布时间"], "一周内")
        self.assertEqual(fields["最低收藏数"], 0)
        self.assertEqual(fields["采集状态"], "采集中")

    def test_pending_task_splits_hash_keywords_and_applies_count_per_keyword(self):
        pending = {
            "record_id": "task-record-1",
            "fields": {
                "任务ID": "xhs-task-1",
                "搜索关键词": "AI工具# AI编程 ##AI工具",
                "排序依据": "最多点赞",
                "笔记类型": "图文",
                "发布时间": "一周内",
                "采集数量": 40,
                "最低点赞数": 100,
                "最低收藏数": 50,
                "最低评论数": 20,
                "采集状态": "未采集",
            },
        }

        plan = MODULE.pending_task_plan(pending)

        self.assertEqual(plan.record_id, "task-record-1")
        self.assertEqual(plan.task_id, "xhs-task-1")
        self.assertEqual([item.keyword for item in plan.configs], ["AI工具", "AI编程"])
        self.assertEqual([item.collect_count for item in plan.configs], [40, 40])
        self.assertTrue(all(item.time_filter == "一周内" for item in plan.configs))

    def test_pending_task_generates_missing_task_id(self):
        pending = {
            "record_id": "task-record-1",
            "fields": {
                **MODULE.task_fields(config(), "placeholder"),
                "任务ID": "",
                "搜索关键词": "AI工具",
                "采集状态": "未采集",
            },
        }
        with patch.object(MODULE, "make_task_id", return_value="xhs-generated"):
            self.assertEqual(MODULE.pending_task_plan(pending).task_id, "xhs-generated")

    def test_note_fields_use_locked_schema(self):
        item = record()

        note = MODULE.note_fields(item, "AI编程", "xhs-task-1")

        self.assertEqual(set(note), set(MODULE.bitable.NOTE_FIELDS))
        self.assertEqual(note["话题"], "AI, 编程")


class SyncTests(unittest.TestCase):
    def test_session_creates_task_then_writes_records_and_marks_success(self):
        client = FakeClient()
        session = MODULE.FeishuSyncSession.start(client, config(), task_id="xhs-task-1")

        result = session.finish((record(),), has_local_errors=False)

        self.assertEqual(client.tasks[0]["采集状态"], "采集中")
        self.assertEqual(len(client.notes), 1)
        self.assertEqual(client.comments, [])
        self.assertEqual(client.statuses, [("task-record-1", "成功")])
        self.assertEqual(result.notes.created, 1)
        self.assertEqual(result.comments.created, 0)

    def test_local_errors_mark_task_as_partial_success(self):
        client = FakeClient()
        session = MODULE.FeishuSyncSession.start(client, config(), task_id="xhs-task-1")

        session.finish((record(),), has_local_errors=True)

        self.assertEqual(client.statuses, [("task-record-1", "部分成功")])

    def test_attach_claims_existing_task_without_creating(self):
        client = FakeClient()
        plan = MODULE.PendingTaskPlan("task-record-1", "xhs-task-1", (config(),))

        session = MODULE.FeishuSyncSession.attach(client, plan)

        self.assertEqual(client.tasks, [])
        self.assertEqual(
            client.task_updates,
            [("task-record-1", {"任务ID": "xhs-task-1", "采集状态": "采集中"})],
        )
        self.assertEqual(session.task_id, "xhs-task-1")

    def test_task_finish_merges_duplicate_note_sources_without_writing_comments(self):
        client = FakeClient()
        plan = MODULE.PendingTaskPlan("task-record-1", "xhs-task-1", (config(),))
        session = MODULE.FeishuSyncSession.attach(client, plan)
        item = record()

        result = session.finish_task(
            (
                MODULE.TaskNoteRecord("AI工具", item),
                MODULE.TaskNoteRecord("AI编程", item),
            ),
            has_local_errors=False,
        )

        self.assertEqual(len(client.notes), 1)
        self.assertEqual(client.notes[0]["来源关键词"], "AI工具, AI编程")
        self.assertEqual(client.comments, [])
        self.assertEqual(result.notes.created, 1)


if __name__ == "__main__":
    unittest.main()
