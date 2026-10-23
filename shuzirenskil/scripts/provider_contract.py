"""Provider-specific response contracts, not model-name based inference.

Request fields and media envelopes remain in request_preview.py. New adapters
must supply documented response paths and fixtures; never recursively find an id.
"""
TASK_PATHS = {
    'aliyun': (('output', 'task_id'),),
    'minimax': (('task_id',),),
    'volcengine': (('id',),),
    'google': (('name',),),
    'fal': (('request_id',),),
    'aihge': (('request_id',),),
    # Preserve the existing gateway contract; do not propagate its aliases.
    '91topgo': tuple((k,) for k in ('task_id','request_id','id')) +
               tuple((p,k) for p in ('data','video') for k in ('task_id','request_id','id')),
}

SUCCESS = {
    'aliyun': {'succeeded'}, 'minimax': {'succeeded'},
    'volcengine': {'succeeded'}, 'google': {'succeeded'},
    'fal': {'completed'}, 'aihge': {'done'},
    '91topgo': {'completed','complete','succeeded','success','done'},
}
PENDING = {
    'aliyun': {'pending','running'}, 'minimax': {'pending','queued','running','processing'},
    'volcengine': {'queued','running'}, 'google': {'running'},
    'fal': {'in_queue','in_progress'}, 'aihge': {'pending','processing'},
    '91topgo': {'pending','queued','running','in_queue','in_progress','processing','submitted'},
}
FAILURE = {
    'aliyun': {'failed','canceled'}, 'minimax': {'failed','cancelled','canceled'},
    'volcengine': {'failed','cancelled','expired'}, 'google': {'failed'},
    'fal': {'failed','cancelled'}, 'aihge': {'failed','cancelled','canceled'},
    '91topgo': {'failed','cancelled','canceled','error'},
}

def task_identifier(provider, response):
    from provider import _first_string
    if provider not in TASK_PATHS: raise ValueError('服务商缺少编号响应合同')
    return _first_string(response, TASK_PATHS[provider])

def task_state(provider, status):
    status = str(status).lower()
    if status in SUCCESS[provider]: return 'succeeded'
    if status in FAILURE[provider]: return 'failed'
    if status in PENDING[provider]: return 'pending'
    raise ValueError('未登记的服务商任务状态；保留编号并核对该平台文档')

# Existing documented encoder paths. A new provider has no default transport.
# AIHGE only has user-reported public-image evidence: require a bound HTTPS
# image until its Data URI contract is separately documented and tested.
IMAGE_TRANSPORT = {
    '91topgo': 'jpeg_data_uri', 'aihge': 'https_required',
    'minimax': 'data_uri_or_https', 'volcengine': 'data_uri_or_https',
    'aliyun': 'data_uri_or_https', 'google': 'inline_data',
}

def image_transport(provider):
    if provider not in IMAGE_TRANSPORT:
        raise ValueError('该服务商未登记图片传输协议，禁止推测上传字段')
    return IMAGE_TRANSPORT[provider]
