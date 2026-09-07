from __future__ import annotations

import sys
import unittest
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = PROJECT_ROOT


class SkillTests(unittest.TestCase):
    """验证独立 Skill 的触发、命令和项目边界。"""

    @classmethod
    def setUpClass(cls) -> None:
        """以 UTF-8 读取 Skill 入口和按需引用。"""

        cls.skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        cls.configuration = (SKILL_ROOT / "references" / "configuration.md").read_text(
            encoding="utf-8"
        )
        cls.contracts = (SKILL_ROOT / "references" / "data-contracts.md").read_text(
            encoding="utf-8"
        )
        cls.bootstrap = (SKILL_ROOT / "scripts" / "bootstrap.ps1").read_text(
            encoding="utf-8"
        )

    def test_description_routes_three_supported_commands(self) -> None:
        """描述应覆盖登记、监控和手动提取三个触发语。"""

        frontmatter = self.skill.split("---", 2)[1]
        self.assertIn("name: awesome-video-reference-monitor", frontmatter)
        for marker in ("记录对标", "监控对标", "手动提取文案"):
            self.assertIn(marker, frontmatter)

    def test_uses_only_standalone_cli(self) -> None:
        """通用 Agent Skill 必须调用当前项目 CLI，不引用特定 Agent 产品。"""

        self.assertIn("-m article_monitor record", self.skill)
        self.assertIn("-m article_monitor monitor", self.skill)
        self.assertIn("-m article_monitor extract", self.skill)
        self.assertIn("scripts/article_monitor/", self.skill)
        self.assertIn("不依赖特定 Agent 产品", self.skill)
        for forbidden in (
            "duibiao_collector",
            "E:\\6-视频号项目",
            "--output-dir",
            ".agents/skills/",
            "src/article_monitor/",
        ):
            self.assertNotIn(forbidden, self.skill)

    def test_manual_extract_documents_output_and_deduplication(self) -> None:
        """手动提取应固定写入案例目录，并明确跨目录去重。"""

        for marker in (
            "3-对标案例/文案/",
            "2-素材库/",
            "案例 ID",
            "不下载媒体、不调用 ASR",
            "不登记账号",
        ):
            self.assertIn(marker, self.skill)

    def test_distribution_contains_all_runtime_resources(self) -> None:
        """根级 Skill 必须自包含安装后运行所需的全部资源。"""

        required_files = (
            "SKILL.md",
            "agents/openai.yaml",
            "assets/env.example",
            "references/configuration.md",
            "references/data-contracts.md",
            "scripts/bootstrap.ps1",
            "scripts/article_monitor/__main__.py",
            "scripts/wechat-decrypt/bridge.mjs",
            "scripts/wechat-decrypt/package.json",
            "scripts/wechat-decrypt/package-lock.json",
            "scripts/wechat-decrypt/vendor/wasm_video_decode.js",
            "scripts/wechat-decrypt/vendor/wasm_video_decode.wasm",
            "scripts/wechat-decrypt/vendor/LICENSE",
        )
        for relative_path in required_files:
            with self.subTest(relative_path=relative_path):
                self.assertTrue((PROJECT_ROOT / relative_path).is_file())

    def test_bootstrap_uses_windows_node_command_shims(self) -> None:
        """Windows 初始化必须绕过可能错误解析参数的 npm.ps1。"""

        self.assertIn("Get-Command npm.cmd", self.bootstrap)
        self.assertIn("Get-Command npx.cmd", self.bootstrap)
        self.assertNotIn("& npm install", self.bootstrap)
        self.assertNotIn("& npx playwright", self.bootstrap)
        self.assertEqual(
            self.bootstrap.count(
                "Copy-Item -LiteralPath $configTemplate -Destination $configPath"
            ),
            1,
        )

    def test_maps_natural_language_filters_to_structured_cli(self) -> None:
        """Skill 应把中文条件映射为可校验参数而非交给核心程序猜测。"""

        for marker in (
            "like_count:gte:500",
            "forward_like_ratio:gte:1.5",
            "--filter-logic any",
            "--window-hours",
            "点赞 500",
            "默认使用 `all`",
        ):
            self.assertIn(marker, self.skill)

    def test_contracts_define_filter_replacement_and_missing_values(self) -> None:
        """数据契约应锁定替换、默认且关系和比例缺失值规则。"""

        for marker in (
            "替换原有指标门槛",
            "没有提到的指标不参与筛选",
            "默认使用“且”",
            "点赞数为零或缺失",
            "比例条件不通过",
            "未提供自定义指标条件",
        ):
            self.assertIn(marker, self.contracts)

    def test_documents_project_directories_and_business_thresholds(self) -> None:
        """数据契约应保留目录、窗口、门槛和已有案例保护。"""

        for marker in (
            "1-对标账号/accounts.md",
            "2-素材库/",
            "3-对标案例/文案/",
            "72 小时",
            "转发数至少 500",
            "1.5",
            "不重复 ASR",
            "项目 `.tmp/`",
        ):
            self.assertIn(marker, self.contracts)

    def test_configuration_documents_local_and_feishu_modes(self) -> None:
        """独立配置应说明双模式和飞书必填项，不得混入 GetOneAPI。"""

        for marker in (
            "TIKHUB_API_KEY",
            "DASHSCOPE_API_KEY",
            "ARTICLEMONITOR_STORAGE_BACKEND",
            "FEISHU_APP_ID",
            "FEISHU_APP_SECRET",
            "FEISHU_APP_TOKEN",
            "DUIBIAO_TABLE_ID",
            "FEISHU_REFERENCE_TABLE_ID",
        ):
            self.assertIn(marker, self.configuration)
        self.assertNotIn("GETONEAPI", self.configuration.upper())

    def test_contracts_keep_one_account_source_and_local_retry(self) -> None:
        """Skill 必须避免账号双写，并在飞书失败后保留本地素材重试。"""

        for marker in (
            "local",
            "feishu",
            "唯一账号源",
            "保留本地 Markdown",
            "下轮监控",
            "不重复 ASR",
        ):
            self.assertIn(marker, self.contracts)


if __name__ == "__main__":
    unittest.main()
