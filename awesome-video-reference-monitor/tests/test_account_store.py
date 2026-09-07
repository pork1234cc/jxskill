from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from typing import Any

from article_monitor.account_store import (
    ACCOUNT_FIELD_TYPES,
    AccountBitableError,
    MonitorAccountBitableClient,
)
from article_monitor.feishu import FeishuTableConfig, app_credentials
from article_monitor.tikhub import MonitorAccount

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

RECORDED_AT = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)


class FakeMonitorAccountBitableClient(MonitorAccountBitableClient):
    """用内存字段和记录验证账号表业务规则。"""

    def __init__(
        self,
        *,
        fields: list[dict[str, Any]] | None = None,
        records: list[dict[str, Any]] | None = None,
    ) -> None:
        """初始化隔离的字段、记录和调用历史。"""

        super().__init__(FeishuTableConfig("app", "secret", "base", "table"))
        self.fields = list(fields or base_fields())
        self.records = list(records or [])
        self.created_fields: list[tuple[str, int, dict[str, Any]]] = []
        self.saved_fields: list[tuple[dict[str, Any], str]] = []

    def list_fields(self) -> list[dict[str, Any]]:
        """返回当前内存字段。"""

        return list(self.fields)

    def create_field(
        self,
        field_name: str,
        field_type: int,
        property_: dict[str, Any] | None,
    ) -> str:
        """记录字段创建并立即加入内存结构。"""

        self.created_fields.append((field_name, field_type, property_))
        self.fields.append({"field_name": field_name, "type": field_type})
        return f"fld_{field_name}"

    def list_records(self) -> list[dict[str, Any]]:
        """返回当前内存记录。"""

        return list(self.records)

    def upsert(self, fields: dict[str, Any], record_id: str = "") -> str:
        """记录新增或更新请求，并同步修改内存记录。"""

        self.saved_fields.append((dict(fields), record_id))
        saved_id = record_id or f"rec_{len(self.records) + 1}"
        if record_id:
            for item in self.records:
                if item.get("record_id") == record_id:
                    item.setdefault("fields", {}).update(fields)
                    break
        else:
            self.records.append({"record_id": saved_id, "fields": dict(fields)})
        return saved_id


def base_fields() -> list[dict[str, Any]]:
    """构造目标表当前已经存在的基础字段。"""

    return [
        {"field_name": "序号", "type": 1, "is_primary": True},
        {"field_name": "作者", "type": 1},
        {
            "field_name": "平台",
            "type": 3,
            "property": {"options": [{"name": "视频号"}, {"name": "抖音"}]},
        },
    ]


def complete_fields() -> list[dict[str, Any]]:
    """构造已经完成首次建字段的账号表结构。"""

    return base_fields() + [
        {"field_name": name, "type": field_type}
        for name, field_type in ACCOUNT_FIELD_TYPES.items()
        if name not in {"序号", "作者", "平台"}
    ]


