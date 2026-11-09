"""Bounded, journaled provider fallback; never resubmit an unknown request."""
import json
from pathlib import Path
from _common import atomic_write_json, now_iso
from provider import ProviderError, create_task
from image_transport import local_image_data_uri


def validate_apiaw(config):
    expected = {'provider': 'apiaw', 'base_url': 'https://newapi.apiaw.com',
                'create_path': '/v1/videos', 'status_path_template': '/v1/videos/{task_id}',
                'model': 'grok-imagine-video-1.5', 'api_key_env': 'SHUZIRENSKIL_APIAW_API_KEY',
                'duration_field': 'seconds', 'duration_payload_type': 'string',
                'reference_field': 'image', 'automatic_retry_allowed': False}
    if any(config.get(k) != v for k, v in expected.items()):
        raise ValueError('APIAW 专用主机、凭据或请求协议不匹配')


def apiaw_payload(payload, provider_input, config):
    validate_apiaw(config)
    refs = provider_input.get('reference_images') if provider_input['mode'] == 'reference-to-video' else [provider_input['image']]
    if len(refs) != 1:
        raise ValueError('APIAW 仅接入单张人物图，不能静默丢弃多张参考图')
    ref = refs[0]
    if not ref.get('local_path'):
        raise ValueError('APIAW 当前只接受已绑定本地图片，禁止猜测素材协议')
    from _common import sha256_file
    path = Path(ref['local_path'])
    if sha256_file(path) != ref['sha256']:
        raise ValueError('APIAW 参考图指纹变化')
    seconds = int(payload.get('seconds', payload.get('duration', 0)))
    if seconds not in [5, 10, 15] or payload.get('resolution') != '720p' or payload.get('aspect_ratio') not in ['9:16', '16:9']:
        raise ValueError('APIAW 本次仅接入5/10/15秒720p，不能改变已确认设置')
    return {'model': config['model'], 'prompt': payload['prompt'], 'seconds': str(seconds),
            'aspect_ratio': payload['aspect_ratio'], 'resolution': payload['resolution'],
            'image': local_image_data_uri(path, max_bytes=800000)}


def definite_rejection(error):
    # Only an explicit 4xx rejection with a specific provider error; generic 5xx,
    # timeout, malformed JSON and missing IDs remain ambiguous.
    text = str(error)
    if text.startswith('Provider HTTP 403:'):
        try:
            body = json.loads(text.split(':', 1)[1])
        except (ValueError, TypeError):
            return False
        return (isinstance(body, dict) and body.get('code') == 'insufficient_user_quota'
                and body.get('data') is None and '预扣费额度失败' in body.get('message', ''))
    return (text.startswith('Provider HTTP 400:') or text.startswith('Provider HTTP 404:')) and 'model_not_found' in text and 'unknown provider for model' in text


def can_resume_rejected_fallback(job, project):
    """Resume only a journaled explicit rejection, never a merely missing ID."""
    attempts = job.get('provider_attempts', [])
    policy = project.get('production_authorization', {}).get('fallback_policy', {})
    used = {a.get('provider') for a in attempts}
    return bool(
        not job.get('task_id') and len(attempts) == 1
        and job.get('status') in {'failed', 'submission_unknown'}
        and attempts[0].get('status') in {'failed', 'submission_unknown'}
        and definite_rejection(attempts[0].get('error', ''))
        and policy.get('max_providers_per_segment') == 2
        and any(c.get('provider_id') not in used for c in policy.get('providers', []))
    )


