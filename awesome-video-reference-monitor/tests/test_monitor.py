from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock

from article_monitor.cli import build_parser
from article_monitor.filtering import FilterCondition, MonitorFilter
from article_monitor.monitor import (
    CHINA_TIMEZONE,
    ReferenceMonitor,
    build_case_index,
    material_filename_stem,
    meets_material_threshold,
    monitor_title,
    render_monitor_markdown,
)
from article_monitor.project import temporary_root
from article_monitor.tikhub import MonitorAccount, MonitorPost, PostPage

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass


NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=CHINA_TIMEZONE)


def make_post(
    video_id: str,
    hours_ago: float,
    *,
    platform: str = "douyin",
    media_url: str = "https://cdn/video.mp4",
    decode_key: str = "",
    is_video: bool = True,
) -> MonitorPost:
    """构造相对固定时钟发布的统一测试作品。"""

    return MonitorPost(
        platform=platform,
        video_id=video_id,
        object_nonce_id="nonce",
        nickname="作者",
        username="example",
        title=f"作品 {video_id}",
        create_time=int((NOW - timedelta(hours=hours_ago)).timestamp()),
        like_count=200,
        fav_count=2,
        forward_count=500,
        comment_count=4,
        media_url=media_url,
        decode_key=decode_key,
        is_video=is_video,
    )


class FakeClient:
    """按账号、渠道和游标返回预设页面。"""

    def __init__(self) -> None:
        """初始化账号、页面、调用记录和可选异常。"""

        self.account = MonitorAccount("douyin", "账号", "dy001", "sec001")
        self.pages: dict[tuple[str, str, str], PostPage | Exception] = {}
        self.calls: list[tuple[str, str, str]] = []
        self.detail_post: MonitorPost | None = None

    def fetch_account(self, _share_url: str) -> MonitorAccount:
        """返回固定登记账号。"""

        return self.account

    def fetch_posts(
        self,
        account: MonitorAccount,
        *,
        cursor: str = "",
        channel: str = "normal",
    ) -> PostPage:
        """读取预设页面，支持按账号模拟失败隔离。"""

        key = (account.api_user_id, channel, cursor)
        self.calls.append(key)
        value = self.pages.get(key, PostPage((), False, ""))
        if isinstance(value, Exception):
            raise value
        return value

    def fetch_douyin_post(
        self,
        _video_id: str,
        _account: MonitorAccount,
    ) -> MonitorPost:
        """返回抖音详情兜底作品。"""

        if self.detail_post is None:
            raise RuntimeError("详情缺失")
        return self.detail_post

    def fetch_post(self, _share_url: str) -> MonitorPost:
        """返回手动提取入口使用的单条详情作品。"""

        if self.detail_post is None:
            raise RuntimeError("详情缺失")
        return self.detail_post


