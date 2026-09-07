from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from article_monitor.local_store import LocalAccountStore, LocalAccountStoreError
from article_monitor.project import temporary_root
from article_monitor.tikhub import MonitorAccount

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass


RECORDED_AT = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)


class LocalAccountStoreTests(unittest.TestCase):
    """验证本地账号清单的幂等读写和异常边界。"""

    def test_creates_and_updates_account_by_stable_key(self) -> None:
        """相同平台和 API 查询 ID 必须更新原账号而不是重复新增。"""

        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            path = Path(temp_value) / "accounts.md"
            store = LocalAccountStore(path)

            created = store.sync_account(
                MonitorAccount("douyin", "旧作者", "old", "sec-a"),
                "https://v.douyin.com/old/",
                RECORDED_AT,
            )
            updated = store.sync_account(
                MonitorAccount("douyin", "新作者", "new", "sec-a"),
                "https://v.douyin.com/new/",
                RECORDED_AT,
            )

            text = path.read_text(encoding="utf-8")
            accounts = store.list_monitor_accounts()

        self.assertEqual(created["action"], "create")
        self.assertEqual(updated["action"], "update")
        self.assertEqual(created["account_id"], "douyin:sec-a")
        self.assertEqual(accounts, [MonitorAccount("douyin", "新作者", "new", "sec-a")])
        self.assertEqual(text.count("douyin:sec-a"), 1)
        self.assertIn("| douyin:sec-a | 抖音 | 新作者 | new | sec-a |", text)
        self.assertIn("https://v.douyin.com/new/", text)

    def test_lists_accounts_in_file_order(self) -> None:
        """监控必须保持本地清单中的登记顺序。"""

        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            path = Path(temp_value) / "accounts.md"
            store = LocalAccountStore(path)
            first = MonitorAccount("douyin", "作者甲", "a", "sec-a")
            second = MonitorAccount("wechat_channels", "作者乙", "b", "finder-b")
            store.sync_account(first, "https://v.douyin.com/a/", RECORDED_AT)
            store.sync_account(second, "https://weixin.qq.com/b/", RECORDED_AT)

            accounts = store.list_monitor_accounts()

        self.assertEqual(accounts, [first, second])

    def test_missing_file_returns_empty_account_list(self) -> None:
        """尚未登记账号时监控应得到空列表。"""

        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            store = LocalAccountStore(Path(temp_value) / "accounts.md")
            self.assertEqual(store.list_monitor_accounts(), [])

    def test_invalid_markdown_stops_with_clear_error(self) -> None:
        """账号清单损坏时必须停止，禁止静默覆盖已有数据。"""

        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            path = Path(temp_value) / "accounts.md"
            path.write_text("# 被破坏的账号清单\n", encoding="utf-8")
            store = LocalAccountStore(path)

            with self.assertRaisesRegex(LocalAccountStoreError, "Markdown 格式无效"):
                store.list_monitor_accounts()

    def test_table_characters_round_trip_without_corrupting_columns(self) -> None:
        """账号字段中的表格字符必须转义并可无损读取。"""

        account = MonitorAccount("douyin", "作者|甲", r"name\one", "sec-a")
        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            path = Path(temp_value) / "accounts.md"
            store = LocalAccountStore(path)
            store.sync_account(account, "https://v.douyin.com/a/", RECORDED_AT)

            text = path.read_text(encoding="utf-8")
            accounts = store.list_monitor_accounts()

        self.assertIn(r"作者\|甲", text)
        self.assertIn(r"name\\one", text)
        self.assertEqual(accounts, [account])

    def test_manually_deleting_account_row_stops_monitoring_it(self) -> None:
        """用户删除账号整行后，本地清单不应再返回该账号。"""

        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            path = Path(temp_value) / "accounts.md"
            store = LocalAccountStore(path)
            store.sync_account(
                MonitorAccount("douyin", "作者甲", "author-a", "sec-a"),
                "https://v.douyin.com/a/",
                RECORDED_AT,
            )
            remaining_lines = [
                line
                for line in path.read_text(encoding="utf-8").splitlines()
                if "douyin:sec-a" not in line
            ]
            path.write_text("\n".join(remaining_lines) + "\n", encoding="utf-8")

            accounts = store.list_monitor_accounts()

        self.assertEqual(accounts, [])


if __name__ == "__main__":
    unittest.main()
