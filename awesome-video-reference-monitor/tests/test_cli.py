from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from article_monitor.cli import (
    build_monitor_filter,
    build_parser,
    resolve_env_path,
    run_command,
)
from article_monitor.project import project_root

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass


class CliTests(unittest.TestCase):
    """验证独立 CLI 不泄漏旧项目能力或路径。"""

    def test_parser_exposes_record_monitor_and_extract(self) -> None:
        """命令行应暴露登记、监控和手动提取，且不能接受外部输出目录。"""

        parser = build_parser()
        record = parser.parse_args(["record", "https://v.douyin.com/test/", "--json"])
        monitor = parser.parse_args(["monitor"])
        extract = parser.parse_args(["extract", "https://v.douyin.com/test/"])
        self.assertEqual(record.command, "record")
        self.assertEqual(monitor.command, "monitor")
        self.assertEqual(extract.command, "extract")
        self.assertFalse(hasattr(record, "output_dir"))
        self.assertFalse(hasattr(extract, "output_dir"))

    def test_rejects_env_file_outside_project(self) -> None:
        """显式 dotenv 也必须位于独立项目根目录内。"""

        with self.assertRaisesRegex(ValueError, "必须位于 articlemonitor 项目内"):
            resolve_env_path(str(project_root().parent / "outside.env"))

    def test_builds_structured_filter_from_repeatable_options(self) -> None:
        """CLI 应把重复筛选参数转换为默认且关系的结构化条件。"""

        args = build_parser().parse_args(
            [
                "monitor",
                "--filter",
                "like_count:gte:500",
                "--filter",
                "forward_like_ratio:gte:1.5",
            ]
        )

        monitor_filter = build_monitor_filter(args)

        self.assertEqual(monitor_filter.logic, "all")
        self.assertEqual(monitor_filter.window_hours, 72)
        self.assertEqual(
            [condition.to_dict() for condition in monitor_filter.conditions],
            [
                {"field": "like_count", "operator": "gte", "value": 500.0},
                {
                    "field": "forward_like_ratio",
                    "operator": "gte",
                    "value": 1.5,
                },
            ],
        )

    def test_filter_allows_explicit_any_and_custom_window(self) -> None:
        """CLI 应允许用户显式指定或关系与时间窗口。"""

        args = build_parser().parse_args(
            [
                "monitor",
                "--filter",
                "like_count:gte:500",
                "--filter-logic",
                "any",
                "--window-hours",
                "168",
            ]
        )

        monitor_filter = build_monitor_filter(args)

        self.assertEqual(monitor_filter.logic, "any")
        self.assertEqual(monitor_filter.window_hours, 168)

    def test_filter_rejects_invalid_condition_text(self) -> None:
        """格式损坏的结构化条件必须在扫描账号前停止。"""

        args = build_parser().parse_args(
            ["monitor", "--filter", "点赞至少五百"]
        )

        with self.assertRaisesRegex(ValueError, "字段:运算符:阈值"):
            build_monitor_filter(args)

    def test_filter_rejects_any_logic_without_conditions(self) -> None:
        """没有显式指标时不得悄悄忽略用户指定的或关系。"""

        args = build_parser().parse_args(["monitor", "--filter-logic", "any"])

        with self.assertRaisesRegex(ValueError, "至少一个 --filter"):
            build_monitor_filter(args)

    @patch("article_monitor.cli.ensure_project_directories")
    def test_invalid_filter_stops_before_storage_initialization(
        self,
        ensure_directories: Mock,
    ) -> None:
        """无效条件必须在目录和飞书客户端初始化前失败。"""

        args = build_parser().parse_args(
            ["monitor", "--filter", "unknown:gte:500"]
        )

        with self.assertRaisesRegex(ValueError, "不支持的筛选字段"):
            run_command(args)

        ensure_directories.assert_not_called()

    @patch("article_monitor.cli.ensure_project_directories")
    @patch("article_monitor.cli.account_registry_path", return_value=Path("1-对标账号/accounts.md"))
    @patch("article_monitor.cli.monitor_output_root", return_value=Path("3-对标案例"))
    @patch("article_monitor.cli.load_config")
    @patch("article_monitor.cli.merged_environment", return_value={"配置": "值"})
    @patch("article_monitor.cli.LocalAccountStore")
    @patch("article_monitor.cli.TikHubClient")
    @patch("article_monitor.cli.ReferenceMonitor")
    def test_record_uses_local_account_store_without_scanning(
        self,
        monitor_class: Mock,
        _tikhub_class: Mock,
        account_store_class: Mock,
        _merged_environment: Mock,
        load_config_mock: Mock,
        _output_root: Mock,
        _registry_path: Mock,
        _ensure_directories: Mock,
    ) -> None:
        """记录账号只初始化本地账号清单，不得扫描作品列表。"""

        args = build_parser().parse_args(
            ["record", "https://v.douyin.com/test/", "--json"]
        )
        load_config_mock.return_value.storage_backend = "local"
        monitor_class.return_value.record_account.return_value = {
            "ok": True,
            "account_action": "create",
        }
        payload = run_command(args)
        self.assertTrue(payload["ok"])
        account_store_class.assert_called_once_with(Path("1-对标账号/accounts.md"))
        monitor_class.return_value.monitor_all.assert_not_called()

    @patch("article_monitor.cli.ensure_project_directories")
    @patch("article_monitor.cli.account_registry_path", return_value=Path("1-对标账号/accounts.md"))
    @patch("article_monitor.cli.monitor_output_root", return_value=Path("3-对标案例"))
    @patch("article_monitor.cli.load_config")
    @patch("article_monitor.cli.merged_environment", return_value={"配置": "值"})
    @patch("article_monitor.cli.LocalAccountStore")
    @patch("article_monitor.cli.TikHubClient")
    @patch("article_monitor.cli.ReferenceMonitor")
    def test_monitor_uses_local_account_store_and_scans_accounts(
        self,
        monitor_class: Mock,
        _tikhub_class: Mock,
        account_store_class: Mock,
        _merged_environment: Mock,
        load_config_mock: Mock,
        _output_root: Mock,
        _registry_path: Mock,
        _ensure_directories: Mock,
    ) -> None:
        """监控使用本地账号清单，不初始化飞书案例同步。"""

        args = build_parser().parse_args(["monitor", "--json"])
        load_config_mock.return_value.storage_backend = "local"
        monitor_class.return_value.monitor_all.return_value = {
            "ok": True,
            "summary": {"failed": 0},
        }
        payload = run_command(args)
        self.assertTrue(payload["ok"])
        account_store_class.assert_called_once_with(Path("1-对标账号/accounts.md"))
        monitor_filter = monitor_class.call_args.kwargs["monitor_filter"]
        self.assertTrue(monitor_filter.to_dict()["uses_default_metrics"])
        monitor_class.return_value.monitor_all.assert_called_once_with()

    @patch("article_monitor.cli.ensure_project_directories")
    @patch(
        "article_monitor.cli.monitor_output_root",
        return_value=Path("3-对标案例"),
    )
    @patch("article_monitor.cli.load_config")
    @patch("article_monitor.cli.merged_environment", return_value={"配置": "值"})
    @patch("article_monitor.cli.LocalAccountStore")
    @patch("article_monitor.cli.TikHubClient")
    @patch("article_monitor.cli.ReferenceMonitor")
    def test_extract_skips_account_store_and_collects_single_post(
        self,
        monitor_class: Mock,
        _tikhub_class: Mock,
        account_store_class: Mock,
        _merged_environment: Mock,
        load_config_mock: Mock,
        _output_root: Mock,
        _ensure_directories: Mock,
    ) -> None:
        """手动提取不应登记账号或扫描账号列表。"""

        args = build_parser().parse_args(
            ["extract", "https://v.douyin.com/manual/", "--json"]
        )
        load_config_mock.return_value.storage_backend = "local"
        monitor_class.return_value.extract_post.return_value = {
            "ok": True,
            "action": "create",
            "markdown": "3-对标案例/文案/测试.md",
        }

        payload = run_command(args)

        self.assertTrue(payload["ok"])
        account_store_class.assert_not_called()
        monitor_class.return_value.extract_post.assert_called_once_with(
            "https://v.douyin.com/manual/"
        )
        monitor_class.return_value.record_account.assert_not_called()
        monitor_class.return_value.monitor_all.assert_not_called()

    def test_feishu_extract_validates_case_table_without_account_table(self) -> None:
        """飞书手动提取只校验案例表，不应初始化账号表。"""

        args = build_parser().parse_args(
            ["extract", "https://weixin.qq.com/sph/manual", "--json"]
        )
        values = {"ARTICLEMONITOR_STORAGE_BACKEND": "feishu"}
        config = Mock(storage_backend="feishu")
        case_client = Mock()
        with (
            patch("article_monitor.cli.ensure_project_directories"),
            patch("article_monitor.cli.merged_environment", return_value=values),
            patch("article_monitor.cli.load_config", return_value=config),
            patch("article_monitor.cli.TikHubClient"),
            patch("article_monitor.cli.LocalAccountStore") as local_store_class,
            patch(
                "article_monitor.cli.account_bitable_client_from_env"
            ) as account_store_factory,
            patch(
                "article_monitor.cli.reference_bitable_client_from_env",
                return_value=case_client,
            ),
            patch("article_monitor.cli.ReferenceMonitor") as monitor_class,
        ):
            monitor_class.return_value.extract_post.return_value = {
                "ok": True,
                "action": "create",
            }

            payload = run_command(args)

        self.assertTrue(payload["ok"])
        local_store_class.assert_not_called()
        account_store_factory.assert_not_called()
        case_client.validate_schema.assert_called_once_with()
        self.assertTrue(monitor_class.call_args.kwargs["sync_to_feishu"])

    def test_feishu_mode_uses_feishu_account_store_and_enables_case_sync(self) -> None:
        """飞书模式以账号表为唯一账号源，并显式开启案例表同步。"""

        args = build_parser().parse_args(
            ["record", "https://v.douyin.com/test/", "--json"]
        )
        values = {"ARTICLEMONITOR_STORAGE_BACKEND": "feishu"}
        config = Mock(storage_backend="feishu")
        feishu_store = Mock()
        with (
            patch("article_monitor.cli.ensure_project_directories"),
            patch("article_monitor.cli.merged_environment", return_value=values),
            patch("article_monitor.cli.load_config", return_value=config),
            patch("article_monitor.cli.TikHubClient"),
            patch("article_monitor.cli.LocalAccountStore") as local_store_class,
            patch(
                "article_monitor.cli.account_bitable_client_from_env",
                return_value=feishu_store,
            ) as feishu_store_factory,
            patch("article_monitor.cli.ReferenceMonitor") as monitor_class,
        ):
            monitor_class.return_value.record_account.return_value = {
                "ok": True,
                "account_action": "create",
            }

            payload = run_command(args)

        self.assertTrue(payload["ok"])
        local_store_class.assert_not_called()
        feishu_store_factory.assert_called_once_with(values)
        self.assertIs(
            monitor_class.call_args.kwargs["account_store"],
            feishu_store,
        )
        self.assertTrue(monitor_class.call_args.kwargs["sync_to_feishu"])

    def test_feishu_monitor_validates_case_table_before_scanning(self) -> None:
        """飞书监控应在扫描和 ASR 前校验案例表，避免完成采集后才失败。"""

        args = build_parser().parse_args(["monitor", "--json"])
        values = {"ARTICLEMONITOR_STORAGE_BACKEND": "feishu"}
        config = Mock(storage_backend="feishu")
        case_client = Mock()
        with (
            patch("article_monitor.cli.ensure_project_directories"),
            patch("article_monitor.cli.merged_environment", return_value=values),
            patch("article_monitor.cli.load_config", return_value=config),
            patch("article_monitor.cli.TikHubClient"),
            patch("article_monitor.cli.account_bitable_client_from_env"),
            patch(
                "article_monitor.cli.reference_bitable_client_from_env",
                return_value=case_client,
            ) as case_store_factory,
            patch("article_monitor.cli.ReferenceMonitor") as monitor_class,
        ):
            monitor_class.return_value.monitor_all.return_value = {
                "ok": True,
                "summary": {"failed": 0},
            }

            payload = run_command(args)

        self.assertTrue(payload["ok"])
        case_store_factory.assert_called_once_with(values)
        case_client.validate_schema.assert_called_once_with()
        monitor_class.return_value.monitor_all.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