class FakeCollector:
    """模拟共享采集核心并真实写入测试 Markdown。"""

    def __init__(self, transcript: str = "测试逐字稿") -> None:
        """保存模拟逐字稿与调用计数。"""

        self.transcript = transcript
        self.calls = 0
        self.sync_flags: list[bool] = []

    def __call__(
        self,
        _profile: dict[str, Any],
        source_url: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """调用监控渲染器并写入稳定文件名。"""

        self.calls += 1
        self.sync_flags.append(bool(kwargs.get("sync_to_feishu")))
        markdown_dir = Path(kwargs["markdown_dir"])
        path = markdown_dir / f"{kwargs['filename_stem']}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        feed = ((_profile.get("data") or {}).get("feedInfo") or {})
        case_id = f"{_profile.get('platform')}_{feed.get('objectId')}"
        markdown = kwargs["markdown_renderer"](
            profile=_profile,
            source_url=source_url,
            collected_at=NOW.strftime("%Y-%m-%d %H:%M:%S"),
            case_id=case_id,
            book_name="",
            transcript=self.transcript,
            segments=[],
            duration=10,
        )
        path.write_text(markdown, encoding="utf-8")
        return {"ok": True, "markdown": str(path), "case_id": case_id}


class FailingCollector:
    """模拟每次都在 Markdown 落盘前失败的采集核心。"""

    def __init__(self) -> None:
        """初始化调用次数。"""

        self.calls = 0

    def __call__(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        """记录调用并抛出可重试错误。"""

        self.calls += 1
        raise RuntimeError("ASR 失败")


class FakeAccountStore:
    """模拟本地账号清单的登记和只读账号列表。"""

    def __init__(self, accounts: list[MonitorAccount] | None = None) -> None:
        """保存待读取账号和登记调用。"""

        self.accounts = list(accounts or [])
        self.sync_calls: list[tuple[MonitorAccount, str, datetime]] = []

    def sync_account(
        self,
        account: MonitorAccount,
        share_url: str,
        recorded_at: datetime,
    ) -> dict[str, str]:
        """记录一次本地账号登记。"""

        self.sync_calls.append((account, share_url, recorded_at))
        return {"account_id": "douyin:sec001", "action": "create"}

    def list_monitor_accounts(self) -> list[MonitorAccount]:
        """返回本地清单中的有效账号。"""

        return list(self.accounts)


class MonitorWorkflowTests(unittest.TestCase):
    def test_custom_filter_replaces_default_metric_thresholds(self) -> None:
        """显式点赞条件应替换旧转发门槛，而不是叠加。"""

        post = replace(make_post("likes-only", 1), like_count=500, forward_count=1)
        rule = MonitorFilter(
            conditions=(FilterCondition("like_count", "gte", 500),)
        )

        self.assertTrue(rule.matches(post))

    def test_custom_filter_uses_all_logic_by_default(self) -> None:
        """多个显式条件默认必须全部满足。"""

        rule = MonitorFilter(
            conditions=(
                FilterCondition("like_count", "gte", 500),
                FilterCondition("forward_like_ratio", "gte", 1.5),
            )
        )
        exact = replace(
            make_post("all-exact", 1), like_count=500, forward_count=750
        )
        low_ratio = replace(exact, forward_count=749)

        self.assertTrue(rule.matches(exact))
        self.assertFalse(rule.matches(low_ratio))

    def test_custom_filter_supports_explicit_any_logic(self) -> None:
        """用户明确说“或”时任一条件满足即可。"""

        rule = MonitorFilter(
            conditions=(
                FilterCondition("like_count", "gte", 500),
                FilterCondition("comment_count", "gte", 100),
            ),
            logic="any",
        )
        post = replace(
            make_post("any", 1), like_count=499, comment_count=100, forward_count=0
        )

        self.assertTrue(rule.matches(post))

    def test_custom_ratio_rejects_zero_or_missing_like_count(self) -> None:
        """显式转赞比条件在点赞为零或缺失时必须不通过。"""

        rule = MonitorFilter(
            conditions=(FilterCondition("forward_like_ratio", "gte", 1.5),)
        )
        zero_like = replace(make_post("zero-ratio", 1), like_count=0)
        missing_like = replace(make_post("missing-ratio", 1), like_count=None)

        self.assertFalse(rule.matches(zero_like))
        self.assertFalse(rule.matches(missing_like))

    def test_missing_metric_does_not_fall_back_to_zero(self) -> None:
        """平台缺失的指标不得按数字零参与比较。"""

        rule = MonitorFilter(
            conditions=(FilterCondition("fav_count", "gte", 0),)
        )
        post = replace(make_post("missing-fav", 1), fav_count=None)

        self.assertFalse(rule.matches(post))

    def test_custom_window_is_used_for_scanning_and_filtering(self) -> None:
        """显式时间窗口应同时控制分页停止和作品过滤。"""

        client = FakeClient()
        post = make_post("seven-days", 100)
        client.pages[("sec001", "normal", "")] = PostPage((post,), False, "")
        collector = FakeCollector()
        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            summary = ReferenceMonitor(
                client,
                Path(temp_value) / "3-对标案例",
                now_provider=lambda: NOW,
                collect_func=collector,
                monitor_filter=MonitorFilter(window_hours=168),
            ).monitor_account(client.account)

        self.assertEqual(summary.added, 1)

    def test_material_threshold_requires_forward_count_and_ratio(self) -> None:
        """新素材必须同时达到最低转发数和转发点赞比。"""

        exact = replace(
            make_post("exact", 1),
            like_count=400,
            forward_count=600,
        )
        low_forward = replace(exact, like_count=100, forward_count=499)
        low_ratio = replace(exact, forward_count=599)

        self.assertTrue(meets_material_threshold(exact))
        self.assertFalse(meets_material_threshold(low_forward))
        self.assertFalse(meets_material_threshold(low_ratio))

    def test_material_threshold_accepts_zero_like_with_enough_forwards(self) -> None:
        """点赞为零时只要转发达到五百就视为通过。"""

        post = replace(make_post("zero-like", 1), like_count=0, forward_count=500)

        self.assertTrue(meets_material_threshold(post))

    def test_below_threshold_post_is_rechecked_until_it_qualifies(self) -> None:
        """未达标新作品暂不采集，窗口内后续达标时再提取。"""

        client = FakeClient()
        low = replace(make_post("growing", 1), like_count=100, forward_count=499)
        high = replace(low, like_count=200, forward_count=500)
        client.pages[("sec001", "normal", "")] = PostPage((low,), False, "")
        collector = FakeCollector()
        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            monitor = ReferenceMonitor(
                client,
                Path(temp_value) / "3-对标案例",
                now_provider=lambda: NOW,
                collect_func=collector,
            )
            first = monitor.monitor_account(client.account)
            client.pages[("sec001", "normal", "")] = PostPage((high,), False, "")
            second = monitor.monitor_account(client.account)

        self.assertEqual(first.skipped, 1)
        self.assertEqual(second.added, 1)
        self.assertEqual(collector.calls, 1)

    def test_new_material_disables_external_case_sync(self) -> None:
        """达标新素材只在本地落盘，不得启用飞书案例表同步。"""

        client = FakeClient()
        post = make_post("new-sync", 1)
        client.pages[("sec001", "normal", "")] = PostPage((post,), False, "")
        collector = FakeCollector()
        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            summary = ReferenceMonitor(
                client,
                Path(temp_value) / "3-对标案例",
                now_provider=lambda: NOW,
                collect_func=collector,
            ).monitor_account(client.account)

        self.assertEqual(summary.added, 1)
        self.assertEqual(collector.sync_flags, [False])

    def test_feishu_mode_enables_case_sync_for_new_material(self) -> None:
        """飞书模式采集达标新素材时必须同步案例表。"""

        client = FakeClient()
        post = make_post("new-feishu-sync", 1)
        client.pages[("sec001", "normal", "")] = PostPage((post,), False, "")
        collector = FakeCollector()
        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            summary = ReferenceMonitor(
                client,
                Path(temp_value) / "3-对标案例",
                now_provider=lambda: NOW,
                collect_func=collector,
                sync_to_feishu=True,
            ).monitor_account(client.account)

        self.assertEqual(summary.added, 1)
        self.assertEqual(collector.sync_flags, [True])

    def test_window_includes_exact_boundary_and_skips_old_and_future(self) -> None:
        """刚好 72 小时应采集，超窗和未来异常作品应跳过。"""

        client = FakeClient()
        client.pages[("sec001", "normal", "")] = PostPage(
            (
                make_post("old", 73),
                make_post("boundary", 72),
                make_post("future", -1),
                make_post("boundary", 72),
            ),
            False,
            "",
        )
        collector = FakeCollector()
        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            monitor = ReferenceMonitor(
                client,
                Path(temp_value) / "3-对标案例",
                now_provider=lambda: NOW,
                collect_func=collector,
            )
            summary = monitor.monitor_account(client.account)

        self.assertEqual(summary.added, 1)
        self.assertEqual(summary.skipped, 2)
        self.assertEqual(collector.calls, 1)

    def test_exact_boundary_ignores_current_time_microseconds(self) -> None:
        """平台秒级时间戳在当前时刻带微秒时仍应包含刚好 72 小时边界。"""

        client = FakeClient()
        boundary = make_post("boundary-microseconds", 72)
        client.pages[("sec001", "normal", "")] = PostPage((boundary,), False, "")
        collector = FakeCollector()
        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            summary = ReferenceMonitor(
                client,
                Path(temp_value) / "3-对标案例",
                now_provider=lambda: NOW.replace(microsecond=999999),
                collect_func=collector,
            ).monitor_account(client.account)

        self.assertEqual(summary.added, 1)

    def test_old_pinned_post_does_not_hide_newer_post_on_same_page(self) -> None:
        """首条旧置顶作品不得导致同页后续新作品漏采。"""

        client = FakeClient()
        client.pages[("sec001", "normal", "")] = PostPage(
            (make_post("pinned", 100), make_post("new", 1)),
            False,
            "",
        )
        collector = FakeCollector()
        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            summary = ReferenceMonitor(
                client,
                Path(temp_value) / "3-对标案例",
                now_provider=lambda: NOW,
                collect_func=collector,
            ).monitor_account(client.account)

        self.assertEqual(summary.added, 1)
        self.assertEqual(summary.skipped, 1)

    def test_disordered_old_page_reads_only_one_extra_page(self) -> None:
        """整页旧作品时间乱序时应额外读取一页寻找新作品。"""

        client = FakeClient()
        client.pages[("sec001", "normal", "")] = PostPage(
            (make_post("older", 100), make_post("less-old", 90)),
            True,
            "next",
        )
        client.pages[("sec001", "normal", "next")] = PostPage(
            (make_post("new", 2),),
            True,
            "third",
        )
        collector = FakeCollector()
        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            summary = ReferenceMonitor(
                client,
                Path(temp_value) / "3-对标案例",
                now_provider=lambda: NOW,
                collect_func=collector,
            ).monitor_account(client.account)

        self.assertEqual(summary.added, 1)
        self.assertEqual(len(client.calls), 2)

    def test_scan_never_exceeds_five_pages(self) -> None:
        """异常游标持续返回时每账号每渠道最多读取五页。"""

        client = FakeClient()
        for index in range(5):
            cursor = "" if index == 0 else f"cursor-{index}"
            next_cursor = f"cursor-{index + 1}"
            client.pages[("sec001", "normal", cursor)] = PostPage(
                (make_post(f"video-{index}", index + 1),),
                True,
                next_cursor,
            )
        collector = FakeCollector()
        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            summary = ReferenceMonitor(
                client,
                Path(temp_value) / "3-对标案例",
                now_provider=lambda: NOW,
                collect_func=collector,
            ).monitor_account(client.account)

        self.assertEqual(len(client.calls), 5)
        self.assertEqual(summary.added, 5)

    def test_douyin_uses_lite_when_normal_page_has_no_recent_posts(self) -> None:
        """普通版只有旧作品时应从第一页切换极速版查找最新作品。"""

        client = FakeClient()
        client.pages[("sec001", "normal", "")] = PostPage((make_post("old", 100),), False, "")
        client.pages[("sec001", "lite", "")] = PostPage((make_post("lite-new", 1),), False, "")
        collector = FakeCollector()
        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            summary = ReferenceMonitor(
                client,
                Path(temp_value) / "3-对标案例",
                now_provider=lambda: NOW,
                collect_func=collector,
            ).monitor_account(client.account)

        self.assertEqual(summary.added, 1)
        self.assertIn(("sec001", "lite", ""), client.calls)

    def test_existing_case_only_refreshes_metrics_without_asr(self) -> None:
        """已有案例只更新本地数据，不应再次调用采集核心。"""

        client = FakeClient()
        post = make_post("existing", 1)
        client.pages[("sec001", "normal", "")] = PostPage((post,), False, "")
        collector = FakeCollector()
        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            root = Path(temp_value)
            case_path = root / "3-对标案例" / "文案" / post.case_id
            case_path = case_path.with_suffix(".md")
            case_path.parent.mkdir(parents=True)
            case_path.write_text(
                "# 原文\n\n## 基础信息\n\n| 字段 | 内容 |\n|---|---|\n"
                f"| 案例 ID | {post.case_id} |\n| 点赞 | 99 |\n\n## 清洗逐字稿\n\n保留正文\n",
                encoding="utf-8",
            )
            summary = ReferenceMonitor(
                client,
                root / "3-对标案例",
                now_provider=lambda: NOW,
                collect_func=collector,
            ).monitor_account(client.account)
            updated = case_path.read_text(encoding="utf-8")

        self.assertEqual(summary.updated, 1)
        self.assertEqual(collector.calls, 0)
        self.assertIn("| 点赞 | 200 |", updated)
        self.assertIn("| 数据更新时间 | 2026-08-26 12:00:00 |", updated)
        self.assertIn("保留正文", updated)

    def test_feishu_failure_keeps_local_case_and_retries_next_round(self) -> None:
        """飞书失败不得删除本地案例，下轮应跳过 ASR 并再次同步。"""

        client = FakeClient()
        post = make_post("feishu-retry", 1)
        client.pages[("sec001", "normal", "")] = PostPage((post,), False, "")
        collector = FakeCollector()
        sync_case = Mock(
            side_effect=[RuntimeError("飞书暂时不可用"), {"action": "create"}]
        )
        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            root = Path(temp_value)
            case_path = root / "2-素材库" / "作者" / "已有案例.md"
            case_path.parent.mkdir(parents=True)
            case_path.write_text(
                "# 已有案例\n\n## 基础信息\n\n| 字段 | 内容 |\n|---|---|\n"
                f"| 案例 ID | {post.case_id} |\n"
                "| 发现时间 | 2026-08-26 11:00:00 |\n"
                "| 点赞 | 99 |\n\n## 清洗逐字稿\n\n保留正文\n",
                encoding="utf-8",
            )
            monitor = ReferenceMonitor(
                client,
                root / "3-对标案例",
                now_provider=lambda: NOW,
                collect_func=collector,
                sync_to_feishu=True,
                case_sync_func=sync_case,
            )

            first = monitor.monitor_account(client.account)
            second = monitor.monitor_account(client.account)

        self.assertEqual(first.failed, 1)
        self.assertEqual(first.updated, 0)
        self.assertEqual(second.updated, 1)
        self.assertEqual(collector.calls, 0)
        self.assertEqual(sync_case.call_count, 2)

    def test_empty_transcript_is_persisted_and_not_retried(self) -> None:
        """无有效口播应写入状态文件，下一轮只刷新数据。"""

        client = FakeClient()
        post = make_post("silent", 1)
        client.pages[("sec001", "normal", "")] = PostPage((post,), False, "")
        collector = FakeCollector(transcript="")
        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            root = Path(temp_value)
            monitor = ReferenceMonitor(
                client,
                root / "3-对标案例",
                now_provider=lambda: NOW,
                collect_func=collector,
            )
            first = monitor.monitor_account(client.account)
            second = monitor.monitor_account(client.account)
            markdown = (
                root / "2-素材库" / "作者" / f"{monitor_title(post)}.md"
            ).read_text(encoding="utf-8")

        self.assertEqual(first.added, 1)
        self.assertEqual(second.updated, 1)
        self.assertEqual(collector.calls, 1)
        self.assertIn("| 采集状态 | 无有效口播 |", markdown)

    def test_failed_collection_is_retried_and_account_failure_is_isolated(self) -> None:
        """采集失败不落成功标记，且整轮监控继续处理后续账号。"""

        client = FakeClient()
        bad = MonitorAccount("douyin", "坏账号", "bad", "bad")
        good = MonitorAccount("douyin", "好账号", "good", "good")
        client.pages[("bad", "normal", "")] = RuntimeError("接口失败")
        client.pages[("good", "normal", "")] = PostPage((make_post("good-video", 1),), False, "")
        collector = FakeCollector()
        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            root = Path(temp_value)
            result = ReferenceMonitor(
                client,
                root / "3-对标案例",
                now_provider=lambda: NOW,
                collect_func=collector,
                account_store=FakeAccountStore([bad, good]),
            ).monitor_all()

        self.assertFalse(result["ok"])
        self.assertEqual(result["summary"]["failed"], 1)
        self.assertEqual(result["summary"]["added"], 1)

    def test_failed_collection_without_markdown_is_retried_next_round(self) -> None:
        """ASR 失败不得创建案例索引，下一轮应再次调用采集核心。"""

        client = FakeClient()
        post = make_post("retry", 1)
        client.pages[("sec001", "normal", "")] = PostPage((post,), False, "")
        collector = FailingCollector()
        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            root = Path(temp_value)
            monitor = ReferenceMonitor(
                client,
                root / "3-对标案例",
                now_provider=lambda: NOW,
                collect_func=collector,
            )
            first = monitor.monitor_account(client.account)
            second = monitor.monitor_account(client.account)
            markdown_path = root / "2-素材库" / "作者" / f"{monitor_title(post)}.md"

        self.assertEqual(first.failed, 1)
        self.assertEqual(second.failed, 1)
        self.assertEqual(collector.calls, 2)
        self.assertFalse(markdown_path.exists())

    def test_missing_douyin_media_refetches_detail_once(self) -> None:
        """抖音视频结构存在但列表地址缺失时只补查一次详情。"""

        client = FakeClient()
        missing = make_post("detail", 1, media_url="", is_video=True)
        client.detail_post = make_post("detail", 1, media_url="https://cdn/detail.mp4")
        client.pages[("sec001", "normal", "")] = PostPage((missing,), False, "")
        collector = FakeCollector()
        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            summary = ReferenceMonitor(
                client,
                Path(temp_value) / "3-对标案例",
                now_provider=lambda: NOW,
                collect_func=collector,
            ).monitor_account(client.account)

        self.assertEqual(summary.added, 1)
        self.assertEqual(collector.calls, 1)

    def test_wechat_missing_decode_key_fails_without_collecting(self) -> None:
        """视频号媒体缺同次密钥时应失败并等待下轮重试。"""

        account = MonitorAccount(
            "wechat_channels",
            "视频号",
            "v2_test@finder",
            "v2_test@finder",
        )
        client = FakeClient()
        post = make_post(
            "wechat-no-key",
            1,
            platform="wechat_channels",
            media_url="https://finder/encrypted.mp4",
            decode_key="",
            is_video=True,
        )
        client.pages[(account.api_user_id, "normal", "")] = PostPage((post,), False, "")
        collector = FakeCollector()
        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            summary = ReferenceMonitor(
                client,
                Path(temp_value) / "3-对标案例",
                now_provider=lambda: NOW,
                collect_func=collector,
            ).monitor_account(account)

        self.assertEqual(summary.failed, 1)
        self.assertEqual(collector.calls, 0)

    def test_record_account_only_writes_account_without_scanning(self) -> None:
        """登记账号只写本地账号信息，不得读取作品列表或调用采集。"""

        client = FakeClient()
        client.pages[("sec001", "normal", "")] = PostPage((make_post("new", 1),), False, "")
        collector = FakeCollector()
        account_store = FakeAccountStore()
        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            result = ReferenceMonitor(
                client,
                Path(temp_value) / "3-对标案例",
                now_provider=lambda: NOW,
                collect_func=collector,
                account_store=account_store,
            ).record_account("https://v.douyin.com/test/")

        self.assertTrue(result["ok"])
        self.assertEqual(result["account_action"], "create")
        self.assertEqual(result["account_id"], "douyin:sec001")
        self.assertEqual(len(account_store.sync_calls), 1)
        self.assertEqual(client.calls, [])
        self.assertEqual(collector.calls, 0)

    def test_manual_extract_collects_single_post_into_case_directory(self) -> None:
        """手动提取应绕过账号清单和监控筛选，直接保存正式案例。"""

        client = FakeClient()
        client.detail_post = make_post("manual", 1000)
        collector = Mock(
            return_value={
                "ok": True,
                "markdown": "手动案例.md",
                "case_id": "douyin_manual",
            }
        )
        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            output_dir = Path(temp_value) / "3-对标案例"
            result = ReferenceMonitor(
                client,
                output_dir,
                collect_func=collector,
            ).extract_post("https://v.douyin.com/manual/")

        self.assertEqual(result["action"], "create")
        self.assertEqual(result["case_id"], "douyin_manual")
        collector.assert_called_once()
        _, source_url = collector.call_args.args
        self.assertEqual(source_url, "https://v.douyin.com/manual/")
        self.assertEqual(
            collector.call_args.kwargs["markdown_dir"],
            output_dir / "文案",
        )
        self.assertFalse(collector.call_args.kwargs["sync_to_feishu"])

    def test_manual_extract_reuses_existing_case_without_asr(self) -> None:
        """素材库或案例库已有相同作品时不得重复下载和调用 ASR。"""

        client = FakeClient()
        post = make_post("existing", 1000)
        client.detail_post = post
        collector = Mock()
        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            root = Path(temp_value)
            existing_path = root / "2-素材库" / "作者" / "已有素材.md"
            existing_path.parent.mkdir(parents=True)
            existing_path.write_text(
                f"| 案例 ID | {post.case_id} |\n",
                encoding="utf-8",
            )
            result = ReferenceMonitor(
                client,
                root / "3-对标案例",
                collect_func=collector,
            ).extract_post("https://v.douyin.com/existing/")

        self.assertEqual(result["action"], "existing")
        self.assertEqual(Path(result["markdown"]), existing_path.resolve())
        collector.assert_not_called()

    def test_manual_extract_respects_feishu_storage_mode(self) -> None:
        """飞书模式下的新手动案例应显式启用案例表同步。"""

        client = FakeClient()
        client.detail_post = make_post("manual", 1000)
        collector = Mock(
            return_value={
                "ok": True,
                "markdown": "手动案例.md",
                "case_id": "douyin_manual",
            }
        )
        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            result = ReferenceMonitor(
                client,
                Path(temp_value) / "3-对标案例",
                collect_func=collector,
                sync_to_feishu=True,
            ).extract_post("https://v.douyin.com/manual/")

        self.assertEqual(result["action"], "create")
        self.assertTrue(collector.call_args.kwargs["sync_to_feishu"])

    def test_manual_extract_rejects_non_video_post(self) -> None:
        """手动提取遇到图文等非视频作品时应在媒体处理前停止。"""

        client = FakeClient()
        client.detail_post = make_post("note", 1, is_video=False, media_url="")
        collector = Mock()
        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            monitor = ReferenceMonitor(
                client,
                Path(temp_value) / "3-对标案例",
                collect_func=collector,
            )
            with self.assertRaisesRegex(RuntimeError, "只支持视频作品"):
                monitor.extract_post("https://www.douyin.com/note/123")

        collector.assert_not_called()

    def test_monitor_all_reads_accounts_from_local_store(self) -> None:
        """监控命令只能从本地账号清单读取账号。"""

        client = FakeClient()
        client.pages[("sec001", "normal", "")] = PostPage((), False, "")
        account_store = FakeAccountStore([client.account])
        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            result = ReferenceMonitor(
                client,
                Path(temp_value) / "3-对标案例",
                now_provider=lambda: NOW,
                account_store=account_store,
            ).monitor_all()

        self.assertTrue(result["ok"])
        self.assertEqual(result["accounts"], 1)
        self.assertTrue(result["filter"]["uses_default_metrics"])
        self.assertEqual(result["filter"]["window_hours"], 72)
        self.assertEqual(client.calls, [("sec001", "normal", "")])

    def test_wechat_markdown_never_contains_read_count(self) -> None:
        """监控 Markdown 只能保留四项筛选数据，不得出现播放或阅读数。"""

        post = make_post(
            "14941130915890399732",
            1,
            platform="wechat_channels",
            media_url="https://finder/video",
            decode_key="key",
        )
        markdown = render_monitor_markdown(
            post=post,
            collected_at="2026-08-26 12:00:00",
            case_id=post.case_id,
            transcript="测试口播",
            segments=[],
            duration=12,
        )
        self.assertNotIn("播放", markdown)
        self.assertNotIn("阅读", markdown)
        self.assertIn("| 视频 ID | 14941130915890399732 |", markdown)

    def test_new_material_uses_account_folder_and_twenty_character_title(self) -> None:
        """新素材应进入账号中文名目录，标题与基础文件名最多二十字。"""

        client = FakeClient()
        long_title = "这是一个刚好用于验证标题最大长度二十个字符的完整描述"
        post = make_post("long-title", 1)
        post = replace(post, title=long_title)
        client.pages[("sec001", "normal", "")] = PostPage((post,), False, "")
        collector = FakeCollector()
        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            root = Path(temp_value)
            summary = ReferenceMonitor(
                client,
                root / "3-对标案例",
                now_provider=lambda: NOW,
                collect_func=collector,
            ).monitor_account(client.account)
            files = list((root / "2-素材库" / "作者").glob("*.md"))
            text = files[0].read_text(encoding="utf-8")

        self.assertEqual(summary.added, 1)
        self.assertEqual(len(files), 1)
        self.assertLessEqual(len(files[0].stem), 20)
        self.assertEqual(text.splitlines()[0], f"# {long_title[:20]}")
        self.assertIn(f"| 标题/描述 | {long_title} |", text)

    def test_duplicate_twenty_character_title_adds_video_id_suffix(self) -> None:
        """不同案例前二十字重名时应追加作品 ID 后六位，禁止覆盖。"""

        title = "完全相同的二十字标题用于重名冲突测试内容"
        first = make_post("111111", 1)
        second = make_post("222222", 1)
        first = replace(first, title=title)
        second = replace(second, title=title)
        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            material_dir = Path(temp_value)
            first_path = material_dir / f"{material_filename_stem(material_dir, first)}.md"
            first_path.write_text(
                f"| 案例 ID | {first.case_id} |\n",
                encoding="utf-8",
            )
            second_stem = material_filename_stem(material_dir, second)

        self.assertEqual(second_stem, f"{title[:20]}_222222")

    def test_cli_exposes_record_monitor_and_extract_commands(self) -> None:
        """命令行应暴露账号登记、全账号监控和手动提取入口。"""

        parser = build_parser()
        self.assertEqual(parser.parse_args(["record", "https://v.douyin.com/a/"]).command, "record")
        self.assertEqual(parser.parse_args(["monitor"]).command, "monitor")
        self.assertEqual(
            parser.parse_args(["extract", "https://v.douyin.com/a/"]).command,
            "extract",
        )
        self.assertFalse(hasattr(parser.parse_args(["monitor", "--json"]), "output_dir"))

    def test_case_index_reads_stable_names_and_material_metadata(self) -> None:
        """案例索引应同时识别稳定文件名和素材文档内的案例 ID。"""

        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            root = Path(temp_value)
            pending = root / "文案"
            materials = root / "素材"
            pending.mkdir()
            materials.mkdir()
            (pending / "douyin_123.md").write_text("# 待改写", encoding="utf-8")
            (materials / "已有素材.md").write_text(
                "| 案例 ID | wechat_channels_456 |\n", encoding="utf-8"
            )
            index = build_case_index(pending, materials)

        self.assertEqual(index["douyin_123"].name, "douyin_123.md")
        self.assertEqual(index["wechat_channels_456"].name, "已有素材.md")


if __name__ == "__main__":
    unittest.main()
