"""验证共用配置与独立 Skill 的目录边界。"""

from __future__ import annotations

import json
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

from article_monitor import cli, config, project, transcription


class SharedEnvironmentTests(unittest.TestCase):
    """使用临时工作区验证配置选择，不读取真实凭据。"""

    def setUp(self) -> None:
        """准备一个含独立 Skill 的临时工作区。"""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.skill = self.workspace / "monitor"
        self.skill.mkdir()
        self.patch_root = patch.object(project, "project_root", return_value=self.skill)
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)

    def enable_workspace(self, skills: list[str]) -> None:
        """写入工作区显式声明。"""
        (self.workspace / "skills-workspace.json").write_text(
            json.dumps({"skills": skills}, ensure_ascii=False), encoding="utf-8"
        )

    def test_declared_skill_uses_shared_configuration(self) -> None:
        """已声明的 Skill 使用根配置，进程参数仍然优先。"""
        self.enable_workspace(["monitor"])
        (self.workspace / ".env").write_text(
            "TIKHUB_API_KEY=公共测试值\nARTICLEMONITOR_STORAGE_BACKEND=local\n",
            encoding="utf-8",
        )
        self.assertEqual(project.environment_root(), self.workspace)
        self.assertEqual(config.load_config(environ={}).tikhub_api_key, "公共测试值")
        self.assertEqual(
            config.load_config(environ={"TIKHUB_API_KEY": "进程测试值"}).tikhub_api_key,
            "进程测试值",
        )

    def test_standalone_skill_does_not_read_parent_configuration(self) -> None:
        """无工作区声明时，忽略父目录配置并保持独立安装行为。"""
        (self.workspace / ".env").write_text("TIKHUB_API_KEY=无关值\n", encoding="utf-8")
        (self.skill / ".env").write_text("TIKHUB_API_KEY=独立值\n", encoding="utf-8")
        self.assertEqual(project.environment_root(), self.skill)
        self.assertEqual(config.load_config(environ={}).tikhub_api_key, "独立值")

    def test_unlisted_skill_remains_independent(self) -> None:
        """空名单或未声明当前 Skill 时不能读取工作区配置。"""
        for skills in ([], ["another-skill"]):
            with self.subTest(skills=skills):
                self.enable_workspace(skills)
                self.assertEqual(project.environment_root(), self.skill)

    def test_malformed_workspace_fails_clearly(self) -> None:
        """工作区声明损坏时停止，避免静默使用错误配置。"""
        marker = self.workspace / "skills-workspace.json"
        for content in ("{", "[]", '{"skills": "monitor"}'):
            with self.subTest(content=content):
                marker.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "工作区"):
                    project.environment_root()

    def test_shared_environment_does_not_expand_data_boundary(self) -> None:
        """共用环境不能使正式产物写入父目录。"""
        self.enable_workspace(["monitor"])
        self.assertEqual(project.account_registry_path().parent.parent, self.skill)
        with self.assertRaisesRegex(ValueError, "必须位于"):
            project.require_project_path(self.workspace / "outside.md", "业务文件")

    def test_explicit_shared_env_allows_only_declared_env_file(self) -> None:
        """CLI 允许明确指向共用 .env，不放开其他工作区文件。"""
        self.enable_workspace(["monitor"])
        env_path = self.workspace / ".env"
        env_path.write_text("TIKHUB_API_KEY=公共测试值\n", encoding="utf-8")
        self.assertEqual(cli.resolve_env_path(str(env_path)), env_path)
        with self.assertRaisesRegex(ValueError, "必须位于"):
            cli.resolve_env_path(str(self.workspace / "other.env"))

    def test_asr_reads_same_shared_configuration(self) -> None:
        """ASR 单独读取配置时也使用共用文件，且不发出网络请求。"""
        self.enable_workspace(["monitor"])
        (self.workspace / ".env").write_text(
            "DASHSCOPE_API_KEY=语音测试值\nDASHSCOPE_ASR_WORKSPACE_ID=test-workspace\n",
            encoding="utf-8",
        )
        with patch.dict("os.environ", {}, clear=True):
            asr_config = transcription.AsrConfig.from_env()
            self.assertEqual(asr_config.api_key, "语音测试值")
            self.assertEqual(asr_config.workspace_id, "test-workspace")


if __name__ == "__main__":
    unittest.main()
