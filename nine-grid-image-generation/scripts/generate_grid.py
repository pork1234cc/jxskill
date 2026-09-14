#!/usr/bin/env python3
"""调用 API 易生成九宫格总图，并完成结构检测与九格裁切。"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import shutil
import sys
import time
from contextlib import suppress
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from grid_processor import make_contact_sheet, process_grid_batch
from PIL import Image, UnidentifiedImageError

with suppress(AttributeError, OSError, ValueError):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


API_BASE_URL = "https://ai.apii.cn"
API_ENDPOINT = f"{API_BASE_URL}/v1/images/generations"
DEFAULT_MODEL = "gpt-image-2.0-4k"
DEFAULT_STYLE = "自然、统一、清晰的电影感画面"
API_KEY_ENV = "APII_API_KEY"
HTTP_TIMEOUT_SECONDS = 600
MAX_HTTP_ATTEMPTS = 3
MAX_STRUCTURE_ATTEMPTS = 2
SKILL_ROOT = Path(__file__).resolve().parents[1]

REQUEST_SIZES: dict[str, tuple[int, int]] = {
    "1:1": (2880, 2880),
    "3:4": (2448, 3264),
    "4:3": (3264, 2448),
    "9:16": (2160, 3840),
    "16:9": (3840, 2160),
}

RATIO_LABELS = {
    "1:1": "1:1 正方形",
    "3:4": "3:4 竖版",
    "4:3": "4:3 横版",
    "9:16": "9:16 竖版",
    "16:9": "16:9 横版",
}

POSITION_NAMES = (
    "左上",
    "上中",
    "右上",
    "左中",
    "正中",
    "右中",
    "左下",
    "下中",
    "右下",
)


class ApiRequestError(RuntimeError):
    """API 请求或图片下载失败。"""


def read_json(path: Path) -> dict[str, Any]:
    """读取 UTF-8 JSON 对象。"""
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise TypeError("输入 JSON 顶层必须是对象")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """以 UTF-8 写入格式化 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def absolute_path(path: Path) -> str:
    """返回适合写入清单的绝对路径。"""
    return str(path.resolve())


def validate_plan(payload: dict[str, Any]) -> dict[str, Any]:
    """验证并规范化九宫格标准输入。"""
    title = str(payload.get("title") or "").strip()
    if not title:
        raise ValueError("title 不能为空")
    ratio = str(payload.get("ratio") or "").strip()
    if ratio not in REQUEST_SIZES:
        raise ValueError(f"ratio 只支持：{', '.join(REQUEST_SIZES)}")
    raw_scenes = payload.get("scenes")
    if not isinstance(raw_scenes, list) or len(raw_scenes) != 9:
        raise ValueError("scenes 必须恰好包含 9 个场景")

    scenes: list[dict[str, Any]] = []
    for raw_scene in raw_scenes:
        if not isinstance(raw_scene, dict):
            raise TypeError("scenes 中每一项都必须是对象")
        slot = raw_scene.get("slot")
        if type(slot) is not int or not 1 <= slot <= 9:
            raise ValueError("每个 scene.slot 必须是 1 到 9 的整数")
        prompt = str(raw_scene.get("prompt") or "").strip()
        if not prompt:
            raise ValueError(f"第 {slot} 格 prompt 不能为空")
        scenes.append({"slot": slot, "prompt": prompt})
    scenes.sort(key=lambda item: item["slot"])
    if [item["slot"] for item in scenes] != list(range(1, 10)):
        raise ValueError("scene.slot 必须完整覆盖 1 到 9，且不得重复")
    return {
        "title": title,
        "ratio": ratio,
        "style": str(payload.get("style") or DEFAULT_STYLE).strip() or DEFAULT_STYLE,
        "global_requirements": str(payload.get("global_requirements") or "").strip(),
        "scenes": scenes,
    }


