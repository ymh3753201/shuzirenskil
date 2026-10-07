#!/usr/bin/env python3
from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, ambiguous: bool = False):
        super().__init__(message)
        self.ambiguous = ambiguous


TASK_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
MAX_VIDEO_BYTES = 500 * 1024 * 1024
MAX_UPLOAD_BYTES = 48 * 1024 * 1024
PROVIDER_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "shuzirenskil/1.3 (+https://router.91topgo.com/)",
}


def _redact_provider_text(value: str, api_key: str | None = None) -> str:
    if api_key:
        value = value.replace(api_key, "<redacted:api-key>")
    value = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "sk-***", value)
    value = re.sub(r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+", "<redacted:image>", value)
    value = re.sub(r"Authorization\s*[:=]\s*[^\s,}]+", "Authorization=<redacted>", value, flags=re.IGNORECASE)
    return value[:2048]


def _first_string(value: dict[str, Any], paths: tuple[tuple[str, ...], ...]) -> str | None:
    for path in paths:
        current: Any = value
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if isinstance(current, str) and current.strip():
            return current.strip()
    return None


def _json_request(url: str, method: str, api_key: str, payload: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            **PROVIDER_HEADERS,
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = _redact_provider_text(exc.read(4096).decode("utf-8", errors="replace"), api_key)
        raise ProviderError(f"Provider HTTP {exc.code}: {detail}", ambiguous=method == "POST") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProviderError(f"Provider 网络结果不明: {exc}", ambiguous=method == "POST") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError("Provider 返回的不是可识别 JSON", ambiguous=method == "POST") from exc
    if not isinstance(value, dict):
        raise ProviderError("Provider JSON 顶层不是对象", ambiguous=method == "POST")
    return value


def create_task(base_url: str, path: str, api_key: str, payload: dict[str, Any], timeout: float) -> str:
    value = _json_request(f"{base_url.rstrip('/')}{path}", "POST", api_key, payload, timeout)
    task_id = _first_string(value, (
        ("task_id",), ("request_id",), ("id",),
        ("data", "task_id"), ("data", "request_id"), ("data", "id"),
        ("video", "task_id"), ("video", "request_id"), ("video", "id"),
    ))
    if not task_id or not TASK_ID_RE.fullmatch(task_id):
        raise ProviderError("创建响应缺少文档约定的任务编号，禁止猜测或重试", ambiguous=True)
    return task_id


def list_model_ids(base_url: str, api_key: str, timeout: float) -> list[str]:
    value = _json_request(f"{base_url.rstrip('/')}/v1/models", "GET", api_key, None, timeout)
    items = value.get("data")
    if not isinstance(items, list):
        raise ProviderError("模型列表响应缺少 data 数组")
    model_ids = [item.get("id") for item in items if isinstance(item, dict) and isinstance(item.get("id"), str)]
    return sorted(set(model_ids))


def probe_endpoint(base_url: str, path: str, api_key: str, timeout: float) -> dict[str, Any]:
    """Probe a provider-specific endpoint without creating a video task."""
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        method="GET",
        headers={**PROVIDER_HEADERS, "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(4096)
            status = int(response.status)
            content_type = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        raw = exc.read(4096)
        status = int(exc.code)
        content_type = exc.headers.get("Content-Type", "") if exc.headers else ""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProviderError(f"Provider 网络结果不明: {exc}") from exc
    detail = _redact_provider_text(raw.decode("utf-8", errors="replace"), api_key)
    return {"status_code": status, "content_type": content_type, "detail": detail}


def upload_file(
    base_url: str,
    path: str,
    api_key: str,
    source: Path,
    expires_after: int,
    timeout: float,
) -> dict[str, Any]:
    if not source.is_file() or source.is_symlink():
        raise ProviderError("待上传素材不是普通文件")
    size = source.stat().st_size
    if size <= 0 or size > MAX_UPLOAD_BYTES:
        raise ProviderError("待上传素材必须大于 0 且不超过 48MB")
    if not 3600 <= expires_after <= 2_592_000:
        raise ProviderError("上传素材有效期必须在 1 小时到 30 天之间")
    boundary = f"----shuzirenskil-{secrets.token_hex(16)}"
    content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", source.name)[:180] or "upload.bin"
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"expires_after\"\r\n\r\n{expires_after}\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\nassistants\r\n".encode(),
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{safe_name}\"\r\n"
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode(),
        source.read_bytes(),
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    body = b"".join(parts)
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        method="POST",
        headers={
            **PROVIDER_HEADERS,
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = _redact_provider_text(exc.read(4096).decode("utf-8", errors="replace"), api_key)
        raise ProviderError(f"Files API HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProviderError(f"Files API 上传结果不明: {exc}") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError("Files API 返回的不是可识别 JSON") from exc
    file_id = value.get("id") if isinstance(value, dict) else None
    if not isinstance(file_id, str) or not re.fullmatch(r"file[-_][A-Za-z0-9._:-]{1,200}", file_id):
        raise ProviderError("Files API 响应缺少有效 file_id")
    return {
        "file_id": file_id,
        "filename": value.get("filename") if isinstance(value.get("filename"), str) else source.name,
        "bytes": value.get("bytes") if isinstance(value.get("bytes"), int) else size,
        "expires_at": value.get("expires_at"),
    }


def get_status(base_url: str, path: str, api_key: str, timeout: float) -> tuple[str, dict[str, Any]]:
    value = _json_request(f"{base_url.rstrip('/')}{path}", "GET", api_key, None, timeout)
    status = value.get("status")
    if not isinstance(status, str) or not status.strip():
        raise ProviderError("状态响应缺少顶层 status")
    return status.strip().lower(), value


def extract_video_url(value: dict[str, Any]) -> str | None:
    return _first_string(value, (
        ("download_url",), ("video_url",), ("url",), ("video", "url"),
        ("video", "download_url"), ("result", "download_url"),
        ("result", "video_url"), ("result", "url"),
        ("data", "download_url"), ("data", "video_url"), ("data", "url"),
        ("data", "video", "url"),
    ))


def download_video_url(base_url: str, video_url: str, output: Path, timeout: float) -> None:
    resolved_url = urljoin(f"{base_url.rstrip('/')}/", video_url)
    parsed = urlsplit(resolved_url)
    loopback_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if (parsed.scheme != "https" and not loopback_http) or not parsed.hostname or parsed.username or parsed.password:
        raise ProviderError("完成响应中的视频地址不是安全的 HTTPS URL")
    request = urllib.request.Request(resolved_url, method="GET", headers=PROVIDER_HEADERS)
    _download_response(request, output, timeout)


def download_content(base_url: str, path: str, api_key: str, output: Path, timeout: float) -> None:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        method="GET",
        headers={**PROVIDER_HEADERS, "Authorization": f"Bearer {api_key}"},
    )
    _download_response(request, output, timeout)


def _download_response(request: urllib.request.Request, output: Path, timeout: float) -> None:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "")
            data = response.read(MAX_VIDEO_BYTES + 1)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProviderError(f"下载视频失败: {exc}") from exc
    if len(data) > MAX_VIDEO_BYTES:
        raise ProviderError("下载视频超过 500MB 安全上限")
    if not data or ("json" in content_type.lower() and data.lstrip().startswith(b"{")):
        raise ProviderError("下载接口没有返回 MP4 内容")
    if len(data) < 12 or data[4:8] != b"ftyp":
        raise ProviderError("下载内容没有有效 MP4 文件头")
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
