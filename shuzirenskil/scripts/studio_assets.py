"""Explicit local/data-URI or hash-bound HTTPS asset transport; no automatic public upload."""
from __future__ import annotations
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from _common import sha256_file
from studio_transport import data_uri, request, safe_url


def utc_stamp():
    return datetime.now(timezone.utc).isoformat()


def unexpired(value):
    try:
        end=datetime.fromisoformat(value.replace('Z','+00:00'))
        return end.tzinfo is not None and end>datetime.now(timezone.utc)
    except (ValueError,TypeError,AttributeError):return False


def load_bindings(path):
    if path is None:return {}
    path=Path(path)
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError('素材地址文件应为非符号链接的 600 权限文件')
    obj=json.loads(path.read_text())
    if not isinstance(obj,dict):raise ValueError('素材绑定必须为 JSON 对象')
    return obj


def resolve(path, sha, bindings, *, require_url=False, loopback=False):
    path=Path(path)
    if sha256_file(path)!=sha:raise ValueError('本地素材已经变化，必须重新确认')
    binding=bindings.get(sha)
    if binding:
        if not unexpired(binding.get('expires_at')):raise ValueError('素材传输地址已过期')
        url=binding.get('url','')
        # Authorized portrait asset IDs are provider references, not local placeholder hashes.
        if url.startswith('asset://asset-'):
            if binding.get('portrait_authorized') is not True:raise ValueError('Seedance 人像资产未确认授权')
            return url
        safe_url(url,loopback=loopback)
        # Optional user-provided presigned PUT is scoped to this content hash. Never set public ACL.
        if binding.get('put_url'):
            request('PUT',binding['put_url'],raw=path.read_bytes(),limit=1024*1024,loopback=loopback)
        raw=request('GET',url,limit=path.stat().st_size+1,loopback=loopback)
        if hashlib.sha256(raw).hexdigest()!=sha:raise ValueError('远端素材与已确认本地文件不同')
        return url
    if require_url:raise ValueError('此接口需要可读取的素材 URL；请提供按 SHA-256 绑定的私有 HTTPS 地址')
    return data_uri(path)
