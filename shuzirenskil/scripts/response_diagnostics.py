"""Bounded diagnostic metadata; never retain request payloads or raw responses."""
import json
import re

def summary(value, key=''):
    if not isinstance(value, dict): return '响应不是对象'
    safe = {'fields': sorted(str(k) for k in value)[:30]}
    for name in ('error', 'message', 'code', 'type', 'request_id', 'base_resp'):
        if name in value:
            item = value[name]
            if isinstance(item, dict):
                item = {k: v for k, v in item.items() if k in {'code','message','type','status_code','status_msg','http_code'} and isinstance(v, (str, int, bool, type(None)))}
            elif not isinstance(item, (str, int, bool, type(None))): item = '<omitted>'
            safe[name] = item
    text = json.dumps(safe, ensure_ascii=False)
    if key: text = text.replace(key, '<redacted>')
    text = re.sub(r'sk-[A-Za-z0-9_-]{8,}', '<redacted>', text)
    text = re.sub(r'https?://[^\s"<>]+', '<redacted:url>', text)
    text = re.sub(r'data:[^"\s]+', '<redacted:data>', text)
    return text[:2000]

def failed(value):
    base = value.get('base_resp')
    return bool(value.get('error')) or value.get('success') is False or value.get('type') == 'error' or (isinstance(base, dict) and base.get('status_code', 0) not in (0, '0'))
