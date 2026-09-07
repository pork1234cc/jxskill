from __future__ import annotations

import base64
import json
import mimetypes
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .project import environment_root, temporary_root

DEFAULT_MODEL = "qwen3-asr-flash"
DEFAULT_REGION = "cn-beijing"
DEFAULT_LANGUAGE = "zh"
DEFAULT_CHUNK_SECONDS = 180.0
DEFAULT_MAX_INLINE_BYTES = 7_500_000


@dataclass
class AsrConfig:
    api_key: str = ""
    workspace_id: str = ""
    region: str = DEFAULT_REGION
    base_url: str = ""
    model: str = DEFAULT_MODEL
    language: str = DEFAULT_LANGUAGE
    enable_itn: bool = False
    timeout: float = 300.0
    max_inline_bytes: int = DEFAULT_MAX_INLINE_BYTES

    @classmethod
    def from_env(cls, env_path: Path | None = None) -> AsrConfig:
        load_dotenv(env_path)
        return cls(
            api_key=(os.environ.get("DASHSCOPE_API_KEY") or "").strip(),
            workspace_id=(os.environ.get("DASHSCOPE_ASR_WORKSPACE_ID") or "").strip(),
            region=(os.environ.get("DASHSCOPE_ASR_REGION") or DEFAULT_REGION).strip() or DEFAULT_REGION,
            base_url=(os.environ.get("DASHSCOPE_ASR_BASE_URL") or "").strip(),
            model=(os.environ.get("DASHSCOPE_ASR_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL,
            language=(os.environ.get("DASHSCOPE_ASR_LANGUAGE") or DEFAULT_LANGUAGE).strip() or DEFAULT_LANGUAGE,
            enable_itn=parse_bool(os.environ.get("DASHSCOPE_ASR_ENABLE_ITN"), default=False),
        )

    def resolved_base_url(self) -> str:
        value = (self.base_url or "").strip().rstrip("/")
        if value and "{WorkspaceId}" in value:
            if not self.workspace_id:
                raise RuntimeError("DASHSCOPE_ASR_BASE_URL contains {WorkspaceId}, but workspace id is missing.")
            value = value.replace("{WorkspaceId}", self.workspace_id)

        if not value:
            if not self.workspace_id:
                raise RuntimeError("Missing DASHSCOPE_ASR_WORKSPACE_ID or DASHSCOPE_ASR_BASE_URL.")
            value = f"https://{self.workspace_id}.{self.region}.maas.aliyuncs.com/compatible-mode/v1"

        return value.rstrip("/")

    def validate(self) -> None:
        if not self.api_key:
            raise RuntimeError("Missing DASHSCOPE_API_KEY. Put it in .env or the process environment.")
        self.resolved_base_url()


@dataclass
class TranscriptionResult:
    provider: str
    model: str
    language: str
    text: str
    segments: list[dict[str, Any]]
    usage: dict[str, Any]
    raw: dict[str, Any]
    duration: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "language": self.language,
            "duration": self.duration,
            "text": self.text,
            "segments": self.segments,
            "usage": self.usage,
            "raw": self.raw,
        }


def load_dotenv(env_path: Path | None = None) -> None:
    for line in read_dotenv_lines(env_path):
        stripped = line.strip()
        if stripped.startswith("- "):
            stripped = stripped[2:].strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip().strip('"').strip("'")
        os.environ[key] = value


def dotenv_candidates(env_path: Path | None = None) -> list[Path]:
    if env_path is not None:
        return [env_path]
    candidates = [environment_root() / ".env"]
    result: list[Path] = []
    for candidate in candidates:
        if candidate not in result:
            result.append(candidate)
    return result


def read_dotenv_lines(env_path: Path | None = None) -> list[str]:
    for candidate in dotenv_candidates(env_path):
        if candidate.exists():
            return candidate.read_text(encoding="utf-8-sig").splitlines()
    return []


def parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None or not str(value).strip():
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def transcribe_url(audio_url: str, *, config: AsrConfig | None = None) -> TranscriptionResult:
    cfg = config or AsrConfig.from_env()
    cfg.validate()
    return _request_transcription(audio_url, cfg, duration=None)


def transcribe_audio(
    audio_path: str | Path,
    *,
    config: AsrConfig | None = None,
    auto_chunk: bool = True,
    chunk_seconds: float = DEFAULT_CHUNK_SECONDS,
    keep_chunks: bool = False,
) -> TranscriptionResult:
    path = Path(audio_path)
    if not path.exists():
        raise FileNotFoundError(f"Audio file does not exist: {path}")

    cfg = config or AsrConfig.from_env()
    cfg.validate()

    duration = probe_duration(path)
    should_chunk = (
        auto_chunk
        and (path.stat().st_size > cfg.max_inline_bytes or (duration and duration > chunk_seconds))
    )
    if not should_chunk:
        data_uri = audio_file_to_data_uri(path)
        return _request_transcription(data_uri, cfg, duration=duration)

    require_ffmpeg()
    return _transcribe_chunks(path, cfg, duration=duration, chunk_seconds=chunk_seconds, keep_chunks=keep_chunks)


def audio_file_to_data_uri(path: Path) -> str:
    mime = guess_audio_mime(path)
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def guess_audio_mime(path: Path) -> str:
    known = {
        ".m4a": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".flac": "audio/flac",
        ".aac": "audio/aac",
        ".ogg": "audio/ogg",
        ".opus": "audio/ogg",
        ".mp4": "video/mp4",
    }
    suffix = path.suffix.lower()
    if suffix in known:
        return known[suffix]
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def _request_transcription(audio_data: str, cfg: AsrConfig, *, duration: float | None) -> TranscriptionResult:
    payload = {
        "model": cfg.model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {"data": audio_data},
                    }
                ],
            }
        ],
        "stream": False,
        "asr_options": {
            "enable_itn": bool(cfg.enable_itn),
        },
    }
    if cfg.language:
        payload["asr_options"]["language"] = cfg.language

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        cfg.resolved_base_url() + "/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {cfg.api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=cfg.timeout) as response:
            response_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Qwen-ASR request failed HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Qwen-ASR request failed: {exc.reason}") from exc

    data = json.loads(response_body)
    text = extract_text(data)
    segments = extract_segments(data)
    if not segments:
        segments = pseudo_segments_from_text(text)
    if not text and segments:
        text = "".join(str(item.get("text") or "") for item in segments).strip()
    if not text:
        raise RuntimeError("Qwen-ASR did not return usable text.")

    return TranscriptionResult(
        provider="dashscope_qwen_asr",
        model=str(data.get("model") or cfg.model),
        language=extract_language(data) or cfg.language,
        text=text,
        segments=segments,
        usage=data.get("usage") if isinstance(data.get("usage"), dict) else {},
        raw=data,
        duration=duration,
    )


