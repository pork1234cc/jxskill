from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from article_monitor.media import probe_media
from article_monitor.project import temporary_root
from article_monitor.wechat_decrypt import (
    KEYSTREAM_SIZE,
    WechatDecryptError,
    decrypt_wechat_media,
    generate_keystream,
)

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass


class GenerateKeystreamTests(unittest.TestCase):
    def test_decode_key_is_passed_only_through_stdin(self) -> None:
        """decode_key 不得进入命令行参数，桥接输出必须是固定长度字节。"""

        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            bridge_path = Path(temp_value) / "bridge.mjs"
            bridge_path.write_text("// test", encoding="utf-8")
            expected = b"\x5a" * KEYSTREAM_SIZE
            calls: list[tuple[list[str], dict[str, Any]]] = []

            def fake_runner(
                args: list[str], **kwargs: Any
            ) -> subprocess.CompletedProcess[str]:
                calls.append((args, kwargs))
                stdout = json.dumps(
                    {
                        "keystream": base64.b64encode(expected).decode("ascii"),
                        "size": KEYSTREAM_SIZE,
                    }
                )
                return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

            result = generate_keystream(
                "12345678901234567890",
                bridge_path=bridge_path,
                node_path="C:/node/node.exe",
                runner=fake_runner,
            )

        self.assertEqual(result, expected)
        self.assertEqual(calls[0][0], ["C:/node/node.exe", str(bridge_path)])
        self.assertEqual(
            json.loads(calls[0][1]["input"])["decode_key"],
            "12345678901234567890",
        )
        self.assertEqual(calls[0][1]["encoding"], "utf-8")
        self.assertNotIn("text", calls[0][1])

    def test_failure_redacts_decode_key(self) -> None:
        """下游错误包含密钥时，对外异常必须脱敏。"""

        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            bridge_path = Path(temp_value) / "bridge.mjs"
            bridge_path.write_text("// test", encoding="utf-8")
            decode_key = "12345678901234567890"

            def fake_runner(
                args: list[str], **_kwargs: Any
            ) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(
                    args,
                    1,
                    stdout="",
                    stderr=f"bad key {decode_key}",
                )

            with self.assertRaises(WechatDecryptError) as captured:
                generate_keystream(
                    decode_key,
                    bridge_path=bridge_path,
                    node_path="node",
                    runner=fake_runner,
                )

        self.assertNotIn(decode_key, str(captured.exception))

    @unittest.skipUnless(
        os.environ.get("RUN_WECHAT_WASM_TEST") == "1",
        "仅在显式启用时启动本地 Chromium/WASM",
    )
    def test_wasm_keystream_known_vector(self) -> None:
        """锁定 ISAAC64 初始化、字节序和整体反转行为。"""

        keystream = generate_keystream("1")

        self.assertEqual(
            hashlib.sha256(keystream).hexdigest(),
            "39d98f5b25cc52f0996f7ef9e1022156cb029901be6a11ebbdd7469c6ffd5839",
        )


class MediaProbeTests(unittest.TestCase):
    def test_probe_media_returns_valid_stream_metadata(self) -> None:
        """ffprobe JSON 至少包含一个媒体流时应返回元数据。"""

        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            media_path = Path(temp_value) / "video.mp4"
            media_path.write_bytes(b"video")

            def fake_runner(
                args: list[str], **_kwargs: Any
            ) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout='{"streams":[{"codec_type":"video"}]}',
                    stderr="",
                )

            metadata = probe_media(
                media_path,
                runner=fake_runner,
                tool_resolver=lambda _name: "ffprobe",
            )

        self.assertEqual(metadata["streams"][0]["codec_type"], "video")


class DecryptWechatMediaTests(unittest.TestCase):
    def test_xors_only_first_128k(self) -> None:
        """超过 128 KiB 的媒体只解密前缀，尾部保持不变。"""

        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            root = Path(temp_value)
            keystream = bytes(range(256)) * (KEYSTREAM_SIZE // 256)
            payload = bytes(index % 251 for index in range(KEYSTREAM_SIZE + 50))
            clear = b"\x00\x00\x00\x18ftypisom" + payload
            encrypted = bytearray(clear)
            for index in range(KEYSTREAM_SIZE):
                encrypted[index] ^= keystream[index]
            encrypted_path = root / "encrypted.mp4"
            output_path = root / "decrypted.mp4"
            encrypted_path.write_bytes(encrypted)

            result = decrypt_wechat_media(
                "12345678901234567890",
                encrypted_path,
                output_path,
                keystream_generator=lambda _key: keystream,
                media_probe=lambda _path: {"streams": [{"codec_type": "video"}]},
            )

            self.assertEqual(result, output_path)
            self.assertEqual(output_path.read_bytes(), clear)
            self.assertEqual(
                output_path.read_bytes()[KEYSTREAM_SIZE:],
                encrypted[KEYSTREAM_SIZE:],
            )

    def test_invalid_ftyp_preserves_existing_output(self) -> None:
        """错误密钥导致 ftyp 缺失时不得覆盖既有文件。"""

        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            root = Path(temp_value)
            encrypted_path = root / "encrypted.mp4"
            output_path = root / "decrypted.mp4"
            encrypted_path.write_bytes(b"not-an-mp4")
            output_path.write_bytes(b"existing")

            with self.assertRaisesRegex(WechatDecryptError, "ftyp"):
                decrypt_wechat_media(
                    "12345678901234567890",
                    encrypted_path,
                    output_path,
                    keystream_generator=lambda _key: b"\x00" * KEYSTREAM_SIZE,
                    media_probe=lambda _path: {"streams": [{}]},
                )

            self.assertEqual(output_path.read_bytes(), b"existing")
            self.assertFalse((root / "decrypted.part.mp4").exists())

    def test_ffprobe_failure_is_mapped_to_decrypt_error(self) -> None:
        """通过 ftyp 但未通过 ffprobe 的文件必须归类为解密失败。"""

        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            root = Path(temp_value)
            encrypted_path = root / "encrypted.mp4"
            encrypted_path.write_bytes(b"\x00\x00\x00\x18ftypisom")

            def failing_probe(_path: Path) -> dict[str, Any]:
                raise RuntimeError("invalid media")

            with self.assertRaisesRegex(WechatDecryptError, "ffprobe"):
                decrypt_wechat_media(
                    "12345678901234567890",
                    encrypted_path,
                    root / "decrypted.mp4",
                    keystream_generator=lambda _key: b"\x00" * KEYSTREAM_SIZE,
                    media_probe=failing_probe,
                )

    def test_rejects_invalid_keystream_length(self) -> None:
        """桥接返回非 128 KiB 数据时不创建解密产物。"""

        with tempfile.TemporaryDirectory(dir=temporary_root()) as temp_value:
            root = Path(temp_value)
            encrypted_path = root / "encrypted.mp4"
            output_path = root / "decrypted.mp4"
            encrypted_path.write_bytes(b"encrypted")

            with self.assertRaisesRegex(WechatDecryptError, "密钥流长度无效"):
                decrypt_wechat_media(
                    "12345678901234567890",
                    encrypted_path,
                    output_path,
                    keystream_generator=lambda _key: b"too-short",
                )

            self.assertFalse(output_path.exists())
            self.assertFalse((root / "decrypted.part.mp4").exists())


if __name__ == "__main__":
    unittest.main()
