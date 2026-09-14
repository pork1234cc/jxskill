#!/usr/bin/env python3
"""九宫格结构检测、拒绝策略和裁切测试。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import suppress
from pathlib import Path

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

from grid_processor import (
    fixed_coordinate_boxes,
    fixed_grid_geometry,
    process_grid_batch,
)


def batch_contract(width: int, height: int) -> dict:
    """创建九个真实场景的裁切合同。"""
    return {
        "gridId": "grid_001_009",
        "realSceneCount": 9,
        "fillerCount": 0,
        "requestedSourceSize": [width, height],
        "targetCellSize": {"width": width // 3, "height": height // 3},
        "slots": [{"slot": slot, "kind": "scene", "sceneIndex": slot} for slot in range(1, 10)],
    }


def full_bleed_grid(width: int, height: int, x_edges: list[int], y_edges: list[int]) -> Image.Image:
    """创建没有可见分隔带、但场景边界清晰的测试总图。"""
    image = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(image)
    for row in range(3):
        for col in range(3):
            color = (55 + row * 60, 70 + col * 55, 95 + (row + col) * 20)
            draw.rectangle(
                (x_edges[col], y_edges[row], x_edges[col + 1] - 1, y_edges[row + 1] - 1),
                fill=color,
            )
    return image


class GridProcessorTests(unittest.TestCase):
    """验证九格像素归属和结构拒绝规则。"""

    def test_fixed_boxes_cover_non_divisible_image(self) -> None:
        """不能三等分的尺寸也必须完整覆盖且不重叠。"""
        boxes = fixed_coordinate_boxes(1001, 1003)
        self.assertEqual(boxes[0], (0, 0, 333, 334))
        self.assertEqual(boxes[-1], (667, 668, 1001, 1003))
        self.assertEqual(
            fixed_grid_geometry(1001, 1003, boxes),
            {"columnWidths": [333, 334, 334], "rowHeights": [334, 334, 335], "fullCoverage": True},
        )

    def test_adaptive_seams_within_tolerance_are_used(self) -> None:
        """轻微偏移的隐形场景边界应按真实边界裁切。"""
        with tempfile.TemporaryDirectory(prefix="adaptive-", dir=TEST_TEMP_DIR) as temp_name:
            temp_dir = Path(temp_name)
            width, height = 900, 1500
            source = temp_dir / "grid.png"
            full_bleed_grid(width, height, [0, 320, 610, width], [0, 500, 1000, height]).save(source)
            cells, summary = process_grid_batch(source, batch_contract(width, height), temp_dir / "output", "9:16")
            self.assertEqual(summary["status"], "processed")
            self.assertEqual(summary["cropMode"], "adaptive_coordinates")
            self.assertEqual(cells[0]["sourceBox"], [0, 0, 320, 500])
            self.assertEqual(cells[2]["sourceBox"], [610, 0, 900, 500])

    def test_adaptive_seams_beyond_tolerance_are_rejected(self) -> None:
        """场景边界偏移超过容差时必须拒绝裁切并交给调用方重试。"""
        with tempfile.TemporaryDirectory(prefix="boundary-drift-", dir=TEST_TEMP_DIR) as temp_name:
            temp_dir = Path(temp_name)
            width, height = 900, 1500
            source = temp_dir / "grid.png"
            full_bleed_grid(width, height, [0, 340, 640, width], [0, 500, 1000, height]).save(source)
            cells, summary = process_grid_batch(source, batch_contract(width, height), temp_dir / "output", "9:16")
            self.assertEqual(cells, [])
            self.assertEqual(summary["status"], "rejected_boundary_drift")
            self.assertEqual(summary["cropMode"], "rejected_boundary_drift")
            self.assertFalse(summary["deliveryReady"])

    def test_visible_white_gutters_are_rejected(self) -> None:
        """可见白色分隔带不得通过裁掉白边来伪装成功。"""
        with tempfile.TemporaryDirectory(prefix="gutter-", dir=TEST_TEMP_DIR) as temp_name:
            temp_dir = Path(temp_name)
            width, height = 900, 1500
            image = Image.new("RGB", (width, height), (255, 255, 255))
            draw = ImageDraw.Draw(image)
            columns = [(0, 285), (315, 585), (615, 900)]
            rows = [(0, 480), (510, 990), (1020, 1500)]
            for row, (top, bottom) in enumerate(rows):
                for col, (left, right) in enumerate(columns):
                    draw.rectangle((left, top, right - 1, bottom - 1), fill=(80 + row * 30, 100 + col * 20, 130))
            source = temp_dir / "visible-grid.png"
            image.save(source)
            cells, summary = process_grid_batch(source, batch_contract(width, height), temp_dir / "output", "9:16")
            self.assertEqual(cells, [])
            self.assertEqual(summary["status"], "rejected_visible_grid")
            self.assertFalse(summary["deliveryReady"])


if __name__ == "__main__":
    unittest.main()
