from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

from article_monitor.project import temporary_root
from article_monitor.workflow import collect_profile

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass


class FakeTranscription:
    """提供采集流程所需的最小 ASR 结果。"""

    text = "独立项目测试逐字稿"
    segments: ClassVar[list[dict[str, object]]] = [
        {"start": 0.0, "end": 1.0, "text": "独立项目测试逐字稿"}
    ]
    duration = 1.0


def sample_profile() -> dict:
    """构造已经由 TikHub 归一化的抖音作品。"""

    return {
        "platform": "douyin",
        "sourceUrl": "https://www.douyin.com/video/123",
        "data": {
            "authorInfo": {"nickname": "测试账号", "username": "tester"},
            "feedInfo": {
                "awemeId": "123",
                "title": "测试标题",
                "description": "测试标题",
                "videoUrl": "https://media.example/video.mp4",
                "likeCount": 10,
                "favCount": 20,
                "commentCount": 30,
                "forwardCount": 600,
            },
        },
    }


class WorkflowTests(unittest.TestCase):
    """验证监控采集核心的本地落盘和清理契约。"""

    def test_collect_profile_writes_local_markdown_without_external_sync(self) -> None:
        """成功采集应保存 Markdown、不调用飞书并清理临时目录。"""

        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            output_root = Path(temp_value) / "3-对标案例"
            material_dir = Path(temp_value) / "2-素材库" / "测试账号"
            work_dir = Path(temp_value) / "工作目录"
            work_dir.mkdir()
            video_path = work_dir / "video.mp4"
            video_path.write_bytes(b"video")
            with (
                patch("article_monitor.workflow.tempfile.mkdtemp", return_value=str(work_dir)),
                patch(
                    "article_monitor.workflow.prepare_transcription_video",
                    return_value=video_path,
                ),
                patch("article_monitor.workflow.extract_audio"),
                patch(
                    "article_monitor.workflow.transcribe_audio",
                    return_value=FakeTranscription(),
                ),
                patch(
                    "article_monitor.workflow.render_markdown",
                    return_value="# 独立项目测试\n",
                ),
                patch(
                    "article_monitor.workflow.sync_collected_case",
                    return_value={"record_id": "rec1", "action": "create"},
                ) as sync_mock,
            ):
                result = collect_profile(
                    sample_profile(),
                    "https://www.douyin.com/video/123",
                    output_dir=output_root,
                    markdown_dir=material_dir,
                    filename_stem="测试标题",
                )
            markdown_path = Path(result["markdown"])
            self.assertEqual(markdown_path.read_text(encoding="utf-8"), "# 独立项目测试\n")
            self.assertEqual(result["feishu_record_id"], "")
            self.assertEqual(result["feishu_action"], "skipped")
            sync_mock.assert_not_called()
            self.assertFalse(work_dir.exists())

    def test_failed_asr_leaves_no_markdown_or_temporary_media(self) -> None:
        """ASR 失败不得生成成功文档，项目内临时媒体必须清理。"""

        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            output_root = Path(temp_value) / "3-对标案例"
            material_dir = Path(temp_value) / "2-素材库" / "测试账号"
            work_dir = Path(temp_value) / "失败工作目录"
            work_dir.mkdir()
            video_path = work_dir / "video.mp4"
            video_path.write_bytes(b"video")
            with (
                patch("article_monitor.workflow.tempfile.mkdtemp", return_value=str(work_dir)),
                patch(
                    "article_monitor.workflow.prepare_transcription_video",
                    return_value=video_path,
                ),
                patch("article_monitor.workflow.extract_audio"),
                patch(
                    "article_monitor.workflow.transcribe_audio",
                    side_effect=RuntimeError("ASR 测试失败"),
                ),
                self.assertRaisesRegex(RuntimeError, "ASR 测试失败"),
            ):
                collect_profile(
                    sample_profile(),
                    "https://www.douyin.com/video/123",
                    output_dir=output_root,
                    markdown_dir=material_dir,
                    filename_stem="测试标题",
                )
            self.assertFalse((material_dir / "测试标题.md").exists())
            self.assertFalse(work_dir.exists())

    def test_feishu_failure_keeps_completed_local_markdown(self) -> None:
        """飞书同步失败时必须保留已经完成的本地素材，供下轮重试。"""

        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            output_root = Path(temp_value) / "3-对标案例"
            material_dir = Path(temp_value) / "2-素材库" / "测试账号"
            work_dir = Path(temp_value) / "飞书失败工作目录"
            work_dir.mkdir()
            video_path = work_dir / "video.mp4"
            video_path.write_bytes(b"video")
            with (
                patch("article_monitor.workflow.tempfile.mkdtemp", return_value=str(work_dir)),
                patch(
                    "article_monitor.workflow.prepare_transcription_video",
                    return_value=video_path,
                ),
                patch("article_monitor.workflow.extract_audio"),
                patch(
                    "article_monitor.workflow.transcribe_audio",
                    return_value=FakeTranscription(),
                ),
                patch(
                    "article_monitor.workflow.render_markdown",
                    return_value="# 已完成的本地素材\n",
                ),
                patch(
                    "article_monitor.workflow.sync_collected_case",
                    side_effect=RuntimeError("飞书暂时不可用"),
                ),
                self.assertRaisesRegex(RuntimeError, "飞书暂时不可用"),
            ):
                collect_profile(
                    sample_profile(),
                    "https://www.douyin.com/video/123",
                    output_dir=output_root,
                    markdown_dir=material_dir,
                    filename_stem="测试标题",
                    sync_to_feishu=True,
                )

            markdown_path = material_dir / "测试标题.md"
            self.assertEqual(
                markdown_path.read_text(encoding="utf-8"),
                "# 已完成的本地素材\n",
            )
            self.assertFalse(work_dir.exists())


if __name__ == "__main__":
    unittest.main()
