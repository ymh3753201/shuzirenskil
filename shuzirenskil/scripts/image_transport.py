#!/usr/bin/env python3
"""Build compact local image references without uploading them to a Provider."""

from __future__ import annotations

import base64
import shutil
import subprocess
from pathlib import Path


class ImageTransportError(ValueError):
    """Raised when a local reference cannot be safely prepared for transport."""


def local_image_data_uri(
    path: Path,
    *,
    transport: str = "jpeg_data_uri",
    max_bytes: int = 800_000,
    quality: int = 6,
) -> str:
    """Convert a local image to a bounded JPEG data URI.

    The source image is never modified. The JPEG exists only in memory and is
    sent as the Provider reference value. Public HTTPS URLs pass through the
    normal URL path and do not call this function.
    """
    source = path.expanduser().resolve()
    if not source.is_file():
        raise ImageTransportError(f"本地参考图不存在: {source}")
    if transport not in {"jpeg_data_uri", "jpg_data_uri"}:
        raise ImageTransportError(f"不支持的本地图片传输方式: {transport}")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ImageTransportError("91topgo 本地参考图传输需要 ffmpeg 生成压缩 JPEG")
    limit = int(max_bytes or 0)
    if limit <= 0:
        raise ImageTransportError("本地图片 Data URI 必须设置正数大小上限")
    initial_quality = max(2, min(31, int(quality or 6)))
    attempts = [initial_quality, min(31, initial_quality + 4), min(31, initial_quality + 8)]
    encoded = b""
    for current_quality in dict.fromkeys(attempts):
        result = subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-f",
                "image2pipe",
                "-c:v",
                "mjpeg",
                "-q:v",
                str(current_quality),
                "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0 or not result.stdout:
            detail = result.stderr.decode("utf-8", errors="replace")[-240:]
            raise ImageTransportError(f"本地参考图无法压缩为 JPEG: {detail}")
        encoded = result.stdout
        if len(encoded) <= limit:
            break
    if len(encoded) > limit:
        raise ImageTransportError(f"压缩后的参考图仍超过 {limit} 字节: {source}")
    return "data:image/jpeg;base64," + base64.b64encode(encoded).decode("ascii")
