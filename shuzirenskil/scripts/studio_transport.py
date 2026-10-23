"""Bounded, no-retry HTTP transport and documented asynchronous video adapters."""
from __future__ import annotations
import base64
import copy
import json
import mimetypes
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MAX_BODY = 64 * 1024 * 1024

class TransportError(RuntimeError):
    def __init__(self, message, diagnostic=None):
        super().__init__(message)
        self.diagnostic = diagnostic or {}

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def safe_url(url, *, loopback=False):
    parsed = urllib.parse.urlsplit(url)
    if loopback and parsed.hostname not in {'127.0.0.1','localhost'}:
        raise ValueError('本机模拟模式禁止外部网络地址')
    if parsed.username or parsed.password or parsed.fragment or not parsed.hostname:
        raise ValueError("URL 含凭据、片段或缺少主机")
    if parsed.scheme != "https" and not (loopback and parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}):
        raise ValueError("生产地址必须为 HTTPS")
    return parsed


def request(method, url, *, headers=None, payload=None, raw=None, limit=4*1024*1024, loopback=False, diagnostic=None):
    safe_url(url, loopback=loopback)
    headers = dict(headers or {})
    body = raw
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    if body is not None and len(body) > MAX_BODY:
        raise ValueError("请求体超过 64 MiB；请改用已绑定的私有素材 URL")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    started = time.monotonic()
    def record(response=None, outcome='response'):
        if diagnostic is not None:
            trace = response.headers.get('x-request-id','') if response is not None else ''
            diagnostic.update(method=method, host=urllib.parse.urlsplit(url).hostname,
                elapsed_seconds=round(time.monotonic()-started,3), outcome=outcome,
                http_status=getattr(response,'code',None),
                trace_id=trace if re.fullmatch(r'[A-Za-z0-9._:-]{1,200}',trace) else '')
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=90) as response:
            record(response)
            data = response.read(limit + 1)
            if len(data) > limit:
                raise TransportError("响应超过大小上限")
            record(response)
            return data
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # Do not echo bodies, signed URLs or API credentials into logs.
        record(exc if isinstance(exc, urllib.error.HTTPError) else None, 'http_error' if isinstance(exc, urllib.error.HTTPError) else 'network_unknown')
        code = getattr(exc, "code", None)
        if isinstance(exc, urllib.error.HTTPError):
            from response_diagnostics import summary
            try: detail = summary(json.loads(exc.read(8192)), headers.get('Authorization','').split(' ',1)[-1] or headers.get('x-goog-api-key',''))
            except (ValueError, UnicodeError): detail = '响应不是 JSON'
            finally: exc.close()
            raise TransportError(f'HTTP {code}: {detail}；未自动重试') from None
        raise TransportError(f"HTTP 请求未确认成功，状态码 {code or 'unknown'}；未自动重试") from None


def json_request(method, url, **kwargs):
    try:
        value = json.loads(request(method, url, **kwargs))
    except (json.JSONDecodeError, UnicodeError):
        raise TransportError("接口响应不是有效 JSON；未自动重试") from None
    if not isinstance(value, dict):
        raise TransportError("接口响应不是 JSON 对象")
    from response_diagnostics import failed, summary
    if failed(value):
        auth = (kwargs.get('headers') or {}).get('Authorization', '')
        raise TransportError('Provider 业务错误: ' + summary(value, auth.split(' ',1)[-1] or (kwargs.get('headers') or {}).get('x-goog-api-key','')))
    return value


def data_uri(path):
    path = Path(path)
    if path.stat().st_size > 45*1024*1024:
        raise ValueError("素材过大，请使用私有 HTTPS 素材绑定")
    return 'data:' + (mimetypes.guess_type(path.name)[0] or 'application/octet-stream') + ';base64,' + base64.b64encode(path.read_bytes()).decode()


def replace_assets(value, mapping):
    if isinstance(value, dict):
        return {k: replace_assets(v, mapping) for k,v in value.items()}
    if isinstance(value, list):
        return [replace_assets(v, mapping) for v in value]
    if isinstance(value, str) and value.startswith('asset://'):
        if value not in mapping:
            raise ValueError("请求素材没有传输绑定")
        return mapping[value]
    return value


