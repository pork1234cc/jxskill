#!/usr/bin/env python3
"""检测并切分九宫格总图，输出可审计的场景图片。"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageOps, ImageStat

SKILL_ROOT = Path(__file__).resolve().parents[1]
ROOT_DIR = SKILL_ROOT


def read_json(path: Path) -> dict[str, Any]:
    """读取 UTF-8 JSON 对象。"""
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise TypeError(f"JSON 顶层必须是对象: {path}")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """以 UTF-8 写入格式化 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def relative_to_root(path: Path) -> str:
    """Skill 内路径写为相对路径，外部交付路径保留绝对路径。"""
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT_DIR).as_posix()
    except ValueError:
        return str(resolved)

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy 是可选加速依赖。
    np = None

with suppress(AttributeError, OSError, ValueError):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


# 九宫格复合图直接传给生图接口的明确画布尺寸。
# 不依赖 quality/4k 等抽象档位参数，接口请求始终使用这里的宽x高。
GRID_SOURCE_SIZES: dict[str, tuple[int, int]] = {
    "1:1": (4096, 4096),
    "3:4": (3520, 4704),
    "4:3": (4704, 3520),
    "9:16": (3040, 5504),
    "16:9": (5504, 3040),
}

SUPPORTED_RATIOS = tuple(GRID_SOURCE_SIZES)
WHITE_GAP_THRESHOLD = 240
WHITE_GAP_FRACTION = 0.95
MIN_WHITE_GAP_PX = 10
UNIFORM_GAP_MEAN_THRESHOLD = 218.0
UNIFORM_GAP_VARIANCE_THRESHOLD = 400.0
UNIFORM_GAP_WHITE_THRESHOLD = 220
UNIFORM_GAP_MIN_FRACTION = 0.45
DARK_GAP_THRESHOLD = 60
DARK_GAP_FRACTION = 0.70
# 深色分隔带必须是窄带。暗色油画、夜景等可能让数百像素宽的场景区域
# 同时满足“均值较低、暗像素占比较高”，不能把这类画面内容当成网格线。
MAX_DARK_GUTTER_FRACTION = 0.05
GUTTER_SAFETY_INSET_PX = 2
# 外框检测以连续深色主体为边界；额外留出 6 px 以消除白色虚线的抗锯齿残留。
OUTER_FRAME_SAFETY_INSET_PX = 6
MAX_OUTER_MARGIN_FRACTION = 0.20
MIN_CONTENT_FRACTION = 0.08
GRID_DETECTION_SAMPLE_MAX = 1200
ADAPTIVE_SEAM_SEARCH_RATIO = 0.18
ADAPTIVE_SEAM_MAX_OFFSET_RATIO = 0.08
ADAPTIVE_SEAM_MIN_PROMINENCE = 12.0
ADAPTIVE_SEAM_REFINE_RADIUS_PX = 8


def grid_source_size(ratio: str, override: tuple[int, int] | None = None) -> tuple[int, int]:
    if override is not None:
        width, height = override
        return int(width), int(height)
    if ratio not in GRID_SOURCE_SIZES:
        raise ValueError(f"九宫格只支持比例：{', '.join(SUPPORTED_RATIOS)}")
    return GRID_SOURCE_SIZES[ratio]


def grid_cell_size(ratio: str, override: tuple[int, int] | None = None) -> tuple[int, int]:
    """标准满版画布三等分后的最小格尺寸；override 用于按生图模型 profile 覆盖全局表。"""
    width, height = grid_source_size(ratio, override=override)
    return width // 3, height // 3


def fixed_coordinate_boxes(width: int, height: int) -> list[tuple[int, int, int, int]]:
    """按实际源图尺寸三等分，完整且无重叠地归属所有源像素。"""
    if width < 3 or height < 3:
        raise ValueError(f"九宫格源图尺寸过小：{width}x{height}")
    x_edges = [(index * width) // 3 for index in range(4)]
    y_edges = [(index * height) // 3 for index in range(4)]
    return [
        (x_edges[col], y_edges[row], x_edges[col + 1], y_edges[row + 1])
        for row in range(3)
        for col in range(3)
    ]


def fixed_grid_geometry(width: int, height: int, boxes: list[tuple[int, int, int, int]]) -> dict[str, Any]:
    """校验固定坐标网格的覆盖、边界和单元尺寸契约。"""
    if len(boxes) != 9:
        raise ValueError(f"固定坐标九宫格必须产生 9 个裁片，当前为 {len(boxes)}")
    column_widths = [boxes[col][2] - boxes[col][0] for col in range(3)]
    row_heights = [boxes[row * 3][3] - boxes[row * 3][1] for row in range(3)]
    valid = (
        boxes[0][:2] == (0, 0)
        and boxes[-1][2:] == (width, height)
        and sum(column_widths) == width
        and sum(row_heights) == height
        and max(column_widths) - min(column_widths) <= 1
        and max(row_heights) - min(row_heights) <= 1
    )
    if not valid:
        raise ValueError(f"固定坐标九宫格几何校验失败：{width}x{height}")
    return {"columnWidths": column_widths, "rowHeights": row_heights, "fullCoverage": True}


def as_root_relative(path: Path) -> str:
    try:
        return relative_to_root(path)
    except (OSError, ValueError):
        return str(path)


def clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, int(value)))


def load_plan(plan_path: Path | None, ratio: str, grid_path: Path) -> dict[str, Any]:
    if plan_path and plan_path.exists():
        return read_json(plan_path)
    width, height = grid_cell_size(ratio)
    return {
        "version": 1,
        "provider": "ai",
        "ratio": ratio,
        "targetCellSize": {"width": width, "height": height},
        "batches": [
            {
                "gridId": grid_path.stem,
                "rows": 3,
                "cols": 3,
                "mode": "main_visual",
                "status": "planned",
                "sourceImage": as_root_relative(grid_path),
                "slots": [
                    {
                        "slot": slot,
                        "kind": "scene",
                        "sceneIndex": slot,
                        "frameId": "",
                        "shotId": slot,
                        "frameType": "main_visual",
                        "prompt": "",
                    }
                    for slot in range(1, 10)
                ],
            }
        ],
    }


def write_plan(plan_path: Path, payload: dict[str, Any]) -> None:
    write_json(plan_path, payload)


def smooth_scores(scores: list[float], radius: int) -> list[float]:
    if radius <= 0 or len(scores) < 3:
        return scores
    window = radius * 2 + 1
    running = sum(scores[:window])
    output: list[float] = []
    for index in range(len(scores)):
        if index == 0:
            running = sum(scores[: min(len(scores), window)])
        else:
            add_index = index + radius
            remove_index = index - radius - 1
            if add_index < len(scores):
                running += scores[add_index]
            if remove_index >= 0:
                running -= scores[remove_index]
        left = max(0, index - radius)
        right = min(len(scores), index + radius + 1)
        output.append(running / max(1, right - left))
    return output


def projection_lines_pillow(gray: Image.Image, axis: str) -> list[float]:
    width, height = gray.size
    scores: list[float] = []
    if axis == "x":
        for x in range(width):
            stat = ImageStat.Stat(gray.crop((x, 0, x + 1, height)))
            scores.append(255.0 - float(stat.mean[0]))
    else:
        for y in range(height):
            stat = ImageStat.Stat(gray.crop((0, y, width, y + 1)))
            scores.append(255.0 - float(stat.mean[0]))
    return scores


def projection_lines(gray: Image.Image, axis: str) -> list[float]:
    if np is None:
        return projection_lines_pillow(gray, axis)
    arr = np.asarray(gray, dtype=np.float32)
    if axis == "x":
        return (255.0 - arr.mean(axis=0)).tolist()
    return (255.0 - arr.mean(axis=1)).tolist()


