from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]
ToolResolver = Callable[[str], str]


def choose_video_url(profile: dict) -> str:
    feed = ((profile.get("data") or {}).get("feedInfo") or {})
    candidates = [
        ((feed.get("h264VideoInfo") or {}).get("videoUrl")),
        ((feed.get("h265VideoInfo") or {}).get("videoUrl")),
        feed.get("videoUrl"),
    ]
    for value in candidates:
        text = str(value or "").strip()
        if text:
            return text.replace("http://", "https://")
    raise RuntimeError("解析结果中没有可下载的视频地址。")


def platform_referer(platform: str) -> str:
    if platform == "douyin":
        return "https://www.douyin.com/"
    return "https://channels.weixin.qq.com/"


def download_video(url: str, output: Path, *, referer: str, timeout: int = 180) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp_output = output.with_name(f"{output.name}.tmp")
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": referer,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if int(status) != 200:
                raise RuntimeError(f"视频下载 HTTP {status}")
            with tmp_output.open("wb") as file:
                shutil.copyfileobj(response, file)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"视频下载 HTTP {exc.code}: {detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"视频下载网络错误: {exc.reason}") from exc
    if not tmp_output.exists() or tmp_output.stat().st_size <= 0:
        raise RuntimeError("视频下载结果为空。")
    last_error: OSError | None = None
    for _ in range(6):
        try:
            tmp_output.replace(output)
            return
        except OSError as exc:
            if getattr(exc, "winerror", None) != 5:
                raise
            last_error = exc
            time.sleep(0.5)

    try:
        shutil.copyfile(tmp_output, output)
        if output.exists() and output.stat().st_size > 0:
            try:
                tmp_output.unlink()
            except OSError:
                pass
            return
    except OSError:
        pass

    if last_error:
        raise last_error


def resolve_tool(name: str) -> str:
    env_name = f"{name.upper()}_PATH"
    configured = os.environ.get(env_name, "").strip()
    if configured:
        configured_path = Path(configured)
        if configured_path.exists():
            return str(configured_path)
        raise RuntimeError(f"{env_name} 指向的可执行文件不存在: {configured}")

    found = shutil.which(name)
    if found:
        return found
    raise RuntimeError(f"找不到 {name}，请把它加入 PATH，或设置 {env_name}。")


def run_command(
    args: list[str],
    *,
    runner: ProcessRunner = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    """使用 UTF-8 执行媒体命令，并把失败转换为统一异常。"""

    try:
        result = runner(
            args,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except (FileNotFoundError, OSError) as exc:
        raise RuntimeError(f"找不到命令: {args[0]}") from exc
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or f"{args[0]} exited with {result.returncode}").strip())
    return result


def probe_media(
    media_path: Path,
    *,
    runner: ProcessRunner = subprocess.run,
    tool_resolver: ToolResolver = resolve_tool,
) -> dict[str, Any]:
    """使用 ffprobe 验证媒体至少包含一个可解析的流。"""

    if not media_path.is_file():
        raise RuntimeError(f"输入媒体不存在: {media_path}")
    result = run_command(
        [
            tool_resolver("ffprobe"),
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(media_path),
        ],
        runner=runner,
    )
    try:
        metadata = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("无法解析 ffprobe 输出") from exc
    streams = metadata.get("streams") if isinstance(metadata, dict) else None
    if not isinstance(streams, list):
        raise TypeError("ffprobe 输出缺少媒体流")
    if not streams:
        raise ValueError("ffprobe 未检测到媒体流")
    return metadata


def extract_audio(video_path: Path, audio_path: Path, sample_rate: int = 16000) -> None:
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = resolve_tool("ffmpeg")
    run_command(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            str(audio_path),
        ]
    )
    if not audio_path.exists() or audio_path.stat().st_size <= 0:
        raise RuntimeError("音频提取结果为空。")