class Adapter:
    """No remote calls in construction. Keys are fetched only by the caller at execution."""
    def __init__(self, provider, config, key, *, loopback=False):
        self.provider, self.key, self.loopback = provider, key, loopback
        self.last_diagnostic = {}
        defaults = {'minimax':'https://api.minimax.io', 'volcengine':'https://ark.cn-beijing.volces.com',
                    'google':'https://generativelanguage.googleapis.com', 'fal':'https://queue.fal.run',
                    'aihge':'https://api-hk.aihge.com', '91topgo':'https://router.91topgo.com'}
        self.base = config.get('base_url', defaults.get(provider, '')).rstrip('/')
        host = safe_url(self.base, loopback=loopback).hostname
        if loopback and host not in {'127.0.0.1','localhost'}:
            raise ValueError('测试模式只能连接本机模拟服务器，不能调用真实付费服务')
        expected = {'minimax':{'api.minimax.io','api.minimaxi.com'}, 'volcengine':{'ark.cn-beijing.volces.com'},
                    'google':{'generativelanguage.googleapis.com'}, 'fal':{'queue.fal.run'}, 'aihge':{'api-hk.aihge.com'}, '91topgo':{'router.91topgo.com'}}
        allowed = host in expected.get(provider,set())
        if provider == 'aliyun':
            allowed = bool(re.fullmatch(r'[a-zA-Z0-9-]+\.cn-beijing\.maas\.aliyuncs\.com', host))
        if not allowed and not (loopback and host in {'127.0.0.1','localhost'}):
            raise ValueError("API 主机不属于此适配器的已登记服务商")
        if provider not in {'aliyun','minimax','volcengine','google','fal','91topgo','aihge'}:
            raise ValueError("此服务商执行适配器未接入")
        self.headers = {'x-goog-api-key':key} if provider == 'google' else {'Authorization':('Key ' if provider == 'fal' else 'Bearer ') + key}
        if provider == 'aliyun': self.headers['X-DashScope-Async']='enable'
        if provider == 'fal': self.headers['X-Fal-No-Retry']='1'

    def _call(self, method, path, payload=None):
        url = path if path.startswith(('https://','http://')) else self.base + path
        if urllib.parse.urlsplit(url).netloc != urllib.parse.urlsplit(self.base).netloc:
            raise ValueError("任务返回地址不属于已绑定服务商，拒绝转发密钥")
        diagnostic = {}
        self.last_diagnostic = diagnostic
        try:
            return json_request(method,url,headers=self.headers,payload=payload,loopback=self.loopback,diagnostic=diagnostic)
        except TransportError as exc:
            exc.diagnostic = diagnostic
            raise

    def create(self, payload):
        p = self.provider
        paths = {'aliyun':'/api/v1/services/aigc/video-generation/video-synthesis', 'minimax':'/v2/video_generation',
                 'volcengine':'/api/v3/contents/generations/tasks', 'fal':'/fal-ai/sync-lipsync/v2', 'aihge':'/v1/videos', '91topgo':'/v1/videos'}
        if p == 'google':
            payload = copy.deepcopy(payload)
            model = payload.pop('model')
            if not re.fullmatch(r'[a-zA-Z0-9.-]+',model): raise ValueError('无效模型 ID')
            path='/v1beta/models/'+model+':predictLongRunning'
        else: path=paths[p]
        result = self._call('POST',path,payload)
        from response_diagnostics import failed, summary
        if failed(result): raise TransportError('Provider 业务错误: ' + summary(result, self.key))
        from provider_contract import task_identifier
        task_id = task_identifier(p, result)
        if not isinstance(task_id,str) or not task_id or len(task_id)>512 or not re.fullmatch(r'[A-Za-z0-9_./:-]+',task_id):
            raise TransportError("提交后没有有效任务 ID；禁止重发；" + summary(result, self.key), self.last_diagnostic)
        task={'id':task_id}
        if p == 'fal':
            for k in ('status_url','response_url'):
                url=result.get(k,'')
                if urllib.parse.urlsplit(url).netloc != urllib.parse.urlsplit(self.base).netloc:
                    raise TransportError('口型任务返回地址无效，需人工恢复任务 ID')
                safe_url(url,loopback=self.loopback)
                task[k]=url
        return task

    def poll(self, task):
        p=self.provider; ident=urllib.parse.quote(task['id'],safe='')
        paths={'aliyun':'/api/v1/tasks/'+ident,'minimax':'/v2/query/video_generation/'+ident,
               'volcengine':'/api/v3/contents/generations/tasks/'+ident,'aihge':'/v1/videos/'+ident,'91topgo':'/v1/videos/'+ident}
        if p=='fal': path=task['status_url']
        elif p=='google':
            if not re.fullmatch(r'[A-Za-z0-9_./-]+',task['id']) or '..' in task['id']: raise ValueError('无效 operation 名称')
            path='/v1beta/'+task['id']
        else: path=paths[p]
        result=self._call('GET',path)
        url=None; state='pending'
        if p=='aliyun':
            obj=result.get('output',{}); status=obj.get('task_status',''); url=obj.get('video_url')
        elif p=='minimax':
            obj=result.get('task',{}); status=obj.get('status',''); url=obj.get('content',{}).get('url')
        elif p=='volcengine':
            status=result.get('status',''); url=result.get('content',{}).get('video_url')
        elif p=='google':
            status='failed' if result.get('error') else ('succeeded' if result.get('done') else 'running')
            samples=result.get('response',{}).get('generateVideoResponse',{}).get('generatedSamples',[])
            if samples: url=samples[0].get('video',{}).get('uri')
        elif p=='fal':
            status=result.get('status','')
            if status=='COMPLETED':
                output=self._call('GET',task['response_url']); url=output.get('video',{}).get('url')
                if output.get('error'): status='failed'
        else:
            from provider import extract_video_url
            status=result.get('status',''); url=extract_video_url(result)
            if url:url=urllib.parse.urljoin(self.base+'/',url)
            if not url and status in {'completed','succeeded','done'}: url=self.base+'/v1/videos/'+ident+'/content'
        from provider_contract import task_state
        try: state = task_state(p, status)
        except ValueError as exc: raise TransportError(str(exc)) from None
        if state=='succeeded' and not url: raise TransportError('任务完成但无媒体地址，不能重复提交')
        return {'state':state,'url':url}

    def download(self,url,path):
        path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
        part=path.with_suffix(path.suffix+'.part')
        origin=urllib.parse.urlsplit(self.base).netloc
        for hop in range(6):
            parsed=safe_url(url,loopback=self.loopback)
            if self.loopback and parsed.hostname not in {'localhost','127.0.0.1'}:
                raise ValueError('测试模式不能下载外部媒体')
            headers=self.headers if parsed.netloc==origin else {}
            req=urllib.request.Request(url,headers=headers)
            try:
                response=urllib.request.build_opener(NoRedirect()).open(req,timeout=90)
            except urllib.error.HTTPError as exc:
                if exc.code in {301,302,303,307,308} and exc.headers.get('Location') and hop<5:
                    url=urllib.parse.urljoin(url,exc.headers['Location']);exc.close();continue
                exc.close();raise TransportError('媒体下载失败；保留任务，稍后仅重新下载') from None
            except (urllib.error.URLError,TimeoutError,OSError):
                raise TransportError('媒体下载连接失败；保留任务，未重新提交') from None
            try:
                with response, open(part,'wb') as stream:
                    os.chmod(part,0o600);total=0
                    while chunk:=response.read(1024*1024):
                        if total==0 and (len(chunk)<12 or chunk[4:8]!=b'ftyp'):
                            raise TransportError('下载不是 MP4/MOV 媒体')
                        total+=len(chunk)
                        if total>500*1024*1024:raise TransportError('媒体超过 500 MiB 上限')
                        stream.write(chunk)
                    if total<12:raise TransportError('媒体为空')
                os.replace(part,path);return
            except Exception:
                if part.exists():part.unlink()
                raise
        raise TransportError('媒体重定向次数过多')