def white_fraction_scores_pillow(gray: Image.Image, axis: str) -> list[float]:
    width, height = gray.size
    scores: list[float] = []
    if axis == "x":
        for x in range(width):
            hist = gray.crop((x, 0, x + 1, height)).histogram()
            scores.append(sum(hist[WHITE_GAP_THRESHOLD + 1 :]) / max(1, height))
    else:
        for y in range(height):
            hist = gray.crop((0, y, width, y + 1)).histogram()
            scores.append(sum(hist[WHITE_GAP_THRESHOLD + 1 :]) / max(1, width))
    return scores


def white_fraction_scores(gray: Image.Image, axis: str) -> list[float]:
    if np is None:
        return white_fraction_scores_pillow(gray, axis)
    arr = np.asarray(gray, dtype=np.uint8)
    white = arr > WHITE_GAP_THRESHOLD
    if axis == "x":
        return white.mean(axis=0).astype(float).tolist()
    return white.mean(axis=1).astype(float).tolist()


def uniform_light_metrics_pillow(gray: Image.Image, axis: str) -> list[tuple[float, float, float]]:
    width, height = gray.size
    total = width if axis == "x" else height
    line_length = height if axis == "x" else width
    metrics: list[tuple[float, float, float]] = []
    for index in range(total):
        box = (index, 0, index + 1, height) if axis == "x" else (0, index, width, index + 1)
        region = gray.crop(box)
        stat = ImageStat.Stat(region)
        histogram = region.histogram()
        mean = float(stat.mean[0])
        variance = float(stat.var[0] if stat.var else 0.0)
        white_fraction = sum(histogram[UNIFORM_GAP_WHITE_THRESHOLD + 1 :]) / max(1, line_length)
        metrics.append((mean, variance, white_fraction))
    return metrics


def uniform_light_metrics(gray: Image.Image, axis: str) -> list[tuple[float, float, float]]:
    if np is None:
        return uniform_light_metrics_pillow(gray, axis)
    arr = np.asarray(gray, dtype=np.float32)
    reduce_axis = 0 if axis == "x" else 1
    means = arr.mean(axis=reduce_axis)
    variances = arr.var(axis=reduce_axis)
    white_fractions = (arr > UNIFORM_GAP_WHITE_THRESHOLD).mean(axis=reduce_axis)
    return [
        (float(mean), float(variance), float(white_fraction))
        for mean, variance, white_fraction in zip(means, variances, white_fractions)
    ]


def uniform_dark_metrics(gray: Image.Image, axis: str) -> list[tuple[float, float, float]]:
    """计算贯穿画布的深色分隔带特征，允许其中带有少量白色虚线。"""
    if np is None:
        width, height = gray.size
        total = width if axis == "x" else height
        line_length = height if axis == "x" else width
        metrics: list[tuple[float, float, float]] = []
        for index in range(total):
            box = (index, 0, index + 1, height) if axis == "x" else (0, index, width, index + 1)
            region = gray.crop(box)
            stat = ImageStat.Stat(region)
            dark_fraction = sum(region.histogram()[:DARK_GAP_THRESHOLD]) / max(1, line_length)
            metrics.append((float(stat.mean[0]), float(stat.var[0] if stat.var else 0.0), dark_fraction))
        return metrics
    arr = np.asarray(gray, dtype=np.float32)
    reduce_axis = 0 if axis == "x" else 1
    means = arr.mean(axis=reduce_axis)
    variances = arr.var(axis=reduce_axis)
    dark_fractions = (arr < DARK_GAP_THRESHOLD).mean(axis=reduce_axis)
    return [
        (float(mean), float(variance), float(dark_fraction))
        for mean, variance, dark_fraction in zip(means, variances, dark_fractions)
    ]


def is_uniform_light_line(metric: tuple[float, float, float]) -> bool:
    mean, variance, white_fraction = metric
    return (
        mean >= UNIFORM_GAP_MEAN_THRESHOLD
        and variance <= UNIFORM_GAP_VARIANCE_THRESHOLD
        and white_fraction >= UNIFORM_GAP_MIN_FRACTION
    )


def is_uniform_dark_line(metric: tuple[float, float, float]) -> bool:
    mean, _variance, dark_fraction = metric
    return mean <= DARK_GAP_THRESHOLD and dark_fraction >= DARK_GAP_FRACTION


def contiguous_true_runs(flags: list[bool]) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    index = 0
    while index < len(flags):
        if not flags[index]:
            index += 1
            continue
        start = index
        while index < len(flags) and flags[index]:
            index += 1
        runs.append((start, index))
    return runs


def uniform_band_confidence(metrics: list[tuple[float, float, float]], start: int, end: int) -> float:
    if end <= start:
        return 0.0
    scores: list[float] = []
    for mean, variance, white_fraction in metrics[start:end]:
        brightness = max(0.0, min(1.0, (mean - UNIFORM_GAP_MEAN_THRESHOLD) / (240.0 - UNIFORM_GAP_MEAN_THRESHOLD)))
        uniformity = max(0.0, min(1.0, 1.0 - variance / UNIFORM_GAP_VARIANCE_THRESHOLD))
        scores.append(brightness * 0.25 + uniformity * 0.25 + white_fraction * 0.5)
    return sum(scores) / max(1, len(scores))


def find_uniform_gap_lines(
    metrics: list[tuple[float, float, float]],
    total: int,
    min_gap: int,
    guard: int,
    content_range: tuple[int, int] | None = None,
) -> list[tuple[int, int, float]]:
    flags = [is_uniform_light_line(metric) for metric in metrics]
    content_start, content_end = content_range or (0, total)
    runs = [
        (start, end)
        for start, end in contiguous_true_runs(flags)
        if (
            end - start >= min_gap
            and start > max(guard, content_start + min_gap)
            and end < min(total - guard, content_end - min_gap)
        )
    ]
    if len(runs) < 2:
        return []
    scored_runs = [
        (start, end, uniform_band_confidence(metrics, start, end))
        for start, end in runs
    ]
    span = max(1, content_end - content_start)
    min_content = max(1, round(span * MIN_CONTENT_FRACTION))
    candidates: list[tuple[float, tuple[int, int, float], tuple[int, int, float]]] = []
    for first, second in itertools.combinations(scored_runs, 2):
        if first[0] > second[0]:
            first, second = second, first
        panel_widths = [
            first[0] - content_start,
            second[0] - first[1],
            content_end - second[1],
        ]
        if min(panel_widths) < min_content:
            continue
        balance = min(panel_widths) / max(panel_widths)
        gap_fraction = ((first[1] - first[0]) + (second[1] - second[0])) / span
        if gap_fraction > 0.30:
            continue
        confidence = (first[2] + second[2]) / 2
        # 等宽只作为软评分，真实白带位置不需要接近严格三等分。
        pair_score = confidence * 0.78 + balance * 0.22
        candidates.append((pair_score, first, second))
    if not candidates:
        return []
    _, first, second = max(candidates, key=lambda item: item[0])
    return [first, second]


def find_uniform_dark_gap_lines(
    metrics: list[tuple[float, float, float]],
    total: int,
    min_gap: int,
    guard: int,
    content_range: tuple[int, int] | None = None,
) -> list[tuple[int, int, float]]:
    """寻找两条贯穿画布的深色分隔带，不要求其纯色无纹理。"""
    content_start, content_end = content_range or (0, total)
    runs = [
        (start, end)
        for start, end in contiguous_true_runs([is_uniform_dark_line(metric) for metric in metrics])
        if end - start >= min_gap and start > max(guard, content_start + min_gap) and end < min(total - guard, content_end - min_gap)
    ]
    min_content = max(1, round((content_end - content_start) * MIN_CONTENT_FRACTION))
    candidates: list[tuple[float, tuple[int, int, float], tuple[int, int, float]]] = []
    for first_run, second_run in itertools.combinations(runs, 2):
        panel_widths = [first_run[0] - content_start, second_run[0] - first_run[1], content_end - second_run[1]]
        if min(panel_widths) < min_content:
            continue
        first = (*first_run, dark_band_confidence(metrics, *first_run))
        second = (*second_run, dark_band_confidence(metrics, *second_run))
        balance = min(panel_widths) / max(panel_widths)
        candidates.append(((first[2] + second[2]) / 2 * 0.80 + balance * 0.20, first, second))
    if not candidates:
        return []
    _, first, second = max(candidates, key=lambda item: item[0])
    return [first, second]


