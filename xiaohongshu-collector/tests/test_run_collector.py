import importlib.util
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
SCRIPT_PATH = SCRIPTS_DIR / "run_collector.py"
SPEC = importlib.util.spec_from_file_location("xiaohongshu_run_collector", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

NOTE_1 = "697c0eee000000000a03c301"
NOTE_2 = "697c0eee000000000a03c302"
NOTE_3 = "697c0eee000000000a03c303"
NOW = int(datetime.now(timezone.utc).timestamp() * 1000)


def search_card(note_id, likes=None):
    card = {"id": note_id, "type": "normal", "time": NOW}
    if likes is not None:
        card.update({"liked_count": likes, "collected_count": 0, "comments_count": 0})
    return card


def detail(note_id, likes=150):
    return {
        "code": 200,
        "data": {
            "data": {
                "note_list": [
                    {
                        "id": note_id,
                        "type": "normal",
                        "time": NOW,
                        "title": f"标题-{note_id[-1]}",
                        "desc": "正文",
                        "liked_count": likes,
                        "collected_count": 0,
                        "comments_count": 2,
                        "shared_count": 1,
                        "images_list": [],
                    }
                ]
            }
        },
    }


def comment_page(note_id, page):
    return {
        "code": 200,
        "data": {
            "data": {
                "comments": [
                    {"id": f"{note_id}-c{page}-1", "content": "评论1"},
                    {"id": f"{note_id}-c{page}-2", "content": "评论2"},
                ],
                "cursor": f"cursor-{page}",
                "index": page * 10,
                "has_more": page == 1,
            }
        },
    }


class FakeTikHubClient:
    def __init__(self):
        self.request_attempts = 0
        self.endpoints = []
        self.comment_pages = {NOTE_1: 0, NOTE_2: 0, NOTE_3: 0}

    def request(self, endpoint, params):
        self.request_attempts += 1
        self.endpoints.append(endpoint)
        if endpoint == "search_notes":
            cards = [search_card(NOTE_1, 1), search_card(NOTE_2, 150), search_card(NOTE_3, 200)]
            return {"code": 200, "data": {"items": [{"model_type": "note", "note": item} for item in cards]}}
        if endpoint == "get_image_note_detail":
            return detail(params["note_id"])
        note_id = params["note_id"]
        self.comment_pages[note_id] += 1
        return comment_page(note_id, self.comment_pages[note_id])


class RunnerTests(unittest.TestCase):
    def test_load_token_reads_project_tikhub_api_key(self):
        """采集器应读取项目统一使用的 TIKHUB_API_KEY。"""
        env_path = Path("project.env")
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(Path, "is_file", return_value=True):
                with patch.object(Path, "read_text", return_value="TIKHUB_API_KEY=file-key\n"):
                    self.assertEqual(MODULE.load_token(env_path), "file-key")

    def test_runner_filters_before_detail_and_stops_after_scanned_count(self):
        config = MODULE.core.CollectorConfig(
            keyword="AI公众号",
            collect_count=3,
            time_filter="不限",
            min_likes=100,
            min_collects=None,
            min_comments=None,
        )
        client = FakeTikHubClient()
        local = MODULE.core.LocalWriteResult(Path("fixtures/结果"), Path("detail.md"), Path("comment.md"), 0, ())
        with patch.object(MODULE.core.LocalResultWriter, "write", return_value=local):
            result = MODULE.XiaohongshuRunner(config, client).run()
        self.assertEqual(result.scanned, 3)
        self.assertEqual(result.search_filtered, 1)
        self.assertEqual(result.qualified, 2)
        self.assertEqual(len(result.records), 2)
        self.assertEqual([record.note.likes for record in result.records], [150, 200])
        self.assertEqual([record.note.comments for record in result.records], [0, 0])
        self.assertEqual([len(record.comments) for record in result.records], [0, 0])
        self.assertNotIn("get_note_comments", client.endpoints)
        self.assertEqual(result.request_attempts, 3)
        self.assertEqual(result.stop_reason, "COLLECT_COUNT_REACHED")

    def test_cli_maps_collect_count_time_filter_and_none_thresholds(self):
        args = MODULE.parse_args(
            [
                "collect",
                "AI公众号",
                "--count",
                "5",
                "--time-filter",
                "不限",
                "--min-likes",
                "none",
            ]
        )
        config = MODULE.config_from_args(args)
        self.assertEqual(config.collect_count, 5)
        self.assertEqual(config.time_filter, "不限")
        self.assertIsNone(config.min_likes)
        self.assertFalse(hasattr(config, "metric_logic"))

    def test_request_upper_bound_counts_one_search_request_per_twenty_cards(self):
        self.assertEqual(MODULE.maximum_request_attempts(5), 6)
        self.assertEqual(MODULE.maximum_request_attempts(40), 42)

    def test_cli_no_longer_exposes_metric_logic(self):
        args = MODULE.parse_args(["collect", "词", "--count", "5"])
        self.assertNotIn("metric_logic", vars(args))
        self.assertNotIn("comments_per_note", vars(args))

    def test_missing_feishu_configuration_keeps_local_collection_available(self):
        config = MODULE.core.CollectorConfig(keyword="词", collect_count=5)

        with patch.object(
            MODULE.bitable.FeishuConfig,
            "from_env",
            side_effect=ValueError("飞书配置缺失：FEISHU_APP_ID"),
        ):
            session, message = MODULE.prepare_feishu_session(config)

        self.assertIsNone(session)
        self.assertIn("未写入飞书", message)

    def test_main_finishes_feishu_session_after_local_collection(self):
        local = MODULE.core.LocalWriteResult(Path("fixtures/结果"), Path("detail.md"), Path("comment.md"), 0, ())
        result = MODULE.RunResult((), local, 0, 0, 0, 0, 1, "SEARCH_EXHAUSTED", ())
        session = MagicMock()
        session.finish.return_value = MODULE.feishu_sync.FeishuSyncResult(
            "xhs-task-1",
            MODULE.bitable.UpsertResult(0, 0),
            MODULE.bitable.UpsertResult(0, 0),
            "成功",
        )

        with patch.object(MODULE, "prepare_feishu_session", return_value=(session, "")):
            with patch.object(MODULE, "load_token", return_value="token"):
                with patch.object(MODULE, "TikHubClient"):
                    with patch.object(MODULE, "XiaohongshuRunner") as runner:
                        runner.return_value.run.return_value = result
                        code = MODULE.main(["collect", "词", "--count", "5"])

        self.assertEqual(code, 0)
        session.finish.assert_called_once_with(result.records, has_local_errors=False)

    def test_direct_mode_initializes_fields_before_creating_task(self):
        """字段必须在新建任务记录之前准备好。"""
        config = MODULE.core.CollectorConfig(keyword="词", collect_count=5)
        client = MagicMock()
        with (
            patch.object(MODULE.bitable.FeishuConfig, "from_env"),
            patch.object(MODULE.bitable, "FeishuBitableClient", return_value=client),
        ):
            session, notice = MODULE.prepare_feishu_session(config)
        self.assertIsNotNone(session)
        self.assertEqual(notice, "")
        self.assertEqual(client.method_calls[0], call.ensure_collection_fields())
        self.assertEqual(client.method_calls[1][0], "create_task")

    def test_initialization_failure_stops_both_modes_before_tikhub(self):
        """飞书初始化失败时，两个入口均不得继续付费采集或写记录。"""
        for arguments in (["collect", "词", "--count", "5"], ["tasks"]):
            with self.subTest(arguments=arguments):
                client = MagicMock()
                client.ensure_collection_fields.side_effect = ValueError("标题字段类型不符")
                with (
                    patch.object(MODULE.bitable.FeishuConfig, "from_env"),
                    patch.object(MODULE.bitable, "FeishuBitableClient", return_value=client),
                    patch.object(MODULE, "load_token") as token,
                    patch.object(MODULE, "TikHubClient") as tikhub,
                    patch.object(MODULE, "XiaohongshuRunner") as runner,
                ):
                    self.assertEqual(MODULE.main(arguments), 1)
                token.assert_not_called()
                tikhub.assert_not_called()
                runner.assert_not_called()
                client.create_task.assert_not_called()
                client.list_pending_tasks.assert_not_called()

    def test_shared_task_cache_avoids_duplicate_detail_requests(self):
        config = MODULE.core.CollectorConfig(
            keyword="AI工具",
            collect_count=1,
            min_likes=0,
            min_collects=None,
            min_comments=None,
        )
        local = MODULE.core.LocalWriteResult(Path("fixtures/结果"), Path("detail.md"), Path("comment.md"), 0, ())
        shared = {}

        with patch.object(MODULE.core.LocalResultWriter, "write", return_value=local):
            first = MODULE.XiaohongshuRunner(config, FakeTikHubClient(), shared_records=shared).run()
            second_config = MODULE.core.CollectorConfig(
                **{**vars(config), "keyword": "AI编程"},
            )
            second = MODULE.XiaohongshuRunner(
                second_config,
                FakeTikHubClient(),
                shared_records=shared,
            ).run()

        self.assertEqual(len(first.records), 1)
        self.assertEqual(first.request_attempts, 2)
        self.assertEqual(second.request_attempts, 1)
        self.assertEqual(second.reused_ids, (NOTE_1,))

    def test_tasks_command_reads_pending_tasks_in_order(self):
        pending = [
            {
                "record_id": "task-record-1",
                "fields": {
                    "任务ID": "xhs-task-1",
                    "搜索关键词": "AI工具#AI编程",
                    "排序依据": "综合",
                    "笔记类型": "不限",
                    "发布时间": "不限",
                    "采集数量": 5,
                    "最低点赞数": 100,
                    "最低收藏数": 100,
                    "最低评论数": 20,
                    "采集状态": "未采集",
                },
            },
            {
                "record_id": "task-record-2",
                "fields": {
                    "任务ID": "xhs-task-2",
                    "搜索关键词": "效率工具",
                    "排序依据": "最新",
                    "笔记类型": "视频",
                    "发布时间": "一周内",
                    "采集数量": 3,
                    "最低点赞数": 0,
                    "最低收藏数": 0,
                    "最低评论数": 0,
                    "采集状态": "未采集",
                },
            },
        ]
        client = MagicMock()
        client.list_pending_tasks.return_value = pending
        tikhub_client = MagicMock()
        tikhub_client.request_attempts = 0
        session = MagicMock()
        session.finish_task.return_value = MODULE.feishu_sync.FeishuSyncResult(
            "xhs-task-1",
            MODULE.bitable.UpsertResult(0, 0),
            MODULE.bitable.UpsertResult(0, 0),
            "成功",
        )
        local = MODULE.core.LocalWriteResult(Path("fixtures/结果"), Path("detail.md"), Path("comment.md"), 0, ())
        result = MODULE.RunResult((), local, 5, 0, 5, 0, 1, "COLLECT_COUNT_REACHED", ())

        with patch.object(MODULE.bitable.FeishuConfig, "from_env"):
            with patch.object(MODULE.bitable, "FeishuBitableClient", return_value=client):
                with patch.object(MODULE.feishu_sync.FeishuSyncSession, "attach", return_value=session):
                    with patch.object(MODULE, "load_token", return_value="token"):
                        with patch.object(MODULE, "TikHubClient", return_value=tikhub_client):
                            with patch.object(MODULE, "XiaohongshuRunner") as runner:
                                runner.return_value.run.return_value = result
                                code = MODULE.main(["tasks"])

        self.assertEqual(code, 0)
        self.assertEqual(
            [call.args[0].keyword for call in runner.call_args_list],
            ["AI工具", "AI编程", "效率工具"],
        )
        self.assertEqual(session.finish_task.call_count, 2)

    def test_tasks_command_without_pending_tasks_does_not_require_tikhub_token(self):
        client = MagicMock()
        client.list_pending_tasks.return_value = []

        with patch.object(MODULE.bitable.FeishuConfig, "from_env"):
            with patch.object(MODULE.bitable, "FeishuBitableClient", return_value=client):
                with patch.object(
                    MODULE,
                    "load_token",
                    side_effect=AssertionError("无待采集任务时不应读取 TikHub 密钥"),
                ):
                    code = MODULE.main(["tasks"])

        self.assertEqual(code, 0)
        self.assertEqual(client.method_calls[:2], [call.ensure_collection_fields(), call.list_pending_tasks()])


if __name__ == "__main__":
    unittest.main()