def submit_with_fallback(job, ledger, ledger_path, config, payload, project, args,
                         get_key, check_auth, validate, terminal_failure=False):
    from generation_gates import require_no_quality_failure
    from text_policy import require_guarded_prompt
    require_no_quality_failure(args.project_dir)
    require_guarded_prompt(payload.get('prompt'))
    policy = project.get('production_authorization', {}).get('fallback_policy', {})
    from _common import digest_json
    expected = project.get('production_authorization', {}).get('fallback_policy_digest')
    if policy.get('providers') and expected != digest_json(policy):
        raise ValueError('备用授权或渠道配置已变化，停止提交')
    attempts = job.setdefault('provider_attempts', [])
    candidates = policy.get('providers', [])
    limit = min(2, policy.get('max_providers_per_segment', 1))
    if config is None and can_resume_rejected_fallback(job, project):
        attempts[-1]['previous_status'] = attempts[-1]['status']
        attempts[-1].update(status='failed', rejection_reclassified_at=now_iso())
        job['status'] = 'failed'
        atomic_write_json(ledger_path, ledger)
    if terminal_failure:
        if job.get('provider_status') not in {'failed', 'failure', 'error'}:
            raise ProviderError('取消或过期任务不自动重做')
        if not attempts:
            raise ProviderError('旧任务未记录备用授权，不自动迁移')
        attempts[-1]['status'] = 'failed'
        config = None
    while True:
        if config is None:
            used = {a['provider'] for a in attempts}
            config = next((c for c in candidates if c['provider_id'] not in used), None)
            if config is None or len(attempts) >= limit:
                raise ProviderError('已明确失败；备用渠道已用尽，不追加请求')
            if config['model'] != project.get('fallback_model', 'grok-imagine-video-1.5'):
                raise ValueError('备用模型与确认模型不一致')
            from workflow import _load_private_provider_inputs
            refs = _load_private_provider_inputs(args.project_dir, project['provider_input_digest'], allow_local=True)
            provider_input = refs[job['shot_id']]
            if config['provider_id'] == 'apiaw':
                payload = apiaw_payload(payload, provider_input, config)
            elif config['provider_id'] == '91topgo':
                from workflow import _provider_reference_payload
                references = provider_input.get('reference_images') if provider_input['mode'] == 'reference-to-video' else [provider_input['image']]
                payload = {k: payload[k] for k in ('model', 'prompt', 'aspect_ratio', 'resolution')}
                dry = json.loads((args.project_dir / 'requests' / 'dry-run' / f"{job['shot_id']}.json").read_text())['payload']
                payload['seconds'] = str(dry.get('seconds', dry.get('duration')))
                payload['reference_images'] = [_provider_reference_payload(ref, config) for ref in references]
            else:
                raise ValueError('备用服务商没有已实现的素材协议')
        validate(config)
        require_guarded_prompt(payload.get('prompt'))
        # Legacy single-provider snapshots predate provider_id. Use the same
        # resolution as workflow/_api_key; do not rewrite the signed snapshot.
        provider = str(config.get('provider_id') or config.get('provider') or 'mikuapi')
        try:
            key = get_key(config, args)
            check_auth(config, key, args.timeout)
        except (ValueError, ProviderError):
            # This is a read-only check: no video creation has been sent.
            attempts.append({'provider': provider, 'status': 'precheck_failed',
                             'video_post_sent': False, 'at': now_iso()})
            job.update(status='failed', submission_attempts=sum(a.get('video_post_sent', True) for a in attempts))
            atomic_write_json(ledger_path, ledger)
            if candidates and len(attempts) < limit:
                config = None
                continue
            raise

        if any(a['provider'] == provider for a in attempts):
            raise ProviderError('同一渠道禁止自动重复提交')
        attempt = {'provider': provider, 'status': 'submitting', 'at': now_iso()}
        attempts.append(attempt)
        job.update(status='submitting', submission_attempts=len(attempts), provider_config=config, task_id=None)
        atomic_write_json(ledger_path, ledger)
        try:
            task_id = create_task(config['base_url'], config['create_path'], key, payload, args.timeout)
        except ProviderError as exc:
            definite = definite_rejection(exc) or not exc.ambiguous
            attempt.update(status='failed' if definite else 'submission_unknown', error=str(exc))
            job.update(status=attempt['status'], error=str(exc), automatic_retry_allowed=False)
            atomic_write_json(ledger_path, ledger)
            if not definite or not candidates or len(attempts) >= limit:
                raise
            config = None
            continue
        attempt.update(status='submitted', task_id=task_id)
        job.update(status='submitted', task_id=task_id, submitted_at=now_iso())
        atomic_write_json(ledger_path, ledger)
        return