def dark_band_confidence(metrics: list[tuple[float, float, float]], start: int, end: int) -> float:
    values = metrics[start:end]
    if not values:
        return 0.0
    scores = [max(0.0, min(1.0, (DARK_GAP_THRESHOLD - mean) / DARK_GAP_THRESHOLD)) * 0.35 + fraction * 0.65 for mean, _variance, fraction in values]
    return sum(scores) / len(scores)


def detect_outer_dark_content_range(
    metrics: list[tuple[float, float, float]],
    total: int,
    min_gap: int,
) -> tuple[tuple[int, int], dict[str, list[int] | None]]:
    """识别贯穿画布最外圈的深色边框，允许白色虚线等装饰。"""
    flags = [is_uniform_dark_line(metric) for metric in metrics]
    leading = 0
    while leading < len(flags) and flags[leading]:
        leading += 1
    trailing = 0
    while trailing < len(flags) and flags[len(flags) - trailing - 1]:
        trailing += 1
    max_margin = round(total * MAX_OUTER_MARGIN_FRACTION)
    if leading < min_gap or leading > max_margin:
        leading = 0
    if trailing < min_gap or trailing > max_margin:
        trailing = 0
    content_start = clamp(leading, 0, total - 1)
    content_end = clamp(total - trailing, content_start + 1, total)
    return (
        (content_start, content_end),
        {
            "leading": [0, content_start] if content_start else None,
            "trailing": [content_end, total] if content_end < total else None,
        },
    )


def detect_outer_content_range(
    metrics: list[tuple[float, float, float]],
    total: int,
    min_gap: int,
) -> tuple[tuple[int, int], dict[str, list[int] | None]]:
    """识别贯穿整张九宫格的外圈亮色低纹理边框。"""
    flags = [is_uniform_light_line(metric) for metric in metrics]
    leading = 0
    while leading < len(flags) and flags[leading]:
        leading += 1
    trailing = 0
    while trailing < len(flags) and flags[len(flags) - trailing - 1]:
        trailing += 1
    max_margin = round(total * MAX_OUTER_MARGIN_FRACTION)
    if leading < min_gap or leading > max_margin:
        leading = 0
    if trailing < min_gap or trailing > max_margin:
        trailing = 0
    content_start = clamp(leading, 0, total - 1)
    content_end = clamp(total - trailing, content_start + 1, total)
    return (
        (content_start, content_end),
        {
            "leading": [0, content_start] if content_start else None,
            "trailing": [content_end, total] if content_end < total else None,
        },
    )


def scale_detected_bands(
    bands: list[tuple[int, int, float]],
    scale: float,
    total: int,
) -> list[tuple[int, int, float]]:
    output: list[tuple[int, int, float]] = []
    for start, end, confidence in bands:
        scaled_start = clamp(round(start / scale), 1, total - 1)
        scaled_end = clamp(round(end / scale), scaled_start + 1, total - 1)
        output.append((scaled_start, scaled_end, confidence))
    return output


def refine_detected_bands(
    metrics: list[tuple[float, float, float]],
    coarse_bands: list[list[int]],
    total: int,
) -> list[tuple[int, int, float]]:
    flags = [is_uniform_light_line(metric) for metric in metrics]
    runs = [
        (start, end)
        for start, end in contiguous_true_runs(flags)
        if end - start >= MIN_WHITE_GAP_PX
    ]
    refined: list[tuple[int, int, float]] = []
    search_radius = max(24, round(total * 0.035))
    for coarse_start, coarse_end in coarse_bands:
        coarse_center = (coarse_start + coarse_end) / 2
        candidates = [
            run
            for run in runs
            if abs((run[0] + run[1]) / 2 - coarse_center) <= search_radius
        ]
        if not candidates:
            return []
        start, end = min(
            candidates,
            key=lambda run: (
                abs((run[0] + run[1]) / 2 - coarse_center),
                abs((run[1] - run[0]) - (coarse_end - coarse_start)),
            ),
        )
        refined.append((start, end, uniform_band_confidence(metrics, start, end)))
    return sorted(refined, key=lambda item: item[0])


def bands_cross_connected(
    gray: Image.Image,
    bands_x: list[tuple[int, int, float]],
    bands_y: list[tuple[int, int, float]],
) -> bool:
    """确认两横两竖白带在四个交叉区域保持高亮、低纹理连通。"""
    for x_start, x_end, _ in bands_x:
        for y_start, y_end, _ in bands_y:
            region = gray.crop((x_start, y_start, x_end, y_end))
            stat = ImageStat.Stat(region)
            histogram = region.histogram()
            white_fraction = sum(histogram[UNIFORM_GAP_WHITE_THRESHOLD + 1 :]) / max(1, region.width * region.height)
            if (
                float(stat.mean[0]) < UNIFORM_GAP_MEAN_THRESHOLD
                or float(stat.var[0] if stat.var else 0.0) > UNIFORM_GAP_VARIANCE_THRESHOLD
                or white_fraction < UNIFORM_GAP_MIN_FRACTION
            ):
                return False
    return True


def dark_bands_cross_connected(
    gray: Image.Image,
    bands_x: list[tuple[int, int, float]],
    bands_y: list[tuple[int, int, float]],
) -> bool:
    for x_start, x_end, _ in bands_x:
        for y_start, y_end, _ in bands_y:
            region = gray.crop((x_start, y_start, x_end, y_end))
            values = list(region.get_flattened_data())
            dark_fraction = sum(value < DARK_GAP_THRESHOLD for value in values) / max(1, len(values))
            if dark_fraction < DARK_GAP_FRACTION:
                return False
    return True


def detect_white_grid_lines(gray: Image.Image, width: int, height: int, scale: float) -> dict[str, Any] | None:
    sample_w, sample_h = gray.size
    min_gap = max(3, round(MIN_WHITE_GAP_PX * scale))
    metrics_x = uniform_light_metrics(gray, "x")
    metrics_y = uniform_light_metrics(gray, "y")
    content_x, _ = detect_outer_content_range(metrics_x, sample_w, min_gap)
    content_y, _ = detect_outer_content_range(metrics_y, sample_h, min_gap)
    bands_x = find_uniform_gap_lines(
        metrics_x,
        sample_w,
        min_gap,
        max(4, round(sample_w * 0.05)),
        content_x,
    )
    bands_y = find_uniform_gap_lines(
        metrics_y,
        sample_h,
        min_gap,
        max(4, round(sample_h * 0.05)),
        content_y,
    )
    if len(bands_x) != 2 or len(bands_y) != 2 or not bands_cross_connected(gray, bands_x, bands_y):
        return None
    scaled_x = scale_detected_bands(bands_x, scale, width)
    scaled_y = scale_detected_bands(bands_y, scale, height)
    detected_x = [round((start + end) / 2) for start, end, _ in scaled_x]
    detected_y = [round((start + end) / 2) for start, end, _ in scaled_y]
    confidence = round(
        sum(score for _, _, score in [*scaled_x, *scaled_y]) / 4,
        3,
    )
    return {
        "detectedX": sorted(detected_x),
        "detectedY": sorted(detected_y),
        "detectedBandsX": [[start, end] for start, end, _ in scaled_x],
        "detectedBandsY": [[start, end] for start, end, _ in scaled_y],
        "contentRangeX": [round(content_x[0] / scale), round(content_x[1] / scale)],
        "contentRangeY": [round(content_y[0] / scale), round(content_y[1] / scale)],
        "confidence": confidence,
        "method": "uniform_gap_detection",
        "cropMode": "white_gutter",
        "warnings": [],
    }


