from __future__ import annotations

import sys
import unittest

from article_monitor.media import choose_video_url, platform_referer
from article_monitor.project import environment_root
from article_monitor.transcription import AsrConfig, dotenv_candidates, parse_bool

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass


class TranscriptionMediaTests(unittest.TestCase):
    """验证媒体选择和独立 ASR 配置边界。"""

    def test_asr_only_searches_declared_environment_dotenv(self) -> None:
        """ASR 只读取独立 Skill 或明确声明的工作区配置。"""

        self.assertEqual(dotenv_candidates(), [environment_root() / ".env"])

    def test_asr_config_requires_api_key_and_workspace(self) -> None:
        """缺少 ASR 密钥或工作空间时应明确失败。"""

        with self.assertRaisesRegex(RuntimeError, "DASHSCOPE_API_KEY"):
            AsrConfig().validate()
        with self.assertRaisesRegex(RuntimeError, "WorkspaceId"):
            AsrConfig(
                api_key="token",
                base_url="https://{WorkspaceId}.example/v1",
            ).validate()

    def test_parse_bool_preserves_safe_default(self) -> None:
        """无法识别的布尔配置应使用显式默认值。"""

        self.assertTrue(parse_bool("yes"))
        self.assertFalse(parse_bool("no", default=True))
        self.assertTrue(parse_bool("未知", default=True))

    def test_selects_normalized_video_url_and_platform_referer(self) -> None:
        """监控归一化详情应提供媒体地址和正确来源页。"""

        profile = {
            "platform": "wechat_channels",
            "data": {"feedInfo": {"videoUrl": "https://media.example/video.mp4"}},
        }
        self.assertEqual(
            choose_video_url(profile),
            "https://media.example/video.mp4",
        )
        self.assertEqual(
            platform_referer("wechat_channels"),
            "https://channels.weixin.qq.com/",
        )


if __name__ == "__main__":
    unittest.main()
