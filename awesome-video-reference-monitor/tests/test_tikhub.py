from __future__ import annotations

import json
import os
import sys
import unittest
from typing import Self
from unittest.mock import patch

from article_monitor.config import MonitorConfig, load_config, require_tikhub_config
from article_monitor.platform import detect_platform
from article_monitor.tikhub import (
    TikHubClient,
    normalize_douyin_account,
    normalize_douyin_page,
    normalize_wechat_account,
    normalize_wechat_page,
)

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass


class FakeResponse:
    """提供 urllib 上下文管理器所需的最小响应对象。"""

    def __init__(self, payload: dict) -> None:
        """把响应对象预先编码为 UTF-8 JSON。"""

        self.body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self) -> Self:
        """返回当前模拟响应。"""

        return self

    def __exit__(self, *_args: object) -> None:
        """退出上下文时不抑制异常。"""

    def read(self) -> bytes:
        """返回已编码的 JSON 响应体。"""

        return self.body


class TikHubMonitorTests(unittest.TestCase):
    def test_detects_supported_share_link_platforms(self) -> None:
        """记录对标入口应识别抖音和视频号分享链接。"""

        self.assertEqual(detect_platform("https://v.douyin.com/abc/"), "douyin")
        self.assertEqual(
            detect_platform("https://weixin.qq.com/sph/example"),
            "wechat_channels",
        )
        self.assertEqual(detect_platform("https://example.com/video"), "unknown")

    def test_loads_tikhub_config_without_leaking_into_getoneapi(self) -> None:
        """TikHub 监控配置应独立于现有 GetOneAPI 手动采集配置。"""

        with patch.dict(
            os.environ,
            {"TIKHUB_API_KEY": "secret", "TIKHUB_BASE_URL": "https://api.example/"},
            clear=False,
        ):
            config = load_config()
        self.assertEqual(config.tikhub_api_key, "secret")
        self.assertEqual(config.tikhub_base_url, "https://api.example")
        require_tikhub_config(config)
        with self.assertRaisesRegex(RuntimeError, "TIKHUB_API_KEY"):
            require_tikhub_config(MonitorConfig())

    def test_normalizes_douyin_account_and_h264_post(self) -> None:
        """抖音昵称、展示号、sec_user_id 和 H.264 地址应正确映射。"""

        account = normalize_douyin_account(
            {
                "aweme_detail": {
                    "author": {
                        "nickname": "示例抖音",
                        "unique_id": "example123",
                        "sec_uid": "MS4w.test",
                    }
                }
            }
        )
        page = normalize_douyin_page(
            {
                "aweme_list": [
                    {
                        "aweme_id": "7399999999999999999",
                        "create_time": 1_700_000_000,
                        "desc": "测试作品",
                        "statistics": {
                            "digg_count": 0,
                            "like_count": 99,
                            "collect_count": 3,
                            "share_count": 2,
                            "comment_count": 1,
                            "play_count": 99999,
                        },
                        "video": {
                            "play_addr_h264": {"url_list": ["https://cdn/h264.mp4"]},
                            "play_addr": {"url_list": ["https://cdn/default.mp4"]},
                        },
                    }
                ],
                "has_more": 1,
                "max_cursor": 123,
            },
            account,
        )
        post = page.posts[0]
        self.assertEqual(account.username, "example123")
        self.assertEqual(account.api_user_id, "MS4w.test")
        self.assertEqual(post.video_id, "7399999999999999999")
        self.assertEqual(post.media_url, "https://cdn/h264.mp4")
        self.assertEqual(post.like_count, 0)
        self.assertEqual(post.fav_count, 3)
        self.assertNotIn("playCount", post.to_profile()["data"]["feedInfo"])
        self.assertTrue(page.has_more)
        self.assertEqual(page.cursor, "123")

    def test_keeps_wechat_large_ids_and_media_key_pair(self) -> None:
        """视频号 64 位 ID、媒体地址和同次 decode_key 必须原样保留。"""

        account = normalize_wechat_account(
            {"nickname": "示例视频号", "username": "v2_example@finder"}
        )
        page = normalize_wechat_page(
            {
                "videos": [
                    {
                        "id": "14941130915890399732",
                        "object_nonce_id": "14941130915890399733",
                        "title": "视频号作品",
                        "create_time": 1_700_000_000,
                        "like_count": 20,
                        "fav_count": 4,
                        "forward_count": 5,
                        "comment_count": 6,
                        "read_count": 100000,
                        "media": {
                            "full_url": "https://finder/encrypted.mp4?token=one",
                            "decode_key": "987654321",
                        },
                    }
                ],
                "up_continue": True,
                "last_buffer": "base64+//=",
            },
            account,
        )
        post = page.posts[0]
        self.assertEqual(post.video_id, "14941130915890399732")
        self.assertEqual(post.object_nonce_id, "14941130915890399733")
        self.assertEqual(post.decode_key, "987654321")
        self.assertEqual(
            post.to_profile()["data"]["feedInfo"]["videoUrl"],
            "https://finder/encrypted.mp4?token=one",
        )
        self.assertNotIn("readCount", post.to_profile()["data"]["feedInfo"])
        self.assertEqual(page.cursor, "base64+//=")

    def test_wechat_media_without_decode_key_is_retryable_video_failure(self) -> None:
        """有媒体但缺密钥的列表项仍是视频，应由监控层失败并在下轮重试。"""

        account = normalize_wechat_account(
            {"nickname": "示例视频号", "username": "v2_example@finder"}
        )
        page = normalize_wechat_page(
            {
                "videos": [
                    {
                        "id": "123",
                        "media": {"full_url": "https://finder/encrypted.mp4"},
                    }
                ]
            },
            account,
        )
        self.assertTrue(page.posts[0].is_video)
        self.assertEqual(page.posts[0].decode_key, "")

    def test_wechat_title_extracts_short_title_from_list_object(self) -> None:
        """视频号列表标题为字典列表时应只提取 shortTitle 文本。"""

        account = normalize_wechat_account(
            {"nickname": "示例视频号", "username": "v2_example@finder"}
        )
        page = normalize_wechat_page(
            {
                "videos": [
                    {
                        "id": "123",
                        "title": [
                            {
                                "shortTitle": "恭喜你，你的儿子正在慢慢变好。",
                                "pbRequestMsgInfo": None,
                            }
                        ],
                        "media": {
                            "full_url": "https://finder/encrypted.mp4",
                            "decode_key": "key",
                        },
                    }
                ]
            },
            account,
        )

        self.assertEqual(page.posts[0].title, "恭喜你，你的儿子正在慢慢变好。")

    def test_client_uses_get_query_and_post_json_contracts(self) -> None:
        """双平台请求应分别使用官方定义的 GET 查询和 POST JSON。"""

        client = TikHubClient(
            MonitorConfig(tikhub_api_key="token", tikhub_base_url="https://api.tikhub.dev")
        )
        douyin_payload = {
            "code": 200,
            "data": {
                "aweme_detail": {
                    "author": {"nickname": "抖音", "unique_id": "dy", "sec_uid": "sec"}
                }
            },
        }
        wechat_payload = {
            "code": 200,
            "data": {"nickname": "视频号", "username": "v2_test@finder"},
        }
        with patch(
            "article_monitor.tikhub.urllib.request.urlopen",
            side_effect=[FakeResponse(douyin_payload), FakeResponse(wechat_payload)],
        ) as urlopen:
            client.fetch_account("https://v.douyin.com/abc/")
            client.fetch_account("https://weixin.qq.com/sph/abc")

        douyin_request = urlopen.call_args_list[0].args[0]
        wechat_request = urlopen.call_args_list[1].args[0]
        self.assertEqual(douyin_request.method, "GET")
        self.assertIn("share_url=https%3A%2F%2Fv.douyin.com%2Fabc%2F", douyin_request.full_url)
        self.assertEqual(wechat_request.method, "POST")
        self.assertEqual(
            json.loads(wechat_request.data.decode("utf-8")),
            {"share_url": "https://weixin.qq.com/sph/abc", "raw": False},
        )
        self.assertEqual(wechat_request.headers["Authorization"], "Bearer token")

    def test_client_refetches_douyin_detail_when_list_media_is_missing(self) -> None:
        """抖音列表缺少播放地址时应能按作品页补查详情。"""

        client = TikHubClient(
            MonitorConfig(tikhub_api_key="token", tikhub_base_url="https://api.tikhub.dev")
        )
        account = normalize_douyin_account(
            {
                "aweme_detail": {
                    "author": {"nickname": "抖音", "unique_id": "dy", "sec_uid": "sec"}
                }
            }
        )
        payload = {
            "code": 200,
            "data": {
                "aweme_detail": {
                    "aweme_id": "123",
                    "video": {
                        "play_addr_h264": {"url_list": ["https://cdn/detail.mp4"]}
                    },
                }
            },
        }
        with patch(
            "article_monitor.tikhub.urllib.request.urlopen",
            return_value=FakeResponse(payload),
        ) as urlopen:
            post = client.fetch_douyin_post("123", account)

        self.assertEqual(post.media_url, "https://cdn/detail.mp4")
        self.assertTrue(post.is_video)
        self.assertIn(
            "share_url=https%3A%2F%2Fwww.douyin.com%2Fvideo%2F123",
            urlopen.call_args.args[0].full_url,
        )

    def test_client_fetches_douyin_post_directly_from_share_link(self) -> None:
        """手动提取应直接把抖音详情归一化为单条作品。"""

        client = TikHubClient(
            MonitorConfig(tikhub_api_key="token", tikhub_base_url="https://api.tikhub.dev")
        )
        payload = {
            "code": 200,
            "data": {
                "aweme_detail": {
                    "aweme_id": "123",
                    "desc": "手动提取测试",
                    "author": {"nickname": "抖音作者", "unique_id": "dy"},
                    "statistics": {"digg_count": 10},
                    "video": {
                        "play_addr_h264": {"url_list": ["https://cdn/manual.mp4"]}
                    },
                }
            },
        }
        with patch(
            "article_monitor.tikhub.urllib.request.urlopen",
            return_value=FakeResponse(payload),
        ) as urlopen:
            post = client.fetch_post("https://v.douyin.com/manual/")

        self.assertEqual(post.case_id, "douyin_123")
        self.assertEqual(post.title, "手动提取测试")
        self.assertEqual(post.nickname, "抖音作者")
        self.assertEqual(post.media_url, "https://cdn/manual.mp4")
        self.assertEqual(urlopen.call_args.args[0].method, "GET")

    def test_client_fetches_wechat_post_with_paired_media_key(self) -> None:
        """手动提取视频号时必须保留同次详情中的媒体地址和密钥。"""

        client = TikHubClient(
            MonitorConfig(tikhub_api_key="token", tikhub_base_url="https://api.tikhub.dev")
        )
        payload = {
            "code": 200,
            "data": {
                "id": "14941130915890399732",
                "object_nonce_id": "14941130915890399733",
                "nickname": "视频号作者",
                "username": "v2_manual@finder",
                "title": "视频号手动提取测试",
                "media": {
                    "url": "https://finder/manual.mp4",
                    "url_token": "?token=one",
                    "decode_key": "987654321",
                },
            },
        }
        with patch(
            "article_monitor.tikhub.urllib.request.urlopen",
            return_value=FakeResponse(payload),
        ) as urlopen:
            post = client.fetch_post("https://weixin.qq.com/sph/manual")

        self.assertEqual(post.case_id, "wechat_channels_14941130915890399732")
        self.assertEqual(post.media_url, "https://finder/manual.mp4?token=one")
        self.assertEqual(post.decode_key, "987654321")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(
            json.loads(request.data.decode("utf-8")),
            {"share_url": "https://weixin.qq.com/sph/manual", "raw": False},
        )

    def test_client_rejects_unsupported_manual_extract_link(self) -> None:
        """手动提取不得把未知平台链接发送给 TikHub。"""

        client = TikHubClient(
            MonitorConfig(tikhub_api_key="token", tikhub_base_url="https://api.tikhub.dev")
        )

        with self.assertRaisesRegex(RuntimeError, "抖音或视频号"):
            client.fetch_post("https://example.com/video")


if __name__ == "__main__":
    unittest.main()
