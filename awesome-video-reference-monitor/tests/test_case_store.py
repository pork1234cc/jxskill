from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from article_monitor.case_store import (
    EXPECTED_FIELD_TYPES,
    ReferenceBitableClient,
    ReferenceBitableError,
    ReferenceBitableRecord,
    build_reference_record_from_markdown,
    reference_bitable_client_from_env,
)
from article_monitor.feishu import FeishuTableConfig

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass


def sample_record() -> ReferenceBitableRecord:
    """构造案例表同步测试数据。"""
    return ReferenceBitableRecord(
        case_id="wechat_channels_123",
        collected_at="2026-08-25 12:00:00",
        source_url="https://weixin.qq.com/sph/test",
        platform="wechat_channels",
        author="测试作者",
        book_name="测试书名",
        title="测试标题",
        topics=("#读书", "#养生"),
        like_count=10,
        favorite_count=20,
        comment_count=30,
        forward_count=40,
        duration="01:20",
        clean_transcript="清洗后的逐字稿",
    )


class ReferenceBitableTests(unittest.TestCase):
    """验证采集表字段映射和案例 ID 幂等更新。"""

    def make_client(self, *, existing: list[dict] | None = None):
        calls = []

        def transport(method, url, headers, body):
            calls.append((method, url, headers, body))
            if url.endswith("tenant_access_token/internal"):
                return {"code": 0, "tenant_access_token": "tenant_test"}
            if "/fields?" in url:
                fields = [
                    {"field_name": name, "type": field_type}
                    for name, field_type in EXPECTED_FIELD_TYPES.items()
                ]
                fields.append({"field_name": "类目", "type": 3})
                return {"code": 0, "data": {"items": fields, "has_more": False}}
            if method == "GET":
                return {
                    "code": 0,
                    "data": {"items": existing or [], "has_more": False},
                }
            record_id = "rec_existing" if method == "PUT" else "rec_new"
            return {"code": 0, "data": {"record": {"record_id": record_id}}}

        client = ReferenceBitableClient(
            FeishuTableConfig("cli_test", "secret", "base_test", "tbl_test"),
            transport=transport,
        )
        return client, calls

    def test_create_omits_category_and_preserves_field_types(self):
        """首次写入必须包含采集字段，但请求体不得出现类目。"""
        client, calls = self.make_client()

        result = client.sync(sample_record())

        self.assertEqual(result["action"], "create")
        fields = json.loads(calls[-1][3].decode("utf-8"))["fields"]
        self.assertNotIn("类目", fields)
        self.assertEqual(fields["案例 ID"], "wechat_channels_123")
        self.assertEqual(fields["话题"], ["#读书", "#养生"])
        self.assertIsInstance(fields["采集时间"], int)
        self.assertEqual(fields["原始链接"]["link"], "https://weixin.qq.com/sph/test")

    def test_empty_source_url_omits_hyperlink_value(self):
        """视频号监控缺少公开链接时不得提交空超链接对象。"""

        record = sample_record()
        record = ReferenceBitableRecord(
            case_id=record.case_id,
            collected_at=record.collected_at,
            source_url="",
            platform=record.platform,
            author=record.author,
            book_name=record.book_name,
            title=record.title,
            topics=record.topics,
            like_count=record.like_count,
            favorite_count=record.favorite_count,
            comment_count=record.comment_count,
            forward_count=record.forward_count,
            duration=record.duration,
            clean_transcript=record.clean_transcript,
        )

        fields = record.to_fields()

        self.assertNotIn("原始链接", fields)

    def test_repeated_case_updates_the_existing_record(self):
        """案例 ID 相同必须更新原记录，不能新增重复案例。"""
        existing = [
            {"record_id": "rec_existing", "fields": {"案例 ID": "wechat_channels_123"}}
        ]
        client, calls = self.make_client(existing=existing)

        result = client.sync(sample_record())

        self.assertEqual(result["action"], "update")
        self.assertEqual(result["record_id"], "rec_existing")
        self.assertEqual(calls[-1][0], "PUT")
        self.assertTrue(calls[-1][1].endswith("/records/rec_existing"))

    def test_reuses_schema_and_record_index_for_multiple_syncs(self):
        """同一监控进程同步多条素材时不得重复拉取表结构和全部记录。"""

        client, calls = self.make_client()

        client.sync(sample_record())
        client.sync(replace(sample_record(), case_id="wechat_channels_456"))

        field_calls = [url for method, url, _headers, _body in calls if "/fields?" in url]
        record_list_calls = [
            url
            for method, url, _headers, _body in calls
            if method == "GET" and "/records?" in url
        ]
        self.assertEqual(len(field_calls), 1)
        self.assertEqual(len(record_list_calls), 1)

    def test_duplicate_case_ids_are_rejected(self):
        """采集表已有重复案例 ID 时必须停止，避免关联到错误来源。"""
        existing = [
            {"record_id": "rec_1", "fields": {"案例 ID": "wechat_channels_123"}},
            {"record_id": "rec_2", "fields": {"案例 ID": "wechat_channels_123"}},
        ]
        client, _ = self.make_client(existing=existing)

        with self.assertRaisesRegex(ReferenceBitableError, "重复案例 ID"):
            client.sync(sample_record())

    def test_reference_table_id_is_required_and_user_configurable(self) -> None:
        """公开版本必须使用用户自己的案例表 ID，不能依赖硬编码表。"""

        base_values = {
            "FEISHU_APP_ID": "app-id",
            "FEISHU_APP_SECRET": "secret",
            "FEISHU_APP_TOKEN": "app-token",
        }
        with self.assertRaisesRegex(ReferenceBitableError, "FEISHU_REFERENCE_TABLE_ID"):
            reference_bitable_client_from_env(base_values)

        client = reference_bitable_client_from_env(
            {**base_values, "FEISHU_REFERENCE_TABLE_ID": "tbl_user_owned"}
        )
        self.assertEqual(client.config.table_id, "tbl_user_owned")

    def test_builds_retry_record_from_local_monitor_markdown(self) -> None:
        """首次飞书同步失败后，应兼容旧时间字段并重建重试记录。"""

        markdown = """# 测试标题

## 基础信息

| 字段 | 内容 |
|---|---|
| 案例 ID | douyin_123 |
| 采集时间 | 2026-08-26 12:00:00 |
| 平台 | 抖音 |
| 作者 | 测试作者 |
| 标题/描述 | 测试\\|标题 |
| 点赞 | 10 |
| 收藏 | 20 |
| 评论 | 30 |
| 转发 | 600 |
| 原始链接 | https://www.douyin.com/video/123 |
| 时长 | 00:01 |

## 清洗逐字稿

第一段。

第二段。

## 分句逐字稿

- [00:00 - 00:01] 第一段。
"""
        with tempfile.TemporaryDirectory() as temp_value:
            path = Path(temp_value) / "案例.md"
            path.write_text(markdown, encoding="utf-8")

            record = build_reference_record_from_markdown(path)

        self.assertEqual(record.case_id, "douyin_123")
        self.assertEqual(record.platform, "douyin")
        self.assertEqual(record.title, "测试|标题")
        self.assertEqual(record.forward_count, 600)
        self.assertEqual(record.clean_transcript, "第一段。\n\n第二段。")


if __name__ == "__main__":
    unittest.main()