class MonitorAccountBitableTests(unittest.TestCase):
    """验证记录对标使用的飞书账号表契约。"""

    def test_prefers_standalone_app_token_name(self) -> None:
        """独立项目应优先读取通用 FEISHU_APP_TOKEN。"""

        credentials = app_credentials(
            {
                "FEISHU_APP_ID": "app-id",
                "FEISHU_APP_SECRET": "secret",
                "FEISHU_APP_TOKEN": "standalone-token",
                "FEISHU_REMIX_APP_TOKEN": "legacy-token",
            }
        )
        self.assertEqual(credentials, ("app-id", "secret", "standalone-token"))

    def test_first_sync_creates_missing_fields_and_new_account(self) -> None:
        """首次写入应补齐字段，然后新增账号而不启动监控。"""

        client = FakeMonitorAccountBitableClient()
        account = MonitorAccount("douyin", "作者甲", "author-a", "sec-a")

        result = client.sync_account(
            account,
            "https://v.douyin.com/source/",
            RECORDED_AT,
        )

        self.assertEqual(result["action"], "create")
        self.assertEqual(result["account_id"], "douyin:sec-a")
        self.assertEqual(result["record_id"], "rec_1")
        self.assertEqual(
            {name for name, _field_type, _property in client.created_fields},
            set(ACCOUNT_FIELD_TYPES) - {"序号", "作者", "平台"},
        )
        self.assertIn(
            ("登记作品链接", 15, None),
            client.created_fields,
        )
        fields = client.saved_fields[-1][0]
        self.assertEqual(fields["序号"], "0001")
        self.assertEqual(fields["作者"], "作者甲")
        self.assertEqual(fields["平台"], "抖音")
        self.assertEqual(fields["账号标识"], "author-a")
        self.assertEqual(fields["API查询ID"], "sec-a")
        self.assertEqual(fields["登记作品链接"]["link"], "https://v.douyin.com/source/")
        self.assertIsInstance(fields["记录时间"], int)

    def test_same_platform_and_api_id_updates_existing_account(self) -> None:
        """平台与API查询ID相同应更新原记录，不能新增重复账号。"""

        client = FakeMonitorAccountBitableClient(
            fields=complete_fields(),
            records=[
                {
                    "record_id": "rec_existing",
                    "fields": {
                        "作者": "旧作者",
                        "平台": "抖音",
                        "账号标识": "old",
                        "API查询ID": "sec-a",
                        "记录时间": 123,
                    },
                }
            ],
        )

        result = client.sync_account(
            MonitorAccount("douyin", "新作者", "new", "sec-a"),
            "https://v.douyin.com/new/",
            RECORDED_AT,
        )

        self.assertEqual(result["action"], "update")
        saved, record_id = client.saved_fields[-1]
        self.assertEqual(record_id, "rec_existing")
        self.assertEqual(saved["作者"], "新作者")
        self.assertNotIn("序号", saved)
        self.assertNotIn("记录时间", saved)

    def test_legacy_author_match_is_enriched_instead_of_duplicated(self) -> None:
        """新字段为空的旧账号应按平台和作者唯一匹配并补齐。"""

        client = FakeMonitorAccountBitableClient(
            fields=complete_fields(),
            records=[
                {
                    "record_id": "rec_legacy",
                    "fields": {"作者": "清芷家风", "平台": "视频号"},
                }
            ]
        )

        result = client.sync_account(
            MonitorAccount(
                "wechat_channels",
                "清芷家风",
                "finder-name",
                "finder-name",
            ),
            "https://weixin.qq.com/sph/source",
            RECORDED_AT,
        )

        self.assertEqual(result["action"], "update")
        self.assertEqual(client.saved_fields[-1][1], "rec_legacy")

    def test_wrong_existing_field_type_stops_before_writing(self) -> None:
        """同名字段类型错误时必须停止，禁止创建重名字段。"""

        client = FakeMonitorAccountBitableClient(
            fields=base_fields() + [{"field_name": "API查询ID", "type": 2}]
        )

        with self.assertRaisesRegex(AccountBitableError, "字段类型不匹配"):
            client.sync_account(
                MonitorAccount("douyin", "作者", "name", "sec"),
                "https://v.douyin.com/source/",
                RECORDED_AT,
            )

        self.assertEqual(client.saved_fields, [])

    def test_list_accounts_skips_rows_without_api_id(self) -> None:
        """监控只读取已经具备平台和API查询ID的有效账号。"""

        client = FakeMonitorAccountBitableClient(
            fields=complete_fields(),
            records=[
                {"record_id": "legacy", "fields": {"作者": "旧行", "平台": "抖音"}},
                {
                    "record_id": "valid",
                    "fields": {
                        "作者": "有效账号",
                        "平台": "视频号",
                        "账号标识": "finder",
                        "API查询ID": "finder",
                    },
                },
            ]
        )

        accounts = client.list_monitor_accounts()

        self.assertEqual(
            accounts,
            [MonitorAccount("wechat_channels", "有效账号", "finder", "finder")],
        )


if __name__ == "__main__":
    unittest.main()
