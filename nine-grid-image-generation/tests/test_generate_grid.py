#!/usr/bin/env python3
"""九宫格 API 入口的输入、Prompt 与 dry-run 测试。"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import unittest
from contextlib import suppress
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

with suppress(AttributeError, OSError, ValueError):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
TEST_TEMP_DIR = SKILL_ROOT / ".tmp"
TEST_TEMP_DIR.mkdir(parents=True, exist_ok=True)
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from generate_grid import (
    API_ENDPOINT,
    DEFAULT_MODEL,
    ApiRequestError,
    build_grid_prompt,
    build_request_body,
    extract_image_bytes,
    run_generation,
    validate_plan,
)


def valid_plan() -> dict:
    """返回最小合法九宫格计划。"""
    return {
        "title": "春日江南",
        "ratio": "9:16",
        "style": "电影感国风摄影",
        "global_requirements": "低饱和青绿色，无文字和水印",
        "scenes": [{"slot": slot, "prompt": f"第 {slot} 个江南生活场景"} for slot in range(1, 10)],
    }


def generated_grid_payload(x_edges: list[int] | None = None, request_id: str = "request-test") -> dict:
    """构造无需联网即可通过结构检测的九宫格 API 响应。"""
    width, height = 900, 1500
    x_edges = x_edges or [0, 300, 600, width]
    image = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(image)
    for row in range(3):
        for col in range(3):
            color = (55 + row * 60, 70 + col * 55, 95 + (row + col) * 20)
            draw.rectangle((x_edges[col], row * 500, x_edges[col + 1] - 1, (row + 1) * 500 - 1), fill=color)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return {"id": request_id, "data": [{"b64_json": encoded}]}


class GenerateGridTests(unittest.TestCase):
    """验证 API 请求前可以确定的行为。"""

    def test_validate_plan_sorts_complete_slots(self) -> None:
        """乱序输入应按 slot 归一化，但不改变场景文本。"""
        payload = valid_plan()
        payload["scenes"].reverse()
        normalized = validate_plan(payload)
        self.assertEqual([scene["slot"] for scene in normalized["scenes"]], list(range(1, 10)))
        self.assertEqual(normalized["scenes"][0]["prompt"], "第 1 个江南生活场景")

    def test_validate_plan_rejects_duplicate_slot(self) -> None:
        """重复 slot 必须在调用 API 前被拒绝。"""
        payload = valid_plan()
        payload["scenes"][-1]["slot"] = 8
        with self.assertRaisesRegex(ValueError, "完整覆盖"):
            validate_plan(payload)

    def test_prompt_contains_positions_and_invisible_grid_contract(self) -> None:
        """复合 Prompt 应包含九个位置和无可见分隔线约束。"""
        prompt = build_grid_prompt(validate_plan(valid_plan()))
        for position in ("左上", "上中", "右上", "左中", "正中", "右中", "左下", "下中", "右下"):
            self.assertIn(position, prompt)
        self.assertIn("边界必须隐形", prompt)
        self.assertIn("zero gutters", prompt)
        self.assertNotIn("纯白分隔带", prompt)

    def test_request_body_matches_apii_contract(self) -> None:
        """9:16 请求应命中 API 易 4K 模型与公开字段。"""
        plan = validate_plan(valid_plan())
        body = build_request_body(plan, build_grid_prompt(plan))
        self.assertEqual(body["model"], DEFAULT_MODEL)
        self.assertEqual(body["aspect_ratio"], "9:16")
        self.assertNotIn("size", body)
        self.assertEqual(body["quality"], "high")
        self.assertEqual(body["output_format"], "png")
        self.assertNotIn("negative_prompt", body)

    def test_extract_image_bytes_accepts_base64_response(self) -> None:
        """响应解析器应支持 OpenAI 风格的 b64_json。"""
        content = b"fake-image-content"
        payload = {"id": "request-1", "data": [{"b64_json": base64.b64encode(content).decode("ascii")}]}
        decoded, request_id = extract_image_bytes(payload)
        self.assertEqual(decoded, content)
        self.assertEqual(request_id, "request-1")

    def test_extract_image_bytes_rejects_missing_content(self) -> None:
        """响应缺少 URL 和 base64 时应返回明确错误。"""
        with self.assertRaisesRegex(RuntimeError, "缺少图片 URL"):
            extract_image_bytes({"data": [{}]})

    def test_dry_run_writes_redacted_snapshot_without_api_key(self) -> None:
        """dry-run 只输出包含脱敏请求的 manifest。"""
        with tempfile.TemporaryDirectory(prefix="dry-run-", dir=TEST_TEMP_DIR) as temp_name:
            temp_dir = Path(temp_name)
            input_path = temp_dir / "input.json"
            output_dir = temp_dir / "output"
            input_path.write_text(json.dumps(valid_plan(), ensure_ascii=False), encoding="utf-8")
            manifest = run_generation(input_path, output_dir, dry_run=True)
            self.assertEqual(manifest["status"], "dry_run")
            self.assertEqual(manifest["request"]["url"], API_ENDPOINT)
            self.assertEqual(manifest["request"]["headers"]["Authorization"], "Bearer <redacted>")
            self.assertEqual({path.name for path in output_dir.iterdir()}, {"manifest.json"})

    def test_success_outputs_only_nine_cells_and_manifest_by_default(self) -> None:
        """成功任务默认只交付九张平铺裁片和一个 manifest。"""
        with tempfile.TemporaryDirectory(prefix="minimal-output-", dir=TEST_TEMP_DIR) as temp_name:
            temp_dir = Path(temp_name)
            input_path = temp_dir / "input.json"
            output_dir = temp_dir / "output"
            input_path.write_text(json.dumps(valid_plan(), ensure_ascii=False), encoding="utf-8")
            with (
                patch.dict(os.environ, {"APII_API_KEY": "test-key"}),
                patch("generate_grid.post_generation_request", return_value=generated_grid_payload()),
            ):
                manifest = run_generation(input_path, output_dir, dry_run=False)

            expected = {"manifest.json", *(f"scene_{slot:03d}.png" for slot in range(1, 10))}
            self.assertEqual({path.name for path in output_dir.iterdir()}, expected)
            self.assertEqual(manifest["status"], "processed")
            self.assertEqual(len(manifest["cells"]), 9)
            self.assertEqual(manifest["cells"][0]["image"], "scene_001.png")
            with Image.open(output_dir / "scene_001.png") as image:
                self.assertEqual(image.size, (720, 1280))

    def test_optional_artifacts_are_only_written_when_requested(self) -> None:
        """显式开启开关时才保留总图和联系表。"""
        with tempfile.TemporaryDirectory(prefix="optional-output-", dir=TEST_TEMP_DIR) as temp_name:
            temp_dir = Path(temp_name)
            input_path = temp_dir / "input.json"
            output_dir = temp_dir / "output"
            input_path.write_text(json.dumps(valid_plan(), ensure_ascii=False), encoding="utf-8")
            with (
                patch.dict(os.environ, {"APII_API_KEY": "test-key"}),
                patch("generate_grid.post_generation_request", return_value=generated_grid_payload()),
            ):
                manifest = run_generation(
                    input_path,
                    output_dir,
                    dry_run=False,
                    keep_debug_artifacts=True,
                    contact_sheet=True,
                )

            self.assertTrue((output_dir / "grid_attempt_1.png").is_file())
            self.assertTrue((output_dir / "contact_sheet.jpg").is_file())
            self.assertEqual(manifest["attempts"][0]["sourceImage"], "grid_attempt_1.png")
            self.assertEqual(manifest["contactSheet"], "contact_sheet.jpg")

    def test_api_error_outputs_manifest_without_partial_cells(self) -> None:
        """API 失败时只留下错误 manifest，并清理上次的标准产物。"""
        with tempfile.TemporaryDirectory(prefix="api-error-", dir=TEST_TEMP_DIR) as temp_name:
            temp_dir = Path(temp_name)
            input_path = temp_dir / "input.json"
            output_dir = temp_dir / "output"
            input_path.write_text(json.dumps(valid_plan(), ensure_ascii=False), encoding="utf-8")
            output_dir.mkdir()
            for slot in range(1, 10):
                (output_dir / f"scene_{slot:03d}.png").write_bytes(b"old-cell")
            (output_dir / "grid_attempt_1.png").write_bytes(b"old-grid")
            (output_dir / "contact_sheet.jpg").write_bytes(b"old-sheet")
            with (
                patch.dict(os.environ, {"APII_API_KEY": "test-key"}),
                patch("generate_grid.post_generation_request", side_effect=ApiRequestError("测试错误")),
                self.assertRaisesRegex(ApiRequestError, "测试错误"),
            ):
                run_generation(input_path, output_dir, dry_run=False)

            manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "api_error")
            self.assertEqual({path.name for path in output_dir.iterdir()}, {"manifest.json"})

    def test_boundary_drift_retries_once_then_rejects_delivery(self) -> None:
        """边界超限时重新生成一次，两次均失败则只交付错误 manifest。"""
        with tempfile.TemporaryDirectory(prefix="boundary-retry-", dir=TEST_TEMP_DIR) as temp_name:
            temp_dir = Path(temp_name)
            input_path = temp_dir / "input.json"
            output_dir = temp_dir / "output"
            input_path.write_text(json.dumps(valid_plan(), ensure_ascii=False), encoding="utf-8")
            responses = [
                generated_grid_payload([0, 340, 640, 900], "request-drift-1"),
                generated_grid_payload([0, 340, 640, 900], "request-drift-2"),
            ]
            with (
                patch.dict(os.environ, {"APII_API_KEY": "test-key"}),
                patch("generate_grid.post_generation_request", side_effect=responses) as request_mock,
                self.assertRaisesRegex(RuntimeError, "结构连续检测失败"),
            ):
                run_generation(input_path, output_dir, dry_run=False)

            manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(request_mock.call_count, 2)
            self.assertEqual(manifest["apiCalls"], 2)
            self.assertEqual(len(manifest["attempts"]), 2)
            self.assertEqual(manifest["status"], "rejected_boundary_drift")
            self.assertEqual({path.name for path in output_dir.iterdir()}, {"manifest.json"})


if __name__ == "__main__":
    unittest.main()