def nested_get(data: Any, path: list[str]) -> Any:
    current = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
        return "".join(parts).strip()
    return ""


def extract_text(data: dict[str, Any]) -> str:
    for choices_path in (["choices"], ["output", "choices"]):
        choices = nested_get(data, choices_path)
        if not isinstance(choices, list):
            continue
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message") or choice.get("delta") or {}
            if isinstance(message, dict):
                text = text_from_content(message.get("content"))
                if text:
                    return text
            text = text_from_content(choice.get("content") or choice.get("text"))
            if text:
                return text

    for path in (
        ["output", "text"],
        ["output", "content"],
        ["output", "message", "content"],
        ["text"],
        ["content"],
    ):
        text = text_from_content(nested_get(data, path))
        if text:
            return text
    return ""


def extract_language(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list):
        return ""
    for choice in choices:
        message = choice.get("message") if isinstance(choice, dict) else None
        annotations = message.get("annotations") if isinstance(message, dict) else None
        if not isinstance(annotations, list):
            continue
        for item in annotations:
            if isinstance(item, dict) and item.get("language"):
                return str(item["language"])
    return ""


def extract_segments(data: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = [
        nested_get(data, ["output", "sentences"]),
        nested_get(data, ["sentences"]),
    ]
    transcripts = nested_get(data, ["output", "transcripts"]) or nested_get(data, ["transcripts"])
    if isinstance(transcripts, list):
        for transcript in transcripts:
            if isinstance(transcript, dict):
                candidates.append(transcript.get("sentences"))

    for candidate in candidates:
        if not isinstance(candidate, list):
            continue
        segments: list[dict[str, Any]] = []
        for item in candidate:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            start_ms = item.get("begin_time", item.get("start_time", item.get("start", 0)))
            end_ms = item.get("end_time", item.get("end", start_ms))
            start = float(start_ms or 0)
            end = float(end_ms or start)
            if start > 1000 or end > 1000:
                start /= 1000
                end /= 1000
            if end <= start:
                end = start + 1
            segments.append({"index": len(segments) + 1, "start": round(start, 3), "end": round(end, 3), "text": text})
        if segments:
            return segments
    return []


def split_text_for_srt(text: str, max_chars: int = 48) -> list[str]:
    text = " ".join(str(text or "").strip().split())
    if not text:
        return []
    chunks: list[str] = []
    buffer = ""
    punctuation = set("。！？!?；;，,")
    for char in text:
        buffer += char
        if char in punctuation or len(buffer) >= max_chars:
            chunks.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        chunks.append(buffer.strip())
    return chunks


def pseudo_segments_from_text(text: str) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    cursor = 0.0
    for chunk in split_text_for_srt(text):
        duration = max(1.0, min(8.0, len(chunk) / 6.0))
        start = cursor
        end = start + duration
        segments.append({"index": len(segments) + 1, "start": round(start, 3), "end": round(end, 3), "text": chunk})
        cursor = end + 0.05
    return segments


def probe_duration(path: Path) -> float | None:
    ffprobe = resolve_tool("ffprobe")
    if not ffprobe:
        return None
    command = [
        ffprobe,
        "-hide_banner",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    try:
        return float((result.stdout or "").strip() or 0)
    except ValueError:
        return None


def tool_env_var(name: str) -> str:
    return f"{name.upper()}_PATH"


def resolve_tool(name: str) -> str:
    configured = os.environ.get(tool_env_var(name), "").strip()
    if configured:
        configured_path = Path(configured)
        if configured_path.exists():
            return str(configured_path)
        raise RuntimeError(f"{tool_env_var(name)} points to a missing executable: {configured}")
    return shutil.which(name) or ""


def require_ffmpeg() -> None:
    missing = [name for name in ("ffmpeg", "ffprobe") if not resolve_tool(name)]
    if missing:
        names = ", ".join(missing)
        raise RuntimeError(
            f"Audio is too large or too long for one request, and {names} is not available. "
            "Put ffmpeg/ffprobe on PATH or set FFMPEG_PATH and FFPROBE_PATH."
        )


def split_audio(path: Path, chunk_dir: Path, chunk_seconds: float) -> list[Path]:
    chunk_dir.mkdir(parents=True, exist_ok=True)
    pattern = chunk_dir / "asr_chunk_%03d.wav"
    command = [
        resolve_tool("ffmpeg"),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "segment",
        "-segment_time",
        f"{chunk_seconds:.3f}",
        "-reset_timestamps",
        "1",
        str(pattern),
    ]
    subprocess.run(command, check=True)
    chunks = sorted(chunk_dir.glob("asr_chunk_*.wav"))
    if not chunks:
        raise RuntimeError("ffmpeg did not produce any ASR chunks.")
    return chunks


def _transcribe_chunks(
    path: Path,
    cfg: AsrConfig,
    *,
    duration: float | None,
    chunk_seconds: float,
    keep_chunks: bool,
) -> TranscriptionResult:
    if keep_chunks:
        chunk_root = path.parent / f"{path.stem}_asr_chunks_{int(time.time() * 1000)}"
        chunks = split_audio(path, chunk_root, chunk_seconds)
        cleanup = None
    else:
        cleanup = tempfile.TemporaryDirectory(
            prefix="asr_qwen_",
            dir=temporary_root(),
        )
        chunk_root = Path(cleanup.name)
        chunks = split_audio(path, chunk_root, chunk_seconds)

    try:
        merged_text: list[str] = []
        merged_segments: list[dict[str, Any]] = []
        chunk_meta: list[dict[str, Any]] = []
        usage_total: dict[str, float] = {}
        cursor = 0.0

        for index, chunk in enumerate(chunks, 1):
            chunk_duration = probe_duration(chunk) or chunk_seconds
            result = _request_transcription(audio_file_to_data_uri(chunk), cfg, duration=chunk_duration)
            if result.text:
                merged_text.append(result.text)
            shifted = offset_segments(result.segments, cursor, len(merged_segments) + 1)
            merged_segments.extend(shifted)
            merge_numeric_usage(usage_total, result.usage)
            chunk_meta.append(
                {
                    "index": index,
                    "audio": str(chunk),
                    "offset": round(cursor, 3),
                    "duration": round(chunk_duration, 3),
                    "segments": len(shifted),
                    "usage": result.usage,
                }
            )
            cursor += chunk_duration

        return TranscriptionResult(
            provider="dashscope_qwen_asr",
            model=cfg.model,
            language=cfg.language,
            text="\n".join(merged_text).strip(),
            segments=merged_segments,
            usage=format_usage(usage_total),
            raw={
                "chunked": True,
                "chunk_seconds": round(chunk_seconds, 3),
                "chunks": chunk_meta,
            },
            duration=duration,
        )
    finally:
        if cleanup is not None:
            cleanup.cleanup()


def offset_segments(segments: list[dict[str, Any]], offset: float, start_index: int) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for segment in segments:
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        start = round(offset + float(segment.get("start") or 0), 3)
        end = round(offset + float(segment.get("end") or start + 1), 3)
        if end <= start:
            end = round(start + 1.0, 3)
        output.append({"index": start_index + len(output), "start": start, "end": end, "text": text})
    return output


def merge_numeric_usage(total: dict[str, float], usage: dict[str, Any]) -> None:
    for key, value in usage.items():
        if isinstance(value, (int, float)):
            total[key] = total.get(key, 0.0) + float(value)


def format_usage(total: dict[str, float]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in total.items():
        output[key] = int(value) if value.is_integer() else round(value, 3)
    return output


def format_srt_time(seconds: float) -> str:
    total_ms = max(0, round(seconds * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def write_srt(path: Path, segments: list[dict[str, Any]]) -> None:
    lines: list[str] = []
    for index, segment in enumerate(segments, 1):
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        start = format_srt_time(float(segment.get("start") or 0))
        end = format_srt_time(float(segment.get("end") or 0))
        lines.extend([str(index), f"{start} --> {end}", text, ""])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
