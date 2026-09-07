"""视频号 ISAAC64 前 128 KiB XOR 本地解密。"""

from __future__ import annotations

import base64
import binascii
import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .media import probe_media

KEYSTREAM_SIZE = 131072
CHUNK_SIZE = 64 * 1024
SUPPORTED_SCHEME = "isaac64-xor-first-128k"

ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]
KeystreamGenerator = Callable[[str], bytes]
MediaProbe = Callable[[Path], dict[str, Any]]


class WechatDecryptError(RuntimeError):
    """视频号本地解密失败。"""


def _default_bridge_path() -> Path:
    """返回项目随附的 Node 解密桥接脚本。"""

    project_root = Path(__file__).resolve().parents[2]
    return project_root / "scripts" / "wechat-decrypt" / "bridge.mjs"


def _resolve_node(
    *,
    environ: Mapping[str, str] | None = None,
    finder: Callable[[str], str | None] = shutil.which,
) -> str:
    """解析视频号解密桥接使用的 Node.js。"""

    active_environ = os.environ if environ is None else environ
    configured = active_environ.get("WECHAT_DECRYPT_NODE_PATH", "").strip()
    if configured:
        if Path(configured).is_file():
            return configured
        raise WechatDecryptError(
            f"WECHAT_DECRYPT_NODE_PATH 指向的可执行文件不存在: {configured}"
        )
    found = finder("node")
    if found:
        return found
    raise WechatDecryptError("找不到 Node.js，请加入 PATH 或设置 WECHAT_DECRYPT_NODE_PATH")


def generate_keystream(
    decode_key: str,
    *,
    bridge_path: Path | None = None,
    node_path: str | None = None,
    runner: ProcessRunner = subprocess.run,
    timeout_seconds: float = 90.0,
) -> bytes:
    """通过本地 Node、Playwright 和 WASM 生成反转后的密钥流。"""

    if not isinstance(decode_key, str) or not decode_key.isdigit():
        raise WechatDecryptError("视频号 decode_key 必须是非空数字字符串")
    if timeout_seconds <= 0:
        raise WechatDecryptError("视频号密钥流生成超时时间必须大于 0")
    active_bridge = bridge_path or _default_bridge_path()
    if not active_bridge.is_file():
        raise WechatDecryptError(f"视频号解密桥接脚本不存在: {active_bridge}")
    active_node = node_path or _resolve_node()
    payload = json.dumps({"decode_key": decode_key}, ensure_ascii=False)
    try:
        result = runner(
            [active_node, str(active_bridge)],
            input=payload,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise WechatDecryptError("视频号密钥流生成超时") from exc
    except (FileNotFoundError, OSError) as exc:
        raise WechatDecryptError("无法启动视频号本地解密运行时") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "未知错误").strip()
        safe_detail = detail.replace(decode_key, "[REDACTED]")
        raise WechatDecryptError(f"视频号密钥流生成失败: {safe_detail}")
    try:
        response = json.loads(result.stdout)
        encoded = response["keystream"]
        keystream = base64.b64decode(encoded, validate=True)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise WechatDecryptError("视频号密钥流桥接返回格式无效") from exc
    if len(keystream) != KEYSTREAM_SIZE:
        raise WechatDecryptError(
            f"视频号密钥流长度无效: 期望 {KEYSTREAM_SIZE}，实际 {len(keystream)}"
        )
    return keystream


def _partial_output_path(output_path: Path) -> Path:
    """创建保留媒体扩展名的临时输出路径。"""

    return output_path.with_name(f"{output_path.stem}.part{output_path.suffix}")


def _cleanup_file(path: Path) -> None:
    """尽力清理当前任务创建的临时文件。"""

    try:
        path.unlink(missing_ok=True)
    except OSError:
        return


def _xor_prefix(encrypted_path: Path, output_path: Path, keystream: bytearray) -> None:
    """流式复制媒体，并仅 XOR 最前方 128 KiB。"""

    position = 0
    with encrypted_path.open("rb") as source, output_path.open("wb") as target:
        while chunk := source.read(CHUNK_SIZE):
            decrypted = bytearray(chunk)
            xor_length = min(len(decrypted), max(0, KEYSTREAM_SIZE - position))
            for index in range(xor_length):
                decrypted[index] ^= keystream[position + index]
            target.write(decrypted)
            position += len(decrypted)


def _assert_ftyp(path: Path) -> None:
    """检查 MP4 文件头偏移 4～7 的 ftyp 签名。"""

    with path.open("rb") as media:
        header = media.read(8)
    if len(header) < 8 or header[4:8] != b"ftyp":
        raise WechatDecryptError(
            "视频号解密结果缺少 MP4 ftyp，请检查媒体地址与 decode_key 配对"
        )


def decrypt_wechat_media(
    decode_key: str,
    encrypted_path: Path,
    output_path: Path,
    *,
    scheme: str = SUPPORTED_SCHEME,
    keystream_generator: KeystreamGenerator = generate_keystream,
    media_probe: MediaProbe = probe_media,
) -> Path:
    """解密视频号媒体，通过 ftyp 与 ffprobe 后原子生成目标文件。"""

    if scheme != SUPPORTED_SCHEME:
        raise WechatDecryptError(f"不支持的视频号解密方案: {scheme}")
    if not isinstance(decode_key, str) or not decode_key.isdigit():
        raise WechatDecryptError("视频号媒体缺少有效 decode_key")
    if not encrypted_path.is_file() or encrypted_path.stat().st_size <= 0:
        raise WechatDecryptError(f"视频号加密媒体不存在或为空: {encrypted_path}")
    if encrypted_path.resolve() == output_path.resolve():
        raise WechatDecryptError("视频号解密禁止原地覆盖输入文件")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = _partial_output_path(output_path)
    _cleanup_file(partial_path)
    keystream = bytearray(keystream_generator(decode_key))
    try:
        if len(keystream) != KEYSTREAM_SIZE:
            raise WechatDecryptError("视频号解密密钥流长度无效")
        _xor_prefix(encrypted_path, partial_path, keystream)
        _assert_ftyp(partial_path)
        try:
            media_probe(partial_path)
        except (RuntimeError, TypeError, ValueError) as exc:
            raise WechatDecryptError(f"视频号解密结果未通过 ffprobe: {exc}") from exc
        partial_path.replace(output_path)
    except WechatDecryptError:
        _cleanup_file(partial_path)
        raise
    except OSError as exc:
        _cleanup_file(partial_path)
        raise WechatDecryptError(f"视频号解密文件处理失败: {exc}") from exc
    finally:
        for index in range(len(keystream)):
            keystream[index] = 0
    return output_path