def detect_dark_grid_lines(image: Image.Image) -> dict[str, Any] | None:
    """识别黑色或深色的九宫格分隔带，例如带白色虚线的黑框。"""
    width, height = image.size
    gray = ImageOps.grayscale(image)
    min_gap = max(3, MIN_WHITE_GAP_PX)
    metrics_x = uniform_dark_metrics(gray, "x")
    metrics_y = uniform_dark_metrics(gray, "y")
    content_x, outer_x = detect_outer_dark_content_range(metrics_x, width, min_gap)
    content_y, outer_y = detect_outer_dark_content_range(metrics_y, height, min_gap)
    bands_x = find_uniform_dark_gap_lines(
        metrics_x, width, min_gap, max(4, round(width * 0.05)), content_x
    )
    bands_y = find_uniform_dark_gap_lines(
        metrics_y, height, min_gap, max(4, round(height * 0.05)), content_y
    )
    if len(bands_x) != 2 or len(bands_y) != 2 or not dark_bands_cross_connected(gray, bands_x, bands_y):
        return None
    if any(end - start > round(width * MAX_DARK_GUTTER_FRACTION) for start, end, _ in bands_x):
        return None
    if any(end - start > round(height * MAX_DARK_GUTTER_FRACTION) for start, end, _ in bands_y):
        return None
    confidence = round(sum(score for _, _, score in [*bands_x, *bands_y]) / 4, 3)
    return {
        "detectedX": [round((start + end) / 2) for start, end, _ in bands_x],
        "detectedY": [round((start + end) / 2) for start, end, _ in bands_y],
        "detectedBandsX": [[start, end] for start, end, _ in bands_x],
        "detectedBandsY": [[start, end] for start, end, _ in bands_y],
        "contentRangeX": list(content_x),
        "contentRangeY": list(content_y),
        "outerBands": {
            "left": outer_x["leading"],
            "right": outer_x["trailing"],
            "top": outer_y["leading"],
            "bottom": outer_y["trailing"],
        },
        "confidence": confidence,
        "method": "dark_gutter_detection",
        "cropMode": "dark_gutter",
        "warnings": [],
    }


def refine_white_grid_detection(image: Image.Image, detection: dict[str, Any]) -> dict[str, Any] | None:
    width, height = image.size
    gray = ImageOps.grayscale(image)
    metrics_x = uniform_light_metrics(gray, "x")
    metrics_y = uniform_light_metrics(gray, "y")
    refined_x = refine_detected_bands(metrics_x, detection.get("detectedBandsX") or [], width)
    refined_y = refine_detected_bands(metrics_y, detection.get("detectedBandsY") or [], height)
    if len(refined_x) != 2 or len(refined_y) != 2 or not bands_cross_connected(gray, refined_x, refined_y):
        return None
    content_x, outer_x = detect_outer_content_range(metrics_x, width, MIN_WHITE_GAP_PX)
    content_y, outer_y = detect_outer_content_range(metrics_y, height, MIN_WHITE_GAP_PX)
    if (
        content_x[0] >= refined_x[0][0]
        or content_x[1] <= refined_x[1][1]
        or content_y[0] >= refined_y[0][0]
        or content_y[1] <= refined_y[1][1]
    ):
        return None
    panel_widths = [
        refined_x[0][0] - content_x[0],
        refined_x[1][0] - refined_x[0][1],
        content_x[1] - refined_x[1][1],
    ]
    panel_heights = [
        refined_y[0][0] - content_y[0],
        refined_y[1][0] - refined_y[0][1],
        content_y[1] - refined_y[1][1],
    ]
    if min(panel_widths + panel_heights) <= 0:
        return None
    confidence = round(sum(score for _, _, score in [*refined_x, *refined_y]) / 4, 3)
    return {
        "detectedX": [round((start + end) / 2) for start, end, _ in refined_x],
        "detectedY": [round((start + end) / 2) for start, end, _ in refined_y],
        "detectedBandsX": [[start, end] for start, end, _ in refined_x],
        "detectedBandsY": [[start, end] for start, end, _ in refined_y],
        "contentRangeX": list(content_x),
        "contentRangeY": list(content_y),
        "outerBands": {
            "left": outer_x["leading"],
            "right": outer_x["trailing"],
            "top": outer_y["leading"],
            "bottom": outer_y["trailing"],
        },
        "panelWidths": panel_widths,
        "panelHeights": panel_heights,
        "confidence": confidence,
        "method": "uniform_gap_detection_refined",
        "cropMode": "white_gutter",
        "warnings": [],
    }


