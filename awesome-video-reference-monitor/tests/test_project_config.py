from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

from article_monitor.config import (
    DEFAULT_TIKHUB_BASE_URL,
    apply_environment,
    load_config,
    read_env_file,
    require_tikhub_config,
)
from article_monitor.project import (
    account_registry_path,
    data_directories,
    monitor_output_root,
    project_root,
    require_project_path,
)


class ProjectConfigTests(unittest.TestCase):
    """验证独立项目路径边界和配置优先级。"""

    def test_project_root_is_standalone_repository(self) -> None:
        """源码推导的根目录应是 articlemonitor 自身。"""

        self.assertTrue((project_root() / "pyproject.toml").is_file())
        for directory in data_directories():
            self.assertTrue(directory.is_relative_to(project_root()))

    def test_local_business_directories_follow_numbered_workflow(self) -> None:
        """正式目录应依次保存账号、素材和人工筛选后的案例。"""

        root = project_root()

        self.assertEqual(
            data_directories(),
            (
                root / "1-对标账号",
                root / "2-素材库",
                root / "3-对标案例" / "文案",
            ),
        )
        self.assertEqual(account_registry_path(), root / "1-对标账号" / "accounts.md")
        self.assertEqual(monitor_output_root(), root / "3-对标案例")

    def test_rejects_path_outside_project(self) -> None:
        """正式写入目标不得逃逸到独立项目之外。"""

        with self.assertRaisesRegex(ValueError, "必须位于 articlemonitor 项目内"):
            require_project_path(project_root().parent, "测试目录")

    def test_process_environment_overrides_utf8_dotenv(self) -> None:
        """显式进程环境应覆盖项目 dotenv，中文内容必须正常读取。"""

        test_temp_root = project_root() / ".tmp"
        test_temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_temp_root) as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "TIKHUB_API_KEY=文件密钥\nTIKHUB_BASE_URL=https://文件地址.example\n",
                encoding="utf-8",
            )
            self.assertEqual(read_env_file(env_path)["TIKHUB_API_KEY"], "文件密钥")
            config = load_config(
                env_path,
                {"TIKHUB_API_KEY": "环境密钥"},
            )
        self.assertEqual(config.tikhub_api_key, "环境密钥")
        self.assertEqual(config.tikhub_base_url, "https://文件地址.example")

    def test_tikhub_defaults_and_required_key(self) -> None:
        """TikHub 地址有安全默认值，但 API Key 必须显式提供。"""

        config = load_config(project_root() / "不存在.env", environ={})
        self.assertEqual(config.tikhub_base_url, DEFAULT_TIKHUB_BASE_URL)
        self.assertEqual(config.storage_backend, "local")
        with self.assertRaisesRegex(RuntimeError, "TIKHUB_API_KEY"):
            require_tikhub_config(config)

    def test_storage_backend_accepts_feishu_and_rejects_unknown_values(self) -> None:
        """存储模式只能是 local 或 feishu，避免误拼写后写错数据源。"""

        config = load_config(
            project_root() / "不存在.env",
            environ={"ARTICLEMONITOR_STORAGE_BACKEND": " FeiShu "},
        )
        self.assertEqual(config.storage_backend, "feishu")

        with self.assertRaisesRegex(ValueError, "ARTICLEMONITOR_STORAGE_BACKEND"):
            load_config(
                project_root() / "不存在.env",
                environ={"ARTICLEMONITOR_STORAGE_BACKEND": "both"},
            )

    def test_dotenv_can_supply_subprocess_tools_without_overriding_process(self) -> None:
        """媒体子进程应继承 dotenv，但显式环境变量必须保持优先。"""

        with patch.dict(
            "article_monitor.config.os.environ",
            {"FFMPEG_PATH": "进程路径"},
            clear=True,
        ):
            apply_environment(
                {
                    "FFMPEG_PATH": "文件路径",
                    "WECHAT_DECRYPT_NODE_PATH": "Node路径",
                }
            )
            self.assertEqual(
                __import__("os").environ["FFMPEG_PATH"],
                "进程路径",
            )
            self.assertEqual(
                __import__("os").environ["WECHAT_DECRYPT_NODE_PATH"],
                "Node路径",
            )


if __name__ == "__main__":
    unittest.main()
