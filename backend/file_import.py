from __future__ import annotations

import base64
import binascii
import hashlib
from pathlib import PurePath

from .service import Problem

MAX_FILE_BYTES = 512 * 1024
ENCODINGS = {"utf-8-sig", "gb18030", "utf-16"}


def decode_upload(filename: str, encoded: str, encoding: str) -> dict:
    if not isinstance(filename, str) or len(filename) > 180 or not filename.strip():
        raise Problem("VALIDATION_ERROR", "文件名无效", 422)
    if filename != PurePath(filename).name or "\\" in filename or "/" in filename or filename.lower().endswith((".txt", ".md")) is False:
        raise Problem("VALIDATION_ERROR", "只允许 TXT 或 Markdown 文件", 422)
    if encoding not in ENCODINGS or not isinstance(encoded, str) or len(encoded) > (MAX_FILE_BYTES * 4 // 3 + 8):
        raise Problem("VALIDATION_ERROR", "文件编码或大小无效", 422)
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise Problem("VALIDATION_ERROR", "文件内容不是有效 Base64", 422)
    if not raw or len(raw) > MAX_FILE_BYTES:
        raise Problem("VALIDATION_ERROR", "文件不能为空或超过 512 KiB", 422)
    try:
        content = raw.decode(encoding, errors="strict")
    except UnicodeError:
        raise Problem("DECODE_FAILED", "此编码无法无损解码，请选择其他编码并重新预览", 422)
    if not content.strip() or len(content) > 200000 or "\x00" in content or "\ufffd" in content:
        raise Problem("VALIDATION_ERROR", "解码文本为空、过长或含无效字符", 422)
    return {
        "filename": filename,
        "encoding": encoding,
        "content": content,
        "byte_sha256": hashlib.sha256(raw).hexdigest(),
        "text_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "byte_size": len(raw),
        "character_count": len(content),
    }