def pick_line_near(scores: list[float], expected: float, search_radius: int, guard: int) -> tuple[int, float]:
    start = clamp(round(expected) - search_radius, guard, len(scores) - guard - 1)
    end = clamp(round(expected) + search_radius, guard + 1, len(scores) - guard)
    if end <= start:
        return clamp(round(expected), 1, len(scores) - 2), 0.0
    window = scores[start:end]
    local_index = max(range(len(window)), key=lambda idx: window[idx])
    peak_index = start + local_index
    peak = float(window[local_index])
    baseline = sorted(scores)[len(scores) // 2] if scores else 0.0
    confidence = max(0.0, min(1.0, (peak - baseline) / 80.0))
    return peak_index, confidence


def adaptive_axis_scores_pillow(image: Image.Image, axis: str) -> list[float]:
    """仅使用 Pillow 计算相邻像素行列的平均颜色差。"""
    rgb = image.convert("RGB")
    width, height = rgb.size
    if axis == "x":
        if width < 2:
            return []
        before = rgb.crop((0, 0, width - 1, height))
        after = rgb.crop((1, 0, width, height))
        difference = ImageChops.difference(after, before)
        projection = difference.resize((width - 1, 1), Image.Resampling.BOX)
    else:
        if height < 2:
            return []
        before = rgb.crop((0, 0, width, height - 1))
        after = rgb.crop((0, 1, width, height))
        difference = ImageChops.difference(after, before)
        projection = difference.resize((1, height - 1), Image.Resampling.BOX)
    pixels = projection.load()
    score_count = projection.width if axis == "x" else projection.height
    return [
        sum(pixels[index, 0] if axis == "x" else pixels[0, index]) / 3
        for index in range(score_count)
    ]


def adaptive_axis_scores(image: Image.Image, axis: str) -> list[float]:
    if np is None:
        return adaptive_axis_scores_pillow(image, axis)
    array = np.asarray(image.convert("RGB"), dtype=np.int16)
    if axis == "x":
        difference = np.abs(array[:, 1:, :] - array[:, :-1, :])
        return difference.mean(axis=(0, 2)).tolist()
    difference = np.abs(array[1:, :, :] - array[:-1, :, :])
    return difference.mean(axis=(1, 2)).tolist()


def pick_adaptive_boundary(scores: list[float], expected: float, cell_size: float) -> tuple[int, float, float] | None:
    if not scores:
        return None
    radius = max(4, round(cell_size * ADAPTIVE_SEAM_SEARCH_RATIO))
    start = clamp(round(expected) - radius, 1, len(scores) - 2)
    end = clamp(round(expected) + radius, start + 1, len(scores) - 1)
    window = scores[start:end]
    if not window:
        return None
    local_index = max(range(len(window)), key=lambda index: window[index])
    score_index = start + local_index
    peak = float(scores[score_index])
    ordered = sorted(float(value) for value in window)
    baseline = ordered[len(ordered) // 2]
    prominence = max(0.0, peak - baseline)
    if prominence < ADAPTIVE_SEAM_MIN_PROMINENCE:
        return None
    confidence = max(0.0, min(1.0, prominence / 40.0))
    return score_index + 1, confidence, prominence


def refine_adaptive_boundary(image: Image.Image, axis: str, candidate: int) -> int:
    if np is None:
        return candidate
    width, height = image.size
    radius = ADAPTIVE_SEAM_REFINE_RADIUS_PX
    if axis == "x":
        start = clamp(candidate - radius, 1, width - 2)
        end = clamp(candidate + radius, start + 2, width - 1)
        strip = image.crop((start - 1, 0, end + 1, height))
    else:
        start = clamp(candidate - radius, 1, height - 2)
        end = clamp(candidate + radius, start + 2, height - 1)
        strip = image.crop((0, start - 1, width, end + 1))
    scores = adaptive_axis_scores(strip, axis)
    if not scores:
        return candidate
    local_index = max(range(len(scores)), key=lambda index: scores[index])
    return start + local_index


def detect_adaptive_seams(image: Image.Image) -> dict[str, Any] | None:
    width, height = image.size
    scale = min(1.0, GRID_DETECTION_SAMPLE_MAX / max(width, height))
    sample = image.resize(
        (max(3, round(width * scale)), max(3, round(height * scale))),
        Image.Resampling.BILINEAR,
    )
    scores_x = adaptive_axis_scores(sample, "x")
    scores_y = adaptive_axis_scores(sample, "y")
    expected_x = [width / 3, width * 2 / 3]
    expected_y = [height / 3, height * 2 / 3]
    sample_expected_x = [sample.width / 3, sample.width * 2 / 3]
    sample_expected_y = [sample.height / 3, sample.height * 2 / 3]
    picked_x = [pick_adaptive_boundary(scores_x, value, sample.width / 3) for value in sample_expected_x]
    picked_y = [pick_adaptive_boundary(scores_y, value, sample.height / 3) for value in sample_expected_y]
    if not any([*picked_x, *picked_y]):
        return None

    detected_x: list[int] = []
    detected_y: list[int] = []
    confidences: list[float] = []
    prominences_x: list[float] = []
    prominences_y: list[float] = []
    for axis, picked, expected_values, source_size, output, prominences in (
        ("x", picked_x, expected_x, width, detected_x, prominences_x),
        ("y", picked_y, expected_y, height, detected_y, prominences_y),
    ):
        for item, expected in zip(picked, expected_values):
            if item is None:
                output.append(round(expected))
                prominences.append(0.0)
                continue
            sample_position, confidence, prominence = item
            mapped = round(sample_position / scale)
            output.append(refine_adaptive_boundary(image, axis, mapped))
            prominences.append(round(prominence, 3))
            confidences.append(confidence)

    offset_x = [actual - round(expected) for actual, expected in zip(detected_x, expected_x)]
    offset_y = [actual - round(expected) for actual, expected in zip(detected_y, expected_y)]
    offset_ratio_x = [abs(offset) / max(1.0, width / 3) for offset in offset_x]
    offset_ratio_y = [abs(offset) / max(1.0, height / 3) for offset in offset_y]
    max_offset_ratio = max([*offset_ratio_x, *offset_ratio_y], default=0.0)
    within_tolerance = max_offset_ratio <= ADAPTIVE_SEAM_MAX_OFFSET_RATIO
    warnings = ["adaptive_seam_detected"]
    if not within_tolerance:
        warnings.append("adaptive_seam_offset_exceeds_8_percent")
    return {
        "detectedX": detected_x,
        "detectedY": detected_y,
        "expectedX": [round(value) for value in expected_x],
        "expectedY": [round(value) for value in expected_y],
        "offsetPx": {"x": offset_x, "y": offset_y},
        "offsetRatio": {
            "x": [round(value, 4) for value in offset_ratio_x],
            "y": [round(value, 4) for value in offset_ratio_y],
        },
        "maxOffsetRatio": round(max_offset_ratio, 4),
        "maxOffsetPercent": round(max_offset_ratio * 100, 2),
        "boundaryToleranceRatio": ADAPTIVE_SEAM_MAX_OFFSET_RATIO,
        "withinTolerance": within_tolerance,
        "prominence": {"x": prominences_x, "y": prominences_y},
        "confidence": round(sum(confidences) / max(1, len(confidences)), 3),
        "method": "adaptive_seam_detection",
        "cropMode": "adaptive_seam",
        "warnings": warnings,
    }


def detect_grid_lines(image: Image.Image) -> dict[str, Any]:
    width, height = image.size
    scale = min(1.0, GRID_DETECTION_SAMPLE_MAX / max(width, height))
    sample_size = (max(1, round(width * scale)), max(1, round(height * scale)))
    gray = ImageOps.grayscale(image.resize(sample_size, Image.Resampling.BILINEAR))
    white_detection = detect_white_grid_lines(gray, width, height, scale)
    if white_detection:
        refined_detection = refine_white_grid_detection(image, white_detection)
        if refined_detection:
            return refined_detection
    dark_detection = detect_dark_grid_lines(image)
    if dark_detection:
        return dark_detection
    adaptive_detection = detect_adaptive_seams(image)
    if adaptive_detection:
        return adaptive_detection
    return {
        "detectedX": [round(width / 3), round(width * 2 / 3)],
        "detectedY": [round(height / 3), round(height * 2 / 3)],
        "confidence": 0.0,
        "method": "proportional_fallback",
        "cropMode": "proportional_fallback",
        "warnings": ["white_gutter_not_detected"],
    }


def line_boxes(
    width: int,
    height: int,
    detected_x: list[int],
    detected_y: list[int],
    detected_bands_x: list[list[int]] | None = None,
    detected_bands_y: list[list[int]] | None = None,
    content_range_x: list[int] | None = None,
    content_range_y: list[int] | None = None,
) -> list[tuple[int, int, int, int]]:
    if detected_bands_x and len(detected_bands_x) == 2 and detected_bands_y and len(detected_bands_y) == 2:
        content_x = content_range_x if content_range_x and len(content_range_x) == 2 else [0, width]
        content_y = content_range_y if content_range_y and len(content_range_y) == 2 else [0, height]
        x_edges = [
            clamp(content_x[0], 0, width - 1),
            clamp(detected_bands_x[0][0], 1, width - 1),
            clamp(detected_bands_x[0][1], 1, width - 1),
            clamp(detected_bands_x[1][0], 1, width - 1),
            clamp(detected_bands_x[1][1], 1, width - 1),
            clamp(content_x[1], 1, width),
        ]
        y_edges = [
            clamp(content_y[0], 0, height - 1),
            clamp(detected_bands_y[0][0], 1, height - 1),
            clamp(detected_bands_y[0][1], 1, height - 1),
            clamp(detected_bands_y[1][0], 1, height - 1),
            clamp(detected_bands_y[1][1], 1, height - 1),
            clamp(content_y[1], 1, height),
        ]
        boxes = []
        for row in range(3):
            for col in range(3):
                left_inset = GUTTER_SAFETY_INSET_PX if col > 0 else (OUTER_FRAME_SAFETY_INSET_PX if x_edges[0] > 0 else 0)
                top_inset = GUTTER_SAFETY_INSET_PX if row > 0 else (OUTER_FRAME_SAFETY_INSET_PX if y_edges[0] > 0 else 0)
                right_inset = GUTTER_SAFETY_INSET_PX if col < 2 else (OUTER_FRAME_SAFETY_INSET_PX if x_edges[-1] < width else 0)
                bottom_inset = GUTTER_SAFETY_INSET_PX if row < 2 else (OUTER_FRAME_SAFETY_INSET_PX if y_edges[-1] < height else 0)
                left = x_edges[col * 2] + left_inset
                top = y_edges[row * 2] + top_inset
                right = x_edges[col * 2 + 1] - right_inset
                bottom = y_edges[row * 2 + 1] - bottom_inset
                if right <= left or bottom <= top:
                    raise ValueError("九宫格分隔带内切后没有有效内容区")
                boxes.append((left, top, right, bottom))
        return boxes
    xs = [0, *detected_x, width]
    ys = [0, *detected_y, height]
    boxes = []
    for row in range(3):
        for col in range(3):
            left = xs[col]
            top = ys[row]
            right = xs[col + 1]
            bottom = ys[row + 1]
            boxes.append((clamp(left, 0, width - 1), clamp(top, 0, height - 1), clamp(right, 1, width), clamp(bottom, 1, height)))
    return boxes


def edge_stat(image: Image.Image, box: tuple[int, int, int, int]) -> tuple[float, float]:
    region = ImageOps.grayscale(image.crop(box))
    stat = ImageStat.Stat(region)
    return float(stat.mean[0]), float(stat.var[0] if stat.var else 0.0)


def should_trim_edge(mean: float, variance: float, inner_mean: float) -> bool:
    if mean < 38 and variance < 260:
        return True
    if mean > 232 and variance < 160:
        return True
    return abs(mean - inner_mean) > 45 and variance < 220


def scan_trim(image: Image.Image, side: str, max_trim: int) -> int:
    width, height = image.size
    if max_trim <= 0:
        return 0
    probe = max(2, min(8, round(min(width, height) * 0.008)))
    inner_margin = max(probe * 4, 18)
    inner_box = (
        min(width - 1, inner_margin),
        min(height - 1, inner_margin),
        max(inner_margin + 1, width - inner_margin),
        max(inner_margin + 1, height - inner_margin),
    )
    inner_mean, _ = edge_stat(image, inner_box)
    trim = 0
    for offset in range(0, max_trim, probe):
        if side == "left":
            box = (offset, 0, min(width, offset + probe), height)
        elif side == "right":
            box = (max(0, width - offset - probe), 0, width - offset, height)
        elif side == "top":
            box = (0, offset, width, min(height, offset + probe))
        else:
            box = (0, max(0, height - offset - probe), width, height - offset)
        mean, variance = edge_stat(image, box)
        if should_trim_edge(mean, variance, inner_mean):
            trim = offset + probe
            continue
        break
    return clamp(trim, 0, max_trim)


def trim_cell(image: Image.Image) -> tuple[Image.Image, list[int], dict[str, int], list[str]]:
    width, height = image.size
    trim_box = [0, 0, width, height]
    edge_trim = {"left": 0, "top": 0, "right": 0, "bottom": 0}
    return image, trim_box, edge_trim, []


def cover_resize(image: Image.Image, target_size: tuple[int, int]) -> Image.Image:
    target_w, target_h = target_size
    width, height = image.size
    scale = max(target_w / width, target_h / height)
    resized = image.resize((max(1, math.ceil(width * scale)), max(1, math.ceil(height * scale))), Image.Resampling.LANCZOS)
    left = max(0, (resized.width - target_w) // 2)
    top = max(0, (resized.height - target_h) // 2)
    return resized.crop((left, top, left + target_w, top + target_h))


def is_black_hairline(mean: float, variance: float) -> bool:
    return mean < 16 and variance < 180


def scan_final_hairline(image: Image.Image, side: str, max_trim: int = 6) -> int:
    width, height = image.size
    gray = ImageOps.grayscale(image)
    trim = 0
    for offset in range(max_trim):
        if side == "left":
            box = (offset, 0, offset + 1, height)
        elif side == "right":
            box = (width - offset - 1, 0, width - offset, height)
        elif side == "top":
            box = (0, offset, width, offset + 1)
        else:
            box = (0, height - offset - 1, width, height - offset)
        stat = ImageStat.Stat(gray.crop(box))
        if is_black_hairline(float(stat.mean[0]), float(stat.var[0] if stat.var else 0.0)):
            trim = offset + 1
            continue
        break
    return trim


def trim_final_hairlines(image: Image.Image, target_size: tuple[int, int]) -> tuple[Image.Image, dict[str, int]]:
    left = scan_final_hairline(image, "left")
    right = scan_final_hairline(image, "right")
    top = scan_final_hairline(image, "top")
    bottom = scan_final_hairline(image, "bottom")
    edge_trim = {"left": left, "top": top, "right": right, "bottom": bottom}
    if not any(edge_trim.values()):
        return image, edge_trim
    width, height = image.size
    crop_box = (left, top, max(left + 1, width - right), max(top + 1, height - bottom))
    return cover_resize(image.crop(crop_box), target_size), edge_trim


def edge_problem_ratio(image: Image.Image, mode: str) -> float:
    width, height = image.size
    band = max(4, round(min(width, height) * 0.025))
    gray = ImageOps.grayscale(image)
    problem_area = 0
    total_area = width * height
    probes = [
        (0, 0, width, band),
        (0, height - band, width, height),
        (0, 0, band, height),
        (width - band, 0, width, height),
    ]
    for box in probes:
        region = gray.crop(box)
        stat = ImageStat.Stat(region)
        mean = float(stat.mean[0])
        variance = float(stat.var[0] if stat.var else 0.0)
        if mode == "black":
            is_problem = mean < 18 and variance < 240
        else:
            is_problem = mean > 232 and variance < 220
        if is_problem:
            problem_area += max(0, box[2] - box[0]) * max(0, box[3] - box[1])
    return round(problem_area / max(1, total_area), 4)


def edge_problem_widths(image: Image.Image, mode: str, max_fraction: float = 0.18) -> dict[str, int]:
    """按四条边分别记录连续黑边或空白边宽度，仅用于审计。"""
    width, height = image.size
    gray = ImageOps.grayscale(image)
    probe = max(2, min(8, round(min(width, height) * 0.005)))
    max_trim = max(probe, round(min(width, height) * max_fraction))
    output: dict[str, int] = {}
    for side in ("left", "top", "right", "bottom"):
        detected = 0
        for offset in range(0, max_trim, probe):
            if side == "left":
                box = (offset, 0, min(width, offset + probe), height)
            elif side == "right":
                box = (max(0, width - offset - probe), 0, width - offset, height)
            elif side == "top":
                box = (0, offset, width, min(height, offset + probe))
            else:
                box = (0, max(0, height - offset - probe), width, height - offset)
            stat = ImageStat.Stat(gray.crop(box))
            mean = float(stat.mean[0])
            variance = float(stat.var[0] if stat.var else 0.0)
            is_problem = (mean < 18 and variance < 240) if mode == "black" else (mean > 232 and variance < 220)
            if not is_problem:
                break
            detected = min(max_trim, offset + probe)
        output[side] = detected
    return output


def is_cell_valid(
    raw_size: tuple[int, int],
    trim_box: list[int],
    black_ratio: float,
    blank_ratio: float,
    warnings: list[str],
    blank_edge_px: dict[str, int] | None = None,
) -> bool:
    raw_area = raw_size[0] * raw_size[1]
    trim_area = max(1, trim_box[2] - trim_box[0]) * max(1, trim_box[3] - trim_box[1])
    if trim_area < raw_area * 0.75:
        return False
    if black_ratio > 0.03 or blank_ratio >= 0.05:
        return False
    if blank_edge_px and max(blank_edge_px.values(), default=0) >= round(min(raw_size) * 0.04):
        return False
    return "trim_too_aggressive" not in warnings


def repeated_outer_residue_warnings(cells: list[dict[str, Any]], target_size: tuple[int, int]) -> list[str]:
    # filler 仅用于补足九宫格，不会进入后续视频交付。它们常被要求为低信息
    # 密度的环境画面，天然可能带有大面积高明度区域；若参与外边框判断，会把
    # filler 的视觉特征误判为真实场景的留白，从而拒绝整批真实画面。
    deliverable_cells = [cell for cell in cells if cell.get("kind") != "filler"]
    threshold = max(8, round(min(target_size) * 0.012))
    side_slots = {
        "left": {1, 4, 7},
        "top": {1, 2, 3},
        "right": {3, 6, 9},
        "bottom": {7, 8, 9},
    }
    warnings: list[str] = []
    for side, slots in side_slots.items():
        candidates = [cell for cell in deliverable_cells if int(cell.get("slot") or 0) in slots]
        residue_count = sum(
            1
            for cell in candidates
            if int(((cell.get("quality") or {}).get("blankEdgePx") or {}).get(side) or 0) >= threshold
        )
        if len(candidates) >= 2 and residue_count >= 2:
            warnings.append(f"outer_border_residue_{side}")
    return warnings


def slot_by_number(batch: dict[str, Any], slot: int) -> dict[str, Any]:
    for item in batch.get("slots") or []:
        if int(item.get("slot") or 0) == slot:
            return item
    return {"slot": slot, "kind": "scene", "sceneIndex": slot, "frameType": "main_visual", "prompt": ""}


def cell_image_path(cells_dir: Path, grid_id: str, slot: int, kind: str, scene_index: int | None) -> Path:
    if kind == "filler":
        return cells_dir / f"filler_{grid_id}_p{slot:02d}.png"
    if scene_index is None:
        raise ValueError(f"scene slot 缺少 sceneIndex: {grid_id} slot {slot}")
    return cells_dir / f"scene_{scene_index:03d}.png"


def process_grid_batch(
    grid_path: Path,
    batch: dict[str, Any],
    output_dir: Path,
    ratio: str,
    cells_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    cells_dir = cells_dir or output_dir / "cells"
    cells_dir.mkdir(parents=True, exist_ok=True)
    source = Image.open(grid_path).convert("RGB")
    source_size = source.size
    grid_detection = detect_grid_lines(source)
    requested_size = list(batch.get("requestedSourceSize") or grid_source_size(ratio))
    actual_size = list(source_size)
    size_contract_ok = requested_size == actual_size
    crop_mode = str(grid_detection["cropMode"])
    # 九宫格的正常合同是“隐形网格 + 满版画布”。检测到任意可见分隔带时，
    # 不再通过裁切掩盖错误结果，必须由调用方重新生图。
    if crop_mode in {"white_gutter", "dark_gutter"}:
        grid_detection.setdefault("warnings", []).append("visible_grid_separator_detected")
        if not size_contract_ok:
            grid_detection["warnings"].append("source_size_contract_mismatch")
        return [], {
            "gridId": str(batch.get("gridId") or grid_path.stem),
            "sourceImage": as_root_relative(grid_path),
            "sourceSize": actual_size,
            "requestedSourceSize": requested_size,
            "actualSourceSize": actual_size,
            "sourceSizeContractOk": size_contract_ok,
            "gridDetection": grid_detection,
            "cropMode": "rejected_visible_grid",
            "validCells": 0,
            "realSceneCount": int(batch.get("realSceneCount") or 0),
            "fillerCount": int(batch.get("fillerCount") or 0),
            "realValidCells": 0,
            "fillerValidCells": 0,
            "structureWarnings": ["visible_grid_separator_detected"],
            "deliveryReady": False,
            "status": "rejected_visible_grid",
        }
    if crop_mode == "adaptive_seam" and not bool(grid_detection.get("withinTolerance", True)):
        if not size_contract_ok:
            grid_detection.setdefault("warnings", []).append("source_size_contract_mismatch")
        return [], {
            "gridId": str(batch.get("gridId") or grid_path.stem),
            "sourceImage": as_root_relative(grid_path),
            "sourceSize": actual_size,
            "requestedSourceSize": requested_size,
            "actualSourceSize": actual_size,
            "sourceSizeContractOk": size_contract_ok,
            "gridDetection": grid_detection,
            "cropMode": "rejected_boundary_drift",
            "validCells": 0,
            "realSceneCount": int(batch.get("realSceneCount") or 0),
            "fillerCount": int(batch.get("fillerCount") or 0),
            "realValidCells": 0,
            "fillerValidCells": 0,
            "structureWarnings": ["adaptive_seam_offset_exceeds_8_percent"],
            "deliveryReady": False,
            "status": "rejected_boundary_drift",
        }
    # 无明显语义边界或边界正好落在理论三等分位置时，使用固定坐标完整分配像素。
    # 模型生成的语义边界若有可接受的小幅偏移，则沿真实边界裁切，避免固定三等分切入相邻场景；
    # 偏移超过容差的异常图已在上方拒绝并交给调用方重新生成。
    detection_mode = crop_mode
    if detection_mode == "adaptive_seam" and any(
        int(offset) != 0
        for offsets in (grid_detection.get("offsetPx") or {}).values()
        for offset in offsets
    ):
        boxes = line_boxes(
            source.width,
            source.height,
            grid_detection["detectedX"],
            grid_detection["detectedY"],
        )
        crop_mode = "adaptive_coordinates"
        geometry = {
            "columnWidths": [boxes[col][2] - boxes[col][0] for col in range(3)],
            "rowHeights": [boxes[row * 3][3] - boxes[row * 3][1] for row in range(3)],
            "fullCoverage": True,
        }
    else:
        boxes = fixed_coordinate_boxes(source.width, source.height)
        crop_mode = "fixed_coordinates"
        geometry = fixed_grid_geometry(source.width, source.height, boxes)
    grid_detection["detectedCropMode"] = detection_mode
    grid_detection["deliveryCropMode"] = crop_mode
    grid_detection["geometry"] = geometry
    if not size_contract_ok:
        grid_detection.setdefault("warnings", []).append("source_size_contract_mismatch")
    target_cell_size = batch.get("targetCellSize") or {}
    if target_cell_size.get("width") and target_cell_size.get("height"):
        target_size = (int(target_cell_size["width"]), int(target_cell_size["height"]))
    else:
        target_size = grid_cell_size(ratio)
    cells: list[dict[str, Any]] = []
    grid_id = str(batch.get("gridId") or grid_path.stem)
    for slot, box in enumerate(boxes, start=1):
        slot_plan = slot_by_number(batch, slot)
        kind = str(slot_plan.get("kind") or "scene").strip() or "scene"
        scene_index = None if kind == "filler" else int(slot_plan.get("sceneIndex") or slot)
        raw = source.crop(box)
        # 每格按已确认的几何边界取完整内容，再单张 cover 归整到目标尺寸。
        trim_box = [0, 0, raw.width, raw.height]
        edge_trim = {"left": 0, "top": 0, "right": 0, "bottom": 0}
        warnings: list[str] = []
        normalized = cover_resize(raw, target_size)
        final_edge_trim = {"left": 0, "top": 0, "right": 0, "bottom": 0}
        image_path = cell_image_path(cells_dir, grid_id, slot, kind, scene_index)
        normalized.save(image_path)
        black_ratio = edge_problem_ratio(normalized, "black")
        blank_ratio = edge_problem_ratio(normalized, "blank")
        black_edge_px = edge_problem_widths(normalized, "black")
        blank_edge_px = edge_problem_widths(normalized, "blank")
        cell_warnings = [*grid_detection.get("warnings", []), *warnings]
        if max(blank_edge_px.values(), default=0) >= round(min(normalized.size) * 0.04):
            cell_warnings.append("blank_edge_residue")
        valid = is_cell_valid(raw.size, trim_box, black_ratio, blank_ratio, cell_warnings, blank_edge_px)
        if not valid and not cell_warnings:
            cell_warnings.append("cell_quality_threshold_failed")
        absolute_trim_box = [box[0] + trim_box[0], box[1] + trim_box[1], box[0] + trim_box[2], box[1] + trim_box[3]]
        # 裁切质量只用于审计，不得阻断真实 scene 进入后续视频合成。
        assignment_status = "filler" if kind == "filler" else "assigned"
        cells.append(
            {
                "gridId": grid_id,
                "slot": slot,
                "kind": kind,
                "sceneIndex": scene_index,
                "frameId": str(slot_plan.get("frameId") or ""),
                "shotId": slot_plan.get("shotId") if kind == "filler" else (slot_plan.get("shotId") or scene_index),
                "frameType": str(slot_plan.get("frameType") or batch.get("mode") or "main_visual"),
                "prompt": str(slot_plan.get("prompt") or ""),
                "sourceImage": as_root_relative(grid_path),
                "sourceBox": list(box),
                "cropMode": crop_mode,
                "detectedGutters": {
                    "x": None,
                    "y": None,
                },
                "contentRange": {
                    "x": None,
                    "y": None,
                },
                "trimBox": absolute_trim_box,
                "image": as_root_relative(image_path),
                "targetSize": list(target_size),
                "normalizationMode": "cover",
                "quality": {
                    "gridLineDetected": crop_mode in {"white_gutter", "dark_gutter"},
                    "gridLineConfidence": grid_detection["confidence"],
                    "gridLineMethod": grid_detection["method"],
                    "edgeTrimPx": edge_trim,
                    "finalEdgeTrimPx": final_edge_trim,
                    "blackEdgeRatio": black_ratio,
                    "blankEdgeRatio": blank_ratio,
                    "blackEdgePx": black_edge_px,
                    "blankEdgePx": blank_edge_px,
                    "valid": valid,
                    "warnings": cell_warnings,
                },
                "assignment": {
                    "status": assignment_status,
                    "assignedSceneIndex": scene_index if kind != "filler" else None,
                },
            }
        )
    real_cells = [cell for cell in cells if cell.get("kind") != "filler"]
    filler_cells = [cell for cell in cells if cell.get("kind") == "filler"]
    real_valid_cells = sum(1 for cell in real_cells if cell["quality"]["valid"])
    filler_valid_cells = sum(1 for cell in filler_cells if cell["quality"]["valid"])
    real_scene_count = int(batch.get("realSceneCount") or len(real_cells))
    filler_count = int(batch.get("fillerCount") or len(filler_cells))
    structure_warnings = repeated_outer_residue_warnings(cells, target_size)
    delivery_ready = not structure_warnings
    batch_summary = {
        "gridId": grid_id,
        "sourceImage": as_root_relative(grid_path),
        "sourceSize": list(source_size),
        "requestedSourceSize": requested_size,
        "actualSourceSize": actual_size,
        "sourceSizeContractOk": size_contract_ok,
        "gridDetection": grid_detection,
        "cropMode": crop_mode,
        "validCells": sum(1 for cell in cells if cell["quality"]["valid"]),
        "realSceneCount": real_scene_count,
        "fillerCount": filler_count,
        "realValidCells": real_valid_cells,
        "fillerValidCells": filler_valid_cells,
        "structureWarnings": structure_warnings,
        "deliveryReady": delivery_ready,
        "status": "processed" if delivery_ready else "processed_with_warnings",
    }
    return cells, batch_summary


def make_contact_sheet(cells: list[dict[str, Any]], output_path: Path, root_dir: Path = ROOT_DIR) -> Path:
    if not cells:
        raise ValueError("没有可生成 contact sheet 的 cell")
    first_path = root_dir / cells[0]["image"] if not Path(str(cells[0]["image"])).is_absolute() else Path(str(cells[0]["image"]))
    with Image.open(first_path) as first:
        cell_w, cell_h = first.size
    thumb_w = min(360, cell_w)
    thumb_h = max(1, round(cell_h * thumb_w / cell_w))
    gap = 18
    label_h = 34
    sheet = Image.new("RGB", (thumb_w * 3 + gap * 4, (thumb_h + label_h) * 3 + gap * 4), (224, 224, 220))
    for offset, cell in enumerate(cells):
        cell_path = root_dir / cell["image"] if not Path(str(cell["image"])).is_absolute() else Path(str(cell["image"]))
        with Image.open(cell_path).convert("RGB") as image:
            thumb = ImageOps.contain(image, (thumb_w, thumb_h), Image.Resampling.LANCZOS)
        row = offset // 3
        col = offset % 3
        x = gap + col * (thumb_w + gap)
        y = gap + row * (thumb_h + label_h + gap)
        sheet.paste(thumb, (x, y))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path, quality=92)
    return output_path


def process_grid_plan(plan: dict[str, Any], output_dir: Path, ratio: str) -> dict[str, Any]:
    target_size = grid_cell_size(ratio)
    all_cells: list[dict[str, Any]] = []
    batch_summaries: list[dict[str, Any]] = []
    for batch in plan.get("batches") or []:
        source_value = str(batch.get("sourceImage") or "").strip()
        if not source_value:
            raise ValueError(f"grid batch 缺少 sourceImage: {batch.get('gridId')}")
        grid_path = Path(source_value)
        if not grid_path.is_absolute():
            grid_path = ROOT_DIR / grid_path
        cells, summary = process_grid_batch(grid_path, batch, output_dir, ratio)
        all_cells.extend(cells)
        batch_summaries.append(summary)
        batch["sourceImage"] = as_root_relative(grid_path)
        batch["sourceSize"] = summary["sourceSize"]
        batch["requestedSourceSize"] = summary["requestedSourceSize"]
        batch["actualSourceSize"] = summary["actualSourceSize"]
        batch["status"] = summary["status"]
        batch["gridDetection"] = summary["gridDetection"]
    contact_sheet = make_contact_sheet(all_cells, output_dir / "contact_sheet.jpg") if all_cells else None
    return {
        "version": 1,
        "ratio": ratio,
        "targetCellSize": {"width": target_size[0], "height": target_size[1]},
        "batches": batch_summaries,
        "contactSheet": as_root_relative(contact_sheet) if contact_sheet else "",
        "cells": all_cells,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="检测并切分九宫格图片，输出审计 manifest")
    parser.add_argument("--grid-image", help="单张九宫格图片；未提供 plan 时用它创建 1 个默认 batch")
    parser.add_argument("--plan", help="grid_batches.json")
    parser.add_argument("--output-dir", required=True, help="输出目录，通常为 task_dir/images")
    parser.add_argument("--ratio", required=True, choices=SUPPORTED_RATIOS)
    parser.add_argument("--manifest", help="输出 grid_cells_manifest.json；默认 output-dir/grid_cells_manifest.json")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    output_dir = Path(args.output_dir)
    plan_path = Path(args.plan) if args.plan else None
    grid_path = Path(args.grid_image) if args.grid_image else None
    if not plan_path and not grid_path:
        raise ValueError("必须提供 --plan 或 --grid-image")
    plan = load_plan(plan_path, args.ratio, grid_path or Path("grid.jpg"))
    if grid_path:
        for batch in plan.get("batches") or []:
            if not str(batch.get("sourceImage") or "").strip():
                batch["sourceImage"] = as_root_relative(grid_path)
    if plan_path:
        write_plan(plan_path, plan)
    manifest = process_grid_plan(plan, output_dir, args.ratio)
    manifest_path = Path(args.manifest) if args.manifest else output_dir / "grid_cells_manifest.json"
    write_json(manifest_path, manifest)
    print(json.dumps({"grid_cells_manifest": str(manifest_path.resolve()), "cells": len(manifest["cells"])}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - CLI 边界需要统一转换为中文错误信息。
        print(f"[错误] {exc}", file=sys.stderr)
        sys.exit(1)