def build_grid_prompt(plan: dict[str, Any]) -> str:
    """构造隐形 3×3 满画布九宫格提示词。"""
    ratio = plan["ratio"]
    ratio_label = RATIO_LABELS[ratio]
    lines = [
        f"生成一张{ratio_label}的九场景分镜总图，严格由 3 列×3 行共 9 个独立场景组成。",
        "3×3 网格仅用于构图定位，边界必须隐形、横平竖直且严格对齐。九格视觉尺寸一致并无缝紧贴，整张画布的每一个像素都属于某个场景。",
        "不得出现任何白线、黑线、彩色线、分隔带、缝隙、留白、外边距、卡片边框、相框、圆角、阴影边框、照片墙或漫画分镜线。",
        f"每一个格子内部都必须是完整的{ratio_label}静态画面。背景、环境、材质与纹理铺满本格四边，在隐形边界处自然截断。",
        "主体和关键物体保持安全边距，不得跨格；每格只表现对应场景，不得在单格内部再次生成拼贴、多宫格或多个时间阶段。",
        f"统一视觉风格：{plan['style']}。",
    ]
    if plan["global_requirements"]:
        lines.append(f"全局统一要求：{plan['global_requirements']}。")
    for scene in plan["scenes"]:
        slot = int(scene["slot"])
        lines.append(f"第 {slot} 格（{POSITION_NAMES[slot - 1]}）：{scene['prompt']}。")
    lines.extend(
        [
            "九格保持统一的媒介、色彩、光线、材质、镜头语言和清晰度，同时保证九个场景可明确区分。",
            "strict 3x3 grid, exactly 9 independent edge-to-edge panels, invisible aligned boundaries, zero gutters, zero gaps, zero margins, zero borders, no cross-panel content.",
        ]
    )
    return "\n".join(lines)


def build_request_body(plan: dict[str, Any], prompt: str) -> dict[str, Any]:
    """构造 API 易图像生成请求体。"""
    return {
        "model": DEFAULT_MODEL,
        "prompt": prompt,
        "aspect_ratio": plan["ratio"],
        "quality": "high",
        "output_format": "png",
        "response_format": "url",
    }


def request_snapshot(body: dict[str, Any]) -> dict[str, Any]:
    """生成不包含鉴权信息的请求快照。"""
    return {
        "method": "POST",
        "url": API_ENDPOINT,
        "headers": {"Authorization": "Bearer <redacted>", "Content-Type": "application/json"},
        "body": body,
    }


def retry_delay(headers: Any, attempt: int) -> float:
    """读取 Retry-After，缺失时采用短暂递增退避。"""
    value = str(headers.get("Retry-After") or "").strip() if headers else ""
    try:
        return min(60.0, max(0.0, float(value))) if value else float(min(8, 2**attempt))
    except ValueError:
        return float(min(8, 2**attempt))


def error_detail(exc: HTTPError) -> str:
    """读取有限长度的服务端错误信息。"""
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except OSError:
        body = ""
    return body[:1000].strip()


