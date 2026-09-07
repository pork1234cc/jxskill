from __future__ import annotations

import shutil
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .case_store import build_reference_record, sync_collected_case
from .markdown import (
    render_markdown,
    safe_filename,
    timestamp_now,
)
from .media import (
    choose_video_url,
    download_video,
    extract_audio,
    platform_referer,
    probe_media,
)
from .profile_data import feed_info
from .project import temporary_root
from .transcription import transcribe_audio
from .wechat_decrypt import WechatDecryptError, decrypt_wechat_media


def wechat_decrypt_info(profile: dict[str, Any]) -> tuple[str, str]:
    """读取主详情响应归一化后保留的解密密钥与方案。"""

    decrypt_info = profile.get("_wechatDecrypt")
    if not isinstance(decrypt_info, dict):
        raise WechatDecryptError("视频号详情缺少本地解密信息。")
    decode_key = str(decrypt_info.get("decodeKey") or "").strip()
    scheme = str(decrypt_info.get("scheme") or "").strip()
    if not decode_key or not scheme:
        raise RuntimeError("视频号详情缺少有效 decode_key 或解密方案。")
    return decode_key, scheme


def cleanup_directory(path: Path, label: str) -> str:
    last_error: OSError | None = None
    for _ in range(3):
        try:
            shutil.rmtree(path)
            return ""
        except OSError as exc:
            last_error = exc
            time.sleep(0.3)
    return f"{label}清理失败: {last_error}" if last_error else f"{label}清理失败"


def output_stem(profile: dict[str, Any], index: int = 0, book_name: str = "") -> str:
    prefix = time.strftime("%Y%m%d_%H%M%S")
    suffix = f"_{index:02d}" if index else ""
    if book_name.strip():
        book_stem = safe_filename(book_name, "duibiao", limit=50)
        return f"{book_stem}_{prefix}{suffix}"

    feed = feed_info(profile)
    title = str(feed.get("title") or feed.get("description") or "").strip()
    object_id = str(feed.get("objectId") or "").strip()
    fallback_stem = safe_filename(title or object_id, "duibiao", limit=50)
    return f"{fallback_stem}_{prefix}{suffix}"


def prepare_transcription_video(profile: dict[str, Any], work_dir: Path) -> Path:
    """下载并按平台解密视频，返回已通过媒体校验的本地路径。

    参数：
        profile: 已归一化且包含媒体地址的平台作品详情。
        work_dir: 本次采集独占的临时目录。
    返回：
        可直接交给 FFmpeg 提取音频的视频路径。
    异常：
        RuntimeError: 下载结果、解密参数或媒体结构无效。
    """

    platform = str(profile.get("platform") or "")
    source_video_path = work_dir / "source.mp4"
    decoded_video_path = work_dir / "decoded.mp4"
    video_url = choose_video_url(profile)
    download_video(video_url, source_video_path, referer=platform_referer(platform))
    if platform == "wechat_channels":
        decode_key, decrypt_scheme = wechat_decrypt_info(profile)
        decrypt_wechat_media(
            decode_key,
            source_video_path,
            decoded_video_path,
            scheme=decrypt_scheme,
        )
        transcription_video_path = decoded_video_path
    else:
        transcription_video_path = source_video_path
    probe_media(transcription_video_path)
    return transcription_video_path


def collect_profile(
    profile: dict[str, Any],
    source_url: str,
    *,
    output_dir: Path | str = Path("3-对标案例"),
    markdown_dir: Path | str | None = None,
    index: int = 0,
    book_name: str = "",
    filename_stem: str = "",
    sync_to_feishu: bool = False,
    markdown_renderer: Callable[..., str] | None = None,
) -> dict[str, Any]:
    """复用已归一化作品详情完成媒体处理、ASR 与 Markdown 落盘。

    参数：
        profile: 详情接口或账号作品列表归一化后的作品数据。
        source_url: 用于记录来源的原始作品链接或稳定平台地址。
        output_dir: 对标案例根目录。
        markdown_dir: 可选的 Markdown 直接落盘目录；为空时使用输出根目录下的“文案”。
        index: 手动批量采集时用于避免同秒文件名冲突的序号。
        book_name: 用户显式提供的关联书名。
        filename_stem: 可选的稳定文件名；监控采集应传入案例 ID。
        sync_to_feishu: 是否显式启用保留的飞书同步能力，默认关闭。
        markdown_renderer: 可选的 Markdown 渲染函数，监控场景可补充平台元数据。
    返回：
        包含 Markdown 路径、案例 ID、同步结果和清理警告的字典。
    异常：
        RuntimeError: 媒体处理、ASR、落盘或飞书同步失败。
    """

    output_root = Path(output_dir)
    target_markdown_dir = Path(markdown_dir) if markdown_dir is not None else output_root / "文案"
    collected_at = timestamp_now()
    cleanup_warnings: list[str] = []
    platform = str(profile.get("platform") or "")
    stem = safe_filename(filename_stem, "duibiao") if filename_stem else output_stem(
        profile,
        index=index,
        book_name=book_name,
    )
    work_dir = Path(
        tempfile.mkdtemp(
            prefix="article_monitor_",
            dir=temporary_root(),
        )
    )
    md_path = target_markdown_dir / f"{stem}.md"

    try:
        target_markdown_dir.mkdir(parents=True, exist_ok=True)
        audio_path = work_dir / "audio.wav"
        transcription_video_path = prepare_transcription_video(profile, work_dir)
        extract_audio(transcription_video_path, audio_path)
        transcript = transcribe_audio(audio_path)
        reference_record = build_reference_record(
            profile=profile,
            source_url=source_url,
            collected_at=collected_at,
            book_name=book_name,
            transcript=transcript.text,
            segments=transcript.segments,
            duration=transcript.duration,
        )

        renderer = markdown_renderer or render_markdown
        markdown = renderer(
            profile=profile,
            source_url=source_url,
            collected_at=collected_at,
            case_id=reference_record.case_id,
            book_name=book_name,
            transcript=transcript.text,
            segments=transcript.segments,
            duration=transcript.duration,
        )
        md_path.write_text(markdown, encoding="utf-8")
        feishu_result = (
            sync_collected_case(reference_record)
            if sync_to_feishu
            else {"record_id": "", "action": "skipped"}
        )
    finally:
        cleanup_warning = cleanup_directory(work_dir, "临时素材目录")
        if cleanup_warning:
            cleanup_warnings.append(cleanup_warning)

    return {
        "ok": True,
        "platform": platform,
        "source_url": source_url,
        "markdown": str(md_path.resolve()),
        "case_id": reference_record.case_id,
        "feishu_record_id": feishu_result["record_id"],
        "feishu_action": feishu_result["action"],
        "warnings": cleanup_warnings,
    }
