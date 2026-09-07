import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "collector.py"
SPEC = importlib.util.spec_from_file_location("xiaohongshu_collector", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

NOTE_ID = "697c0eee000000000a03c308"
OTHER_NOTE_ID = "697c0eee000000000a03c309"
NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


def note(
    note_id: str = NOTE_ID,
    *,
    note_type: str | None = "normal",
    days_ago: int | None = 2,
    likes=100,
    collects=100,
    comments=20,
):
    result = {
        "id": note_id,
        "liked_count": likes,
        "collected_count": collects,
        "comments_count": comments,
    }
    if note_type is not None:
        result["type"] = note_type
    if days_ago is not None:
        result["time"] = int((NOW - timedelta(days=days_ago)).timestamp() * 1000)
    return result


class ProjectRootTests(unittest.TestCase):
    def test_project_root_uses_runtime_working_directory(self) -> None:
        workspace = Path("fixtures/xiaohongshu-workbench")

        with patch.object(MODULE.Path, "cwd", return_value=workspace):
            self.assertEqual(MODULE.resolve_project_root(), workspace)

    def test_default_output_uses_xiaohongshu_project_directory(self) -> None:
        self.assertEqual(
            MODULE.DEFAULT_OUTPUT_ROOT,
            MODULE.PROJECT_ROOT / "projects" / "03小红书" / "01小红书素材",
        )


class CollectorConfigTests(unittest.TestCase):
    def test_mode_presets_use_confirmed_search_counts(self):
        test_config = MODULE.CollectorConfig.preset("关键词", "test")
        self.assertEqual(test_config.collect_count, 5)
        self.assertFalse(hasattr(test_config, "comments_per_note"))
        self.assertEqual(MODULE.CollectorConfig.preset("关键词", "normal").collect_count, 40)

    def test_invalid_limits_are_rejected(self):
        invalid = (
            {"keyword": ""},
            {"keyword": "词", "collect_count": 0},
            {"keyword": "词", "min_likes": -1},
            {"keyword": "词", "time_filter": "30天"},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                MODULE.CollectorConfig(**values)

    def test_api_filters_map_to_supported_values(self):
        config = MODULE.CollectorConfig(
            keyword="AI选题",
            collect_count=5,
            note_type="video",
            time_filter="半年内",
            sort="likes",
        )
        params = MODULE.build_search_params(config, 1)
        self.assertEqual(params["note_type"], "视频笔记")
        self.assertEqual(params["time_filter"], "半年内")
        self.assertEqual(params["sort_type"], "popularity_descending")
        self.assertEqual(
            MODULE.build_search_params(MODULE.CollectorConfig(keyword="词", sort="comments"), 1)["sort_type"],
            "comment_descending",
        )
        self.assertEqual(
            MODULE.build_search_params(MODULE.CollectorConfig(keyword="词", sort="collects"), 1)["sort_type"],
            "collect_descending",
        )
        self.assertEqual(
            MODULE.build_search_params(MODULE.CollectorConfig(keyword="词", time_filter="不限"), 1)[
                "time_filter"
            ],
            "不限",
        )


class NormalizeTests(unittest.TestCase):
    def test_counts_support_chinese_and_english_suffixes(self):
        self.assertEqual(MODULE.parse_count("1.2万"), 12000)
        self.assertEqual(MODULE.parse_count("2.5k"), 2500)
        self.assertEqual(MODULE.parse_count("1,234"), 1234)
        self.assertIsNone(MODULE.parse_count("未知"))

    def test_nested_metrics_and_millisecond_time_are_normalized(self):
        raw = {
            "id": NOTE_ID,
            "type": "video",
            "interact_info": {"liked_count": "1.2万", "collected_count": 300, "comments_count": 40},
            "time": int(NOW.timestamp() * 1000),
        }
        normalized = MODULE.normalize_note(raw)
        self.assertEqual(normalized.note_type, "video")
        self.assertEqual(normalized.likes, 12000)
        self.assertEqual(normalized.collects, 300)
        self.assertEqual(normalized.publish_time, NOW)

    def test_detail_fields_topics_author_and_link_are_normalized(self):
        raw = note()
        raw.update(
            {
                "title": "标题",
                "desc": "正文",
                "topics": [{"name": "AI"}],
                "hash_tag": [{"name": "AI"}, {"name": "公众号"}],
                "user": {"user_id": "user-1", "nickname": "作者"},
                "shared_count": 9,
            }
        )
        normalized = MODULE.normalize_note(raw)
        self.assertEqual(normalized.title, "标题")
        self.assertEqual(normalized.body, "正文")
        self.assertEqual(normalized.topics, ("AI", "公众号"))
        self.assertEqual(normalized.author_nickname, "作者")
        self.assertEqual(normalized.user_id, "user-1")
        self.assertEqual(normalized.shares, 9)
        self.assertEqual(normalized.source_url, f"https://www.xiaohongshu.com/explore/{NOTE_ID}")


class GateTests(unittest.TestCase):
    def test_all_active_metrics_must_reach_thresholds(self):
        config = MODULE.CollectorConfig(keyword="词", collect_count=5, min_likes=500, min_collects=300, min_comments=50)
        normalized = MODULE.normalize_note(note(likes=280, collects=1800, comments=40))
        self.assertEqual(MODULE.evaluate_search_note(normalized, config), MODULE.GateDecision.FAIL)

    def test_active_metric_missing_from_search_is_filtered(self):
        config = MODULE.CollectorConfig(keyword="词", collect_count=5, min_likes=500, min_collects=300, min_comments=50)
        normalized = MODULE.normalize_note(note(likes=500, collects=None, comments=None))
        self.assertEqual(MODULE.evaluate_search_note(normalized, config), MODULE.GateDecision.FAIL)

    def test_zero_and_none_metrics_do_not_participate(self):
        config = MODULE.CollectorConfig(
            keyword="词",
            collect_count=5,
            min_likes=0,
            min_collects=None,
            min_comments=50,
        )
        normalized = MODULE.normalize_note(note(likes=None, collects=None, comments=50))
        self.assertEqual(MODULE.evaluate_search_note(normalized, config), MODULE.GateDecision.PASS)

    def test_disabled_metrics_do_not_block_note(self):
        config = MODULE.CollectorConfig(
            keyword="词",
            collect_count=5,
            min_likes=None,
            min_collects=None,
            min_comments=None,
        )
        normalized = MODULE.normalize_note(note(likes=None, collects=None, comments=None))
        self.assertEqual(MODULE.evaluate_search_note(normalized, config), MODULE.GateDecision.PASS)

    def test_type_is_checked_but_time_is_left_to_search_api(self):
        config = MODULE.CollectorConfig(keyword="词", collect_count=5, note_type="video", time_filter="一周内")
        image = MODULE.normalize_note(note(note_type="normal"))
        old_video = MODULE.normalize_note(note(note_type="video", days_ago=8))
        self.assertEqual(MODULE.evaluate_search_note(image, config), MODULE.GateDecision.FAIL)
        self.assertEqual(MODULE.evaluate_search_note(old_video, config), MODULE.GateDecision.PASS)


class CollectionStateTests(unittest.TestCase):
    def test_duplicate_search_results_count_as_cards_without_reentering_detail(self):
        state = MODULE.CollectionState(MODULE.CollectorConfig(keyword="词", collect_count=2))
        self.assertIsNotNone(state.register_search(note(), now=NOW))
        decision, _ = state.register_search(note(), now=NOW)
        self.assertEqual(decision, MODULE.GateDecision.FAIL)
        self.assertEqual(state.scanned_count, 2)
        self.assertEqual(len(state.scanned_ids), 1)
        self.assertTrue(state.should_stop)

    def test_search_card_without_note_id_still_counts_toward_collect_count(self):
        state = MODULE.CollectionState(MODULE.CollectorConfig(keyword="词", collect_count=1))
        decision, _ = state.register_search({"title": "缺少 ID"}, now=NOW)
        self.assertEqual(decision, MODULE.GateDecision.FAIL)
        self.assertEqual(state.scanned_count, 1)
        self.assertTrue(state.should_stop)

    def test_only_scanned_card_count_stops_collection(self):
        state = MODULE.CollectionState(MODULE.CollectorConfig(keyword="词", collect_count=2))
        state.register_search(note(), now=NOW)
        state.register_detail(note(likes=0, collects=0, comments=0), now=NOW)
        self.assertFalse(state.should_stop)

        state.register_search(note(OTHER_NOTE_ID), now=NOW)
        self.assertTrue(state.should_stop)
        self.assertEqual(state.stop_reason, "COLLECT_COUNT_REACHED")

    def test_detail_enrichment_does_not_reapply_search_metrics(self):
        state = MODULE.CollectionState(
            MODULE.CollectorConfig(
                keyword="词",
                collect_count=2,
                min_likes=500,
                min_collects=300,
                min_comments=50,
            )
        )
        state.register_search(note(likes=500, collects=300, comments=50), now=NOW)
        decision, _ = state.register_detail(note(likes=0, collects=0, comments=0), now=NOW)
        self.assertEqual(decision, MODULE.GateDecision.PASS)

    def test_invalid_detail_id_cannot_enter_qualified_results(self):
        state = MODULE.CollectionState(MODULE.CollectorConfig(keyword="词", collect_count=1))
        decision, _ = state.register_detail(note("invalid-note-id"), now=NOW)
        self.assertEqual(decision, MODULE.GateDecision.FAIL)
        self.assertEqual(state.qualified_ids, set())


class CommentTests(unittest.TestCase):
    def test_comments_obey_count_page_and_dedup_limits(self):
        pages = []
        for page_number in range(1, 5):
            pages.append(
                {
                    "data": {
                        "comments": [
                            {
                                "id": f"comment-{page_number}",
                                "content": f"一级评论{page_number}",
                                "sub_comments": [{"id": "child", "content": "二级回复"}],
                            },
                            {"id": "duplicate", "content": "重复评论"},
                        ]
                    }
                }
            )
        comments = MODULE.merge_comment_pages(pages, comments_per_note=10)
        self.assertEqual([item["id"] for item in comments], ["comment-1", "duplicate", "comment-2", "comment-3"])
        self.assertNotIn("comment-4", [item["id"] for item in comments])
        self.assertNotIn("child", [item["id"] for item in comments])

    def test_comment_count_zero_avoids_consuming_pages(self):
        consumed = []

        def pages():
            consumed.append(True)
            yield {"comments": [{"id": "one"}]}

        self.assertEqual(MODULE.merge_comment_pages(pages(), 0), [])
        self.assertEqual(consumed, [])


def make_local_records():
    image_raw = note()
    image_raw.update(
        {
            "title": "图文标题",
            "desc": "图文正文",
            "topics": [{"name": "AI"}],
            "shared_count": 8,
            "images_list": [
                {"original": "https://example.com/1.webp"},
                {"original": "https://example.com/2.webp"},
            ],
        }
    )
    video_raw = note(OTHER_NOTE_ID, note_type="video")
    video_raw.update({"title": "视频标题", "desc": "视频正文"})
    search_video = {
        "video_info_v2": {"media": {"stream": {"h264": [{"master_url": "https://example.com/video.mp4"}]}}}
    }
    comments = (
        {
            "id": "comment-1",
            "content": "一级内容",
            "like_count": 3,
            "user": {"id": "comment-user", "nickname": "评论者"},
            "sub_comments": [{"content": "二级内容"}],
        },
    )
    return [
        MODULE.LocalNoteRecord(MODULE.normalize_note(image_raw), comments),
        MODULE.LocalNoteRecord(MODULE.normalize_note(video_raw), (), search_video),
    ]


def run_local_writer():
    downloads = []

    def fake_download(url, target):
        downloads.append((url, target))

    writer = MODULE.LocalResultWriter(
        MODULE.CollectorConfig.preset("AI公众号", "test"),
        output_root=Path("fixtures/测试输出"),
        downloader=fake_download,
    )
    with patch.object(MODULE.Path, "mkdir"), patch.object(MODULE, "write_markdown") as write:
        result = writer.write(make_local_records())
    return result, downloads, write


class LocalResultWriterTests(unittest.TestCase):
    def test_writer_creates_only_detail_markdown_document(self):
        result, _, write = run_local_writer()
        self.assertEqual(write.call_count, 1)
        detail = write.call_args_list[0].args[1]
        self.assertEqual(detail.count("\n## "), 2)
        self.assertIn("图文正文", detail)
        self.assertIn("分享：8", detail)
        self.assertIsNone(result.comment_path)

    def test_writer_uses_note_id_media_paths(self):
        result, downloads, _ = run_local_writer()
        self.assertEqual(
            [target for _, target in downloads],
            [
                result.run_dir / "03笔记素材" / NOTE_ID / "001.webp",
                result.run_dir / "03笔记素材" / NOTE_ID / "002.webp",
                result.run_dir / "03笔记素材" / OTHER_NOTE_ID / "video.mp4",
            ],
        )
        self.assertEqual(result.media_downloads, 3)
        self.assertEqual(result.run_dir.parts[-2:], ("测试输出", "AI公众号"))


if __name__ == "__main__":
    unittest.main()