def post_generation_request(body: dict[str, Any], api_key: str) -> dict[str, Any]:
    """发送生图请求，并对限流和临时服务错误做有限重试。"""
    encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    for attempt in range(1, MAX_HTTP_ATTEMPTS + 1):
        request = Request(
            API_ENDPOINT,
            data=encoded,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise ApiRequestError("API 返回的 JSON 顶层不是对象")
            return payload
        except HTTPError as exc:
            detail = error_detail(exc)
            if exc.code in {429, 500, 502, 503, 504} and attempt < MAX_HTTP_ATTEMPTS:
                time.sleep(retry_delay(exc.headers, attempt))
                continue
            suffix = f"：{detail}" if detail else ""
            raise ApiRequestError(f"API 请求失败，HTTP {exc.code}{suffix}") from exc
        except URLError as exc:
            if attempt < MAX_HTTP_ATTEMPTS:
                time.sleep(float(min(8, 2**attempt)))
                continue
            raise ApiRequestError(f"API 网络请求失败：{exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise ApiRequestError("API 返回内容不是有效 JSON") from exc
    raise ApiRequestError("API 请求重试次数已用尽")


def download_url(url: str) -> bytes:
    """下载生成图片。"""
    if url.startswith("data:") and ";base64," in url:
        return base64.b64decode(url.split(",", 1)[1], validate=True)
    request = Request(url, headers={"User-Agent": "nine-grid-image-generation/1.0"})
    try:
        with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return response.read()
    except (HTTPError, URLError) as exc:
        raise ApiRequestError(f"生成图片下载失败：{exc}") from exc


def extract_image_bytes(payload: dict[str, Any]) -> tuple[bytes, str]:
    """兼容 URL 与 base64 两种常见图像响应。"""
    data = payload.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise ApiRequestError("API 响应缺少 data[0]")
    item = data[0]
    encoded = str(item.get("b64_json") or "").strip()
    url = str(item.get("url") or "").strip()
    if not encoded and not url:
        raise ApiRequestError("API 响应缺少图片 URL 或 b64_json")
    try:
        content = base64.b64decode(encoded, validate=True) if encoded else download_url(url)
    except (ValueError, binascii.Error) as exc:
        raise ApiRequestError("API 返回的 base64 图片无效") from exc
    if not content:
        raise ApiRequestError("API 响应没有可用的图片内容")
    request_id = str(payload.get("id") or payload.get("request_id") or "")
    return content, request_id


def save_png(content: bytes, output_path: Path) -> None:
    """验证图片字节并统一保存为 RGB PNG。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(BytesIO(content)) as source:
            source.load()
            image = source.convert("RGB")
        image.save(output_path, format="PNG")
    except (UnidentifiedImageError, OSError) as exc:
        raise ApiRequestError("API 返回内容不是可识别的图片") from exc


def build_batch(plan: dict[str, Any]) -> dict[str, Any]:
    """构造裁切器需要的九格批次合同。"""
    source_width, source_height = REQUEST_SIZES[plan["ratio"]]
    slots = [
        {
            "slot": scene["slot"],
            "kind": "scene",
            "sceneIndex": scene["slot"],
            "frameId": f"scene-{scene['slot']}",
            "shotId": scene["slot"],
            "prompt": scene["prompt"],
        }
        for scene in plan["scenes"]
    ]
    return {
        "gridId": "grid_001_009",
        "rows": 3,
        "cols": 3,
        "mode": "main_visual",
        "requestedSourceSize": [source_width, source_height],
        "targetCellSize": {"width": source_width // 3, "height": source_height // 3},
        "realSceneCount": 9,
        "fillerCount": 0,
        "slots": slots,
    }


def initial_manifest(plan: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """创建生成过程的基础审计清单。"""
    return {
        "version": 1,
        "status": "planned",
        "title": plan["title"],
        "ratio": plan["ratio"],
        "model": DEFAULT_MODEL,
        "apiBaseUrl": API_BASE_URL,
        "input": plan,
        "request": request_snapshot(body),
        "apiCalls": 0,
        "attempts": [],
        "cells": [],
    }


def run_structure_attempt(
    plan: dict[str, Any], body: dict[str, Any], api_key: str, work_dir: Path, attempt: int
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """执行一次 API 生图及九宫格结构处理。"""
    response = post_generation_request(body, api_key)
    content, request_id = extract_image_bytes(response)
    attempt_dir = work_dir / f"attempt_{attempt}"
    grid_path = attempt_dir / f"grid_attempt_{attempt}.png"
    save_png(content, grid_path)
    cells, summary = process_grid_batch(grid_path, build_batch(plan), attempt_dir, plan["ratio"])
    audit = {
        "attempt": attempt,
        "requestId": request_id,
        "sourceImage": absolute_path(grid_path),
        "status": summary["status"],
        "cropMode": summary["cropMode"],
        "structureWarnings": summary.get("structureWarnings") or [],
        "summary": summary,
    }
    return cells, summary, audit


def remove_standard_outputs(output_dir: Path, keep_debug_artifacts: bool, contact_sheet: bool) -> None:
    """清理本工具拥有的旧版交付文件，避免失败任务混入上次结果。"""
    for slot in range(1, 10):
        (output_dir / f"scene_{slot:03d}.png").unlink(missing_ok=True)
    if not contact_sheet:
        (output_dir / "contact_sheet.jpg").unlink(missing_ok=True)
    if not keep_debug_artifacts:
        for attempt in range(1, MAX_STRUCTURE_ATTEMPTS + 1):
            (output_dir / f"grid_attempt_{attempt}.png").unlink(missing_ok=True)


def persisted_attempt(audit: dict[str, Any], output_dir: Path, keep_debug_artifacts: bool) -> dict[str, Any]:
    """移除临时路径；调试模式下把总图复制到正式输出目录。"""
    persisted = {key: value for key, value in audit.items() if key not in {"sourceImage", "summary"}}
    summary = {key: value for key, value in audit["summary"].items() if key != "sourceImage"}
    persisted["summary"] = summary
    if keep_debug_artifacts:
        source_path = Path(str(audit["sourceImage"]))
        target_path = output_dir / f"grid_attempt_{audit['attempt']}.png"
        shutil.copy2(source_path, target_path)
        persisted["sourceImage"] = target_path.name
    return persisted


def source_cell_path(cell: dict[str, Any]) -> Path:
    """解析裁切器返回的单图路径。"""
    path = Path(str(cell.get("image") or ""))
    return path if path.is_absolute() else SKILL_ROOT / path


def deliver_cells(cells: list[dict[str, Any]], output_dir: Path) -> list[dict[str, Any]]:
    """校验九张裁片并平铺到输出目录，返回精简后的审计记录。"""
    ordered = sorted(cells, key=lambda item: int(item.get("slot") or 0))
    if [int(cell.get("slot") or 0) for cell in ordered] != list(range(1, 10)):
        raise RuntimeError("结构检测通过后未得到完整的 9 张裁片")
    sources = [source_cell_path(cell) for cell in ordered]
    if not all(path.is_file() for path in sources):
        raise RuntimeError("结构检测通过后存在缺失裁片")

    delivered: list[dict[str, Any]] = []
    for cell, source_path in zip(ordered, sources, strict=True):
        slot = int(cell["slot"])
        target_path = output_dir / f"scene_{slot:03d}.png"
        source_path.replace(target_path)
        quality = cell.get("quality") or {}
        delivered.append(
            {
                "slot": slot,
                "sceneIndex": cell.get("sceneIndex"),
                "prompt": str(cell.get("prompt") or ""),
                "image": target_path.name,
                "size": list(cell.get("targetSize") or []),
                "sourceBox": list(cell.get("sourceBox") or []),
                "cropMode": str(cell.get("cropMode") or ""),
                "quality": {
                    "valid": bool(quality.get("valid")),
                    "warnings": list(quality.get("warnings") or []),
                    "blackEdgeRatio": quality.get("blackEdgeRatio"),
                    "blankEdgeRatio": quality.get("blankEdgeRatio"),
                },
            }
        )
    return delivered


def complete_delivery(
    manifest: dict[str, Any],
    cells: list[dict[str, Any]],
    summary: dict[str, Any],
    output_dir: Path,
    contact_sheet: bool,
) -> dict[str, Any]:
    """提交九张裁片和可选联系表，并写入成功 manifest。"""
    delivered = deliver_cells(cells, output_dir)
    manifest.update({"status": "processed", "cropMode": summary["cropMode"], "cells": delivered})
    if contact_sheet:
        sheet_path = make_contact_sheet(delivered, output_dir / "contact_sheet.jpg", root_dir=output_dir)
        manifest["contactSheet"] = sheet_path.name
    write_json(output_dir / "manifest.json", manifest)
    return manifest


def run_live_generation(
    plan: dict[str, Any],
    body: dict[str, Any],
    api_key: str,
    output_dir: Path,
    manifest: dict[str, Any],
    keep_debug_artifacts: bool,
    contact_sheet: bool,
) -> dict[str, Any]:
    """在临时目录执行 API 生成、结构重试和最终交付。"""
    manifest_path = output_dir / "manifest.json"
    with TemporaryDirectory(prefix="nine-grid-work-", dir=output_dir.parent) as temp_name:
        for attempt in range(1, MAX_STRUCTURE_ATTEMPTS + 1):
            try:
                cells, summary, audit = run_structure_attempt(plan, body, api_key, Path(temp_name), attempt)
            except ApiRequestError as exc:
                manifest["status"] = "api_error"
                manifest["attempts"].append({"attempt": attempt, "status": "api_error", "error": str(exc)})
                write_json(manifest_path, manifest)
                raise
            manifest["apiCalls"] += 1
            manifest["attempts"].append(persisted_attempt(audit, output_dir, keep_debug_artifacts))
            if summary["status"] == "processed":
                return complete_delivery(manifest, cells, summary, output_dir, contact_sheet)
            manifest["status"] = summary["status"]
            write_json(manifest_path, manifest)

    manifest["status"] = str(manifest["attempts"][-1]["status"] or "rejected")
    write_json(manifest_path, manifest)
    raise RuntimeError(f"九宫格结构连续检测失败，详见：{manifest_path.resolve()}")


def run_generation(
    input_path: Path,
    output_dir: Path,
    dry_run: bool,
    keep_debug_artifacts: bool = False,
    contact_sheet: bool = False,
) -> dict[str, Any]:
    """执行 dry-run 或完整九宫格生成流程。"""
    plan = validate_plan(read_json(input_path))
    output_dir.mkdir(parents=True, exist_ok=True)
    prompt = build_grid_prompt(plan)
    body = build_request_body(plan, prompt)
    manifest = initial_manifest(plan, body)
    manifest_path = output_dir / "manifest.json"
    if dry_run:
        manifest["status"] = "dry_run"
        write_json(manifest_path, manifest)
        return manifest

    api_key = str(os.environ.get(API_KEY_ENV) or "").strip()
    if not api_key:
        raise ValueError(f"缺少环境变量 {API_KEY_ENV}，无法调用生图 API")
    remove_standard_outputs(output_dir, keep_debug_artifacts, contact_sheet)
    return run_live_generation(
        plan,
        body,
        api_key,
        output_dir,
        manifest,
        keep_debug_artifacts,
        contact_sheet,
    )


def build_parser() -> argparse.ArgumentParser:
    """创建命令行参数解析器。"""
    parser = argparse.ArgumentParser(description="通过 API 易生成并切分九宫格场景图")
    parser.add_argument("--input", required=True, help="九宫格标准输入 JSON")
    parser.add_argument("--output-dir", required=True, help="输出目录")
    parser.add_argument("--dry-run", action="store_true", help="只验证输入并把脱敏请求写入 manifest，不访问网络")
    parser.add_argument("--keep-debug-artifacts", action="store_true", help="保留每次 API 返回的九宫格总图")
    parser.add_argument("--contact-sheet", action="store_true", help="额外生成九张裁片的联系表")
    return parser


def main() -> None:
    """命令行入口。"""
    args = build_parser().parse_args()
    manifest = run_generation(
        Path(args.input),
        Path(args.output_dir),
        bool(args.dry_run),
        keep_debug_artifacts=bool(args.keep_debug_artifacts),
        contact_sheet=bool(args.contact_sheet),
    )
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "manifest": absolute_path(Path(args.output_dir) / "manifest.json"),
                "cells": len(manifest.get("cells") or []),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - CLI 边界需要统一转换为中文错误信息。
        print(f"[错误] {exc}", file=sys.stderr)
        sys.exit(1)


