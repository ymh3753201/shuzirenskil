#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import mimetypes
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class ImageProviderError(RuntimeError):
    pass


MAX_IMAGE_BYTES = 20 * 1024 * 1024


def _join_endpoint(base_url: str, path: str) -> str:
    base = base_url.rstrip("/")
    suffix = path if path.startswith("/") else f"/{path}"
    if base.endswith("/v1") and suffix.startswith("/v1/"):
        suffix = suffix[3:]
    return f"{base}{suffix}"


def _request_json(url: str, api_key: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "shuzirenskil-image/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_IMAGE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        detail = exc.read(2048).decode("utf-8", errors="replace").replace(api_key, "<redacted>")
        raise ImageProviderError(f"图片接口 HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ImageProviderError(f"图片接口网络失败: {exc}") from exc
    if len(raw) > MAX_IMAGE_BYTES:
        raise ImageProviderError("图片接口响应超过 20MB")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImageProviderError("图片接口没有返回可识别 JSON") from exc
    if not isinstance(value, dict):
        raise ImageProviderError("图片接口 JSON 顶层不是对象")
    return value


def _request_multipart(
    url: str,
    api_key: str,
    fields: dict[str, str],
    image_path: Path,
    timeout: float,
) -> dict[str, Any]:
    image_bytes = image_path.read_bytes()
    if not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
        raise ImageProviderError("输入参考图为空或超过 20MB")
    boundary = "----shuzirenskil-image-edit-7f8e9d0c"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend([
            f"--{boundary}\r\n".encode("ascii"),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"),
            value.encode("utf-8"),
            b"\r\n",
        ])
    mime_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
    chunks.extend([
        f"--{boundary}\r\n".encode("ascii"),
        b'Content-Disposition: form-data; name="image[]"; filename="identity-reference.png"\r\n',
        f"Content-Type: {mime_type}\r\n\r\n".encode("ascii"),
        image_bytes,
        b"\r\n",
        f"--{boundary}--\r\n".encode("ascii"),
    ])
    request = urllib.request.Request(
        url,
        data=b"".join(chunks),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
            "User-Agent": "shuzirenskil-image/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_IMAGE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        detail = exc.read(2048).decode("utf-8", errors="replace").replace(api_key, "<redacted>")
        raise ImageProviderError(f"图片编辑接口 HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ImageProviderError(f"图片编辑接口网络失败: {exc}") from exc
    if len(raw) > MAX_IMAGE_BYTES:
        raise ImageProviderError("图片编辑接口响应超过 20MB")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImageProviderError("图片编辑接口没有返回可识别 JSON") from exc
    if not isinstance(value, dict):
        raise ImageProviderError("图片编辑接口 JSON 顶层不是对象")
    return value


def _download_image(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "shuzirenskil-image/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(MAX_IMAGE_BYTES + 1)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ImageProviderError(f"下载生成图片失败: {exc}") from exc
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ImageProviderError("生成图片为空或超过 20MB")
    return data


def generate_image(
    base_url: str,
    api_key: str,
    model: str,
    path: str,
    prompt: str,
    size: str,
    output: Path,
    timeout: float,
    source_image: Path | None = None,
    edit_path: str | None = None,
) -> dict[str, Any]:
    if model != "gpt-image-2":
        raise ValueError("图片模型必须是 gpt-image-2")
    payload = {"model": model, "prompt": prompt, "size": size, "n": 1}
    if source_image:
        if not edit_path:
            raise ValueError("使用本人参考图时必须配置图片编辑路径")
        response = _request_multipart(
            _join_endpoint(base_url, edit_path),
            api_key,
            {key: str(value) for key, value in payload.items()},
            source_image,
            timeout,
        )
        request_fields = sorted([*payload, "image[]"])
    else:
        response = _request_json(_join_endpoint(base_url, path), api_key, payload, timeout)
        request_fields = sorted(payload)
    items = response.get("data")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        raise ImageProviderError("图片接口响应缺少 data[0]")
    item = items[0]
    provider_image_url = None
    if isinstance(item.get("b64_json"), str):
        try:
            data = base64.b64decode(item["b64_json"], validate=True)
        except (ValueError, TypeError) as exc:
            raise ImageProviderError("图片接口返回的 Base64 无效") from exc
    elif isinstance(item.get("url"), str) and item["url"].startswith(("https://", "http://")):
        provider_image_url = item["url"] if item["url"].startswith("https://") else None
        data = _download_image(item["url"], timeout)
    else:
        raise ImageProviderError("图片接口响应没有 b64_json 或可下载 URL")
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ImageProviderError("生成图片为空或超过 20MB")
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, output)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    return {
        "model": model,
        "size": size,
        "request_fields": request_fields,
        "output": str(output.resolve()),
        "provider_image_url": provider_image_url,
    }
