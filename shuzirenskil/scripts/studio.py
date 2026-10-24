#!/usr/bin/env python3
"""Confirmed production projects: submit once, resume, review, revise and deliver."""
from __future__ import annotations
import argparse
import base64
import copy
import json
import math
import re
import sys
import time
from pathlib import Path
from _common import atomic_write_json, digest_json, project_lock, read_json, sha256_file
from production_plan import build_plan, _normalized
from private_env import load_private_value
from audio_timeline import slice_master
from media import extract_review_frames
from quality_contract import validate_text_review
from ad_contract import VIDEO_CHECKS
from studio_transport import Adapter, TransportError, replace_assets
from studio_assets import load_bindings, resolve, unexpired, utc_stamp
from studio_media import check_clip, context_clip, render, write_subtitles
from audio_qc import inspect_audio, tail_decision, evidence_valid
from runtime_tools import capabilities

CHECKS=('identity','wardrobe_scene','motion','product_facts','visual_text','normal_speed_playback','continuity')
AUDIO_CHECKS=('script_complete','voice_identity','voice_naturalness','lip_sync','pacing')
FINAL_CHECKS=CHECKS+AUDIO_CHECKS+('seams','advertising_claims_cta')


def number(value, label, minimum=0):
    if type(value) not in (int,float) or not math.isfinite(value) or value<minimum:
        raise ValueError(label+'必须是有效非负数字')
    return value


def config_check(config, plan):
    allowed={'provider','lip_sync','expected_dimensions','expected_fps','rates','currency','test_mode'}
    if set(config)-allowed:raise ValueError('执行配置存在未知字段；密钥须放环境变量或私密 .env')
    for entry in [config.get('provider',{})]+([config.get('lip_sync',{})] if plan['audio_plan'] or 'lip_sync' in config else []):
        if set(entry)-{'base_url','key_env'} or not re.fullmatch(r'[A-Z][A-Z0-9_]*',entry.get('key_env','')):
            raise ValueError('接口配置仅允许 base_url 和 key_env 变量名')
    dimensions=config.get('expected_dimensions')
    if not isinstance(dimensions,list) or len(dimensions)!=2 or any(type(v) is not int or not 16<=v<=8192 or v%2 for v in dimensions):
        raise ValueError('须填写服务商文档对应的 expected_dimensions=[宽,高]，且为偶数')
    a,b=map(int,plan['aspect_ratio'].split(':'))
    if abs(dimensions[0]/dimensions[1]-a/b)>0.02:raise ValueError('输出尺寸与计划画幅不符')
    standard={'480p':480,'720p':720,'768p':768,'1080p':1080}
    minimum=standard.get(plan['resolution'],1080)
    if min(dimensions)<minimum and not config.get('test_mode'):raise ValueError('输出尺寸低于已选分辨率')
    if config.get('expected_fps') is not None:number(config['expected_fps'],'预期帧率',1)
    if config.get('currency') is not None and (not isinstance(config['currency'],str) or len(config['currency'])!=3):raise ValueError('currency 需三字母币种')
    rates=config.get('rates') or {}
    if not isinstance(rates,dict) or set(rates)-{'video_per_second','lip_sync_per_second'}:raise ValueError('可选单价字段无效')
    for value in rates.values():
        if value is not None:number(value,'可选单价')
    if type(config.get('test_mode',False)) is not bool:raise ValueError('test_mode 必须为布尔值')
    if not config.get('test_mode',False):
        provider=plan['model_snapshot']['provider']
        names={'aliyun':{'DASHSCOPE_API_KEY','SHUZIRENSKIL_ALIYUN_API_KEY'},'minimax':{'MINIMAX_API_KEY','SHUZIRENSKIL_MINIMAX_API_KEY'},
               'volcengine':{'ARK_API_KEY','SHUZIRENSKIL_ARK_API_KEY'},'google':{'GEMINI_API_KEY','GOOGLE_API_KEY'},
               'aihge':{'SHUZIRENSKIL_AIHGE_API_KEY'},'91topgo':{'SHUZIRENSKIL_91TOPGO_API_KEY'}}
        if config['provider']['key_env'] not in names.get(provider,set()):raise ValueError('请使用服务商专用密钥变量，不能混用不同服务商的 Key')
        if plan['audio_plan'] and config['lip_sync']['key_env'] not in {'FAL_KEY','SHUZIRENSKIL_FAL_KEY'}:raise ValueError('口型接口须使用 FAL 专用密钥')
    Adapter(plan['model_snapshot']['provider'],config['provider'],'not-a-key',loopback=config.get('test_mode',False))
    if plan['audio_plan']:Adapter('fal',config['lip_sync'],'not-a-key',loopback=config.get('test_mode',False))


def prepare(spec_path, config_path, directory):
    if directory.exists():raise ValueError('项目目录已存在，请选择新目录以保留历史')
    plan=build_plan(read_json(spec_path),spec_path.resolve().parent)
    if any(j['request_preview']['payload'] is None for j in plan['jobs']):raise ValueError('此模型没有新执行适配器；请使用其旧入口或选择已接入模型')
    config=read_json(config_path);config_check(config,plan)
    contract={'plan_digest':plan['plan_digest'],'config_digest':digest_json(config),'version':'studio-1.1-tail-qc'}
    contract['digest']=digest_json(contract)
    directory.mkdir(parents=True)
    for name,obj in [('plan',plan),('config',config),('contract',contract)]:atomic_write_json(directory/(name+'.json'),obj)
    state={'version':'studio-1.0','contract_digest':contract['digest'],'created_at':utc_stamp(),'jobs':{},'attempts':[],
           'approvals':[],'revisions':[],'final':None,'status':'awaiting_authorization'}
    for job in plan['jobs']:
        state['jobs'][job['job_id']]={'state':'planned','revision':0,'phase':'video','task':None,'clip':None}
    atomic_write_json(directory/'state.json',state)
    template={'contract_digest':contract['digest'],'approved_by':'','expires_at':'','images_reviewed':False,
              'script_and_voice_approved':False,'asset_upload_approved':False,'audio_rights_confirmed':False,
              'unverified_provider_pilot_approved':False,'max_attempts':0,'max_request_seconds':0}
    atomic_write_json(directory/'approval-template.json',template)
    return {'status':'awaiting_authorization','contract_digest':contract['digest'],'project':str(directory.resolve()),
            'video_requests':len(plan['jobs']),'lip_sync_requests':sum(j['requires_lip_sync'] for j in plan['jobs']),
            'note':'按用户已有制作确认记录模型、素材、声音和请求范围；无需报价、估费或单独费用确认'}


class Studio:
    def __init__(self,directory,env_file=None,bindings=None):
        self.root=Path(directory).resolve();self.env_file=env_file;self.bindings_path=bindings
        self.plan=read_json(self.root/'plan.json');self.config=read_json(self.root/'config.json')
        self.contract=read_json(self.root/'contract.json');self.state=read_json(self.root/'state.json')
        plan_body={k:v for k,v in self.plan.items() if k!='plan_digest'}
        contract_body={k:v for k,v in self.contract.items() if k!='digest'}
        if (digest_json(plan_body)!=self.plan['plan_digest'] or digest_json(contract_body)!=self.contract['digest']
                or self.contract['plan_digest']!=self.plan['plan_digest'] or self.contract['config_digest']!=digest_json(self.config)
                or self.state['contract_digest']!=self.contract['digest']):raise ValueError('项目合同或计划已改变，必须新建项目重新确认')
        config_check(self.config,self.plan)
        self.loopback=self.config.get('test_mode',False)

    def save(self):atomic_write_json(self.root/'state.json',self.state)
    def job(self,ident):
        found=next((j for j in self.plan['jobs'] if j['job_id']==ident),None)
        if not found:raise ValueError('未知片段 ID')
        return found,self.state['jobs'][ident]
    def assets_unchanged(self):
        for item in self.plan['assets'].values():
            if sha256_file(Path(item['path']))!=item['sha256']:raise ValueError('确认素材已经改变，需重新建立计划')
    def adapter(self,phase):
        config=self.config['lip_sync' if phase=='lip' else 'provider']
        key=load_private_value(config['key_env'],self.env_file,self.root,keychain_service='shuzirenskil-91topgo-video' if phase=='video' and self.plan['model_snapshot']['provider']=='91topgo' else None,prefer_keychain=True)
        if not key:raise ValueError('接口密钥为空')
        return Adapter('fal' if phase=='lip' else self.plan['model_snapshot']['provider'],config,key,loopback=self.loopback)
    def preflight(self):
        self.assets_unchanged()
        if self.plan.get('review_mode') == 'continuous_candidate' and not self.loopback:
            from production_review import require_runtime
            require_runtime()
        if not self.loopback:
            from media import probe
            provider=self.plan['model_snapshot']['provider']
            for asset in self.plan['assets'].values():
                if asset['kind']!='image':continue
                metadata=probe(Path(asset['path']))
                stream=next(s for s in metadata['streams'] if s['codec_type']=='video')
                width,height=stream['width'],stream['height']
                if provider=='aliyun' and (min(width,height)<240 or max(width,height)>8000 or max(width/height,height/width)>8 or asset['size_bytes']>20*1024*1024):
                    raise ValueError('Wan 参考图超出尺寸或大小限制')
                if provider=='minimax' and (min(width,height)<256 or max(width,height)>5760 or not 0.4<=width/height<=2.5 or asset['size_bytes']>30*1024*1024):
                    raise ValueError('MiniMax 参考图超出尺寸或大小限制')
        for phase in ['video']+(['lip'] if self.plan['audio_plan'] else []):self.adapter(phase)
        return {'status':'local_preflight_passed','network_calls':0,'account_entitlement_verified':False,
                'local_quality_capabilities':capabilities() if not self.loopback else {'synthetic_test':True},
                'note':'本地参数和密钥存在性通过，不代表账号额度、模型权限或真实效果通过'}

    def authorize(self,evidence):
        if evidence.get('contract_digest')!=self.contract['digest']:raise ValueError('授权不匹配当前合同')
        if not isinstance(evidence.get('approved_by'),str) or not evidence['approved_by'].strip():raise ValueError('缺少真实批准者')
        if not unexpired(evidence.get('expires_at')):raise ValueError('授权缺少有效到期时间')
        required=['images_reviewed','script_and_voice_approved','asset_upload_approved','unverified_provider_pilot_approved']
        if self.plan['voice']['mode']!='model_generated':required.append('audio_rights_confirmed')
        if any(evidence.get(k) is not True for k in required):raise ValueError('素材、声音、上传或首次模型试片尚未获得明确确认')
        if type(evidence.get('max_attempts')) is not int or evidence['max_attempts']<1:raise ValueError('须明确最多付费请求次数')
        number(evidence.get('max_request_seconds'),'max_request_seconds')
        # Monetary caps are opt-in only; ordinary production needs no quote.
        self.check_cost_cap(evidence)
        # New approval is cumulative and never resets already reserved requests.
        attempts=self.state['attempts']
        if len(attempts)>evidence['max_attempts'] or sum(x['seconds'] for x in attempts)>evidence['max_request_seconds']:raise ValueError('累计授权不能低于已预留用量')
        self.assets_unchanged();self.state['approvals'].append(copy.deepcopy(evidence));self.state['status']='authorized';self.save()
        return {'status':'authorized','approval_number':len(self.state['approvals'])}
    def current_approval(self):
        if not self.state['approvals'] or not unexpired(self.state['approvals'][-1]['expires_at']):raise ValueError('没有有效制作授权')
        return self.state['approvals'][-1]
    def estimate_cost(self,phase,seconds,input_seconds=0):
        rate=(self.config.get('rates') or {}).get('lip_sync_per_second' if phase=='lip' else 'video_per_second')
        # Old example zero rates mean unknown, never free generation.
        if rate is None or rate<=0 or not self.config.get('currency'):return None
        return (seconds+input_seconds)*rate
    def check_cost_cap(self,approval,additional=None):
        if approval.get('max_cost') is None:return
        number(approval['max_cost'],'用户主动指定的金额上限')
        if not self.config.get('currency') or approval.get('currency')!=self.config['currency']:raise ValueError('用户金额上限的币种与配置不匹配')
        phases=['video']+(['lip'] if self.plan['audio_plan'] else [])
        costs=[x.get('reserved_cost') for x in self.state['attempts']]+([] if additional is None else [additional])
        if any(self.estimate_cost(p,1) is None for p in phases) or any(c is None for c in costs):
            raise ValueError('用户主动设置了金额上限，但现有单价或历史金额不足以核验；不能声称满足此限制')
        if sum(costs)>approval['max_cost']+1e-9:raise ValueError('本次请求将超过用户主动指定的累计金额上限')
    def reserve(self,job,phase):
        approval=self.current_approval();seconds=job.get('use_seconds',job['request_seconds']) if phase=='lip' else job['request_seconds']
        input_seconds=2 if phase=='video' and self.plan['continuity_mode']=='continue' and job['index']>1 else 0
        cost=self.estimate_cost(phase,seconds,input_seconds)
        self.check_cost_cap(approval,cost)
        attempts=self.state['attempts']
        if len(attempts)+1>approval['max_attempts'] or sum(x['seconds'] for x in attempts)+seconds>approval['max_request_seconds']+1e-6:raise ValueError('本次请求将超过累计次数或秒数上限')
        entry={'job_id':job['job_id'],'phase':phase,'seconds':seconds,'input_video_seconds':input_seconds,'reserved_cost':cost,'created_at':utc_stamp(),
               'approval_number':len(self.state['approvals']),'state':'submitting'}
        attempts.append(entry);return entry
    def verify_accepted(self,ident):
        job,record=self.job(ident)
        if record['state'] == 'production_checked':
            if self.plan.get('review_mode') != 'continuous_candidate':raise ValueError('未授权连续候选制作')
            from production_review import validate
            result = validate(record['production_review'], Path(record['clip']), job['script'], self.contract['digest'])
            if job['index'] > 1 and record['production_review'].get('baseline_sha256') != self.verify_accepted(self.plan['jobs'][0]['job_id'])['clip_sha256']:
                raise ValueError('继续制作观察与当前首段基准不一致')
            if (digest_json(record['production_review']) != record.get('production_review_digest')
                    or digest_json(result['tail_edit']) != record.get('tail_edit_digest')
                    or digest_json(record.get('tail_edit')) != record.get('tail_edit_digest')
                    or result['cues'] != record.get('machine_cues')):
                raise ValueError('继续制作证据已变化')
            return record
        if record['state']!='accepted' or not record.get('review'):raise ValueError('前置片段尚未完整验收')
        if sha256_file(Path(record['clip']))!=record['clip_sha256']:raise ValueError('已验收片段已经变化')
        if digest_json(record['review'])!=record.get('review_digest'):
            raise ValueError('已验收记录已经变化，需要重新验收')
        if record['review'].get('video_sha256')!=record['clip_sha256']:raise ValueError('验收不匹配视频')
        if record.get('tail_edit') and (digest_json(record['tail_edit'])!=record.get('tail_edit_digest') or record['tail_edit']['source_sha256']!=record['clip_sha256']):
            raise ValueError('裁尾证据已经变化，需要重新检查')
        return record
    def _payload(self,job,record,phase):
        bindings=load_bindings(self.bindings_path);profile=self.plan['model_snapshot']
        if phase=='lip':
            if not record.get('base_review') or sha256_file(Path(record['base_clip']))!=record['base_sha256']:raise ValueError('口型处理的基础视频未验收或已变化')
            audio_dir=self.root/'audio'
            if not audio_dir.exists():
                slice_master(self.plan['audio_plan'],audio_dir)
                self.state['audio_manifest_sha256']=sha256_file(audio_dir/'audio-manifest.json');self.save()
            if sha256_file(audio_dir/'audio-manifest.json')!=self.state.get('audio_manifest_sha256'):
                raise ValueError('音频切片清单变化或未完成绑定，请核对本地主音轨切片')
            manifest=read_json(audio_dir/'audio-manifest.json');audio=manifest['slices'][job['index']-1]
            return {'model':'lipsync-2','sync_mode':'cut_off',
                    'video_url':resolve(record['base_clip'],record['base_sha256'],bindings,loopback=self.loopback),
                    'audio_url':resolve(audio['path'],audio['sha256'],bindings,loopback=self.loopback)}
        payload=copy.deepcopy(job['request_preview']['payload']);serialized=json.dumps(payload)
        mapping={}
        for item in self.plan['assets'].values():
            token='asset://'+item['sha256']
            if token not in serialized:continue
            from provider_contract import image_transport
            transport = image_transport(profile['provider']) if item['kind']=='image' else None
            if transport=='jpeg_data_uri':
                from image_transport import local_image_data_uri
                mapping[token]=local_image_data_uri(Path(item['path']));continue
            mapping[token]=resolve(item['path'],item['sha256'],bindings,
                require_url=(profile['provider']=='aliyun' and item['kind']=='audio') or transport=='https_required',loopback=self.loopback)
            if mapping[token].startswith('asset://asset-') and profile['provider']!='volcengine':raise ValueError('人像资产 ID 仅适用于 Seedance')
        if profile['provider']=='google':
            for ref in payload['instances'][0]['referenceImages']:
                token=ref['image'].pop('asset_token');asset=next(x for x in self.plan['assets'].values() if 'asset://'+x['sha256']==token)
                # Gemini REST accepts inlineData. Avoid handing it an arbitrary reference URL.
                from studio_transport import data_uri
                uri=data_uri(asset['path']);header,data=uri.split(',',1)
                ref['image']['inlineData']={'mimeType':header[5:].split(';')[0],'data':data}
        else:payload=replace_assets(payload,mapping)
        if self.plan['continuity_mode']=='continue' and job['index']>1:
            previous=self.verify_accepted(self.plan['jobs'][job['index']-2]['job_id'])
            tail=self.root/f"context-{job['job_id']}-r{record['revision']}.mp4"
            context=context_clip(Path(previous['clip']),tail,end_seconds=previous.get('tail_edit',{}).get('end_seconds'))
            uri=resolve(context['path'],context['sha256'],bindings,require_url=profile['provider']=='aliyun',loopback=self.loopback)
            note='参考视频为上一段已验收结尾，只生成其后新的画面，不重播参考片段。原始参考图仍是人物身份基准。'
            if profile['provider']=='aliyun':
                payload['input']['media'].append({'type':'reference_video','url':uri});payload['input']['prompt']+='\n'+note
            else:
                payload['content'].append({'type':'video_url','video_url':{'url':uri},'role':'reference_video'})
                payload['content'][0]['text']+='\n'+note
            record['context']={**context,'previous_video_sha256':previous['clip_sha256']}
        return payload
    def context(self,ident):
        job,record=self.job(ident)
        if self.plan['continuity_mode']!='continue' or job['index']==1:raise ValueError('此片段不需要续接上下文')
        previous=self.verify_accepted(self.plan['jobs'][job['index']-2]['job_id'])
        return context_clip(Path(previous['clip']),self.root/f"context-{ident}-r{record['revision']}.mp4",end_seconds=previous.get('tail_edit',{}).get('end_seconds'))
    def submit(self,ident):
        job,record=self.job(ident);phase=record['phase']
        if record['state'] not in {'planned','lip_ready'}:raise ValueError('片段已提交或待验收；不能自动重新下单')
        self.current_approval();self.assets_unchanged()
        if job['index']>1:self.verify_accepted(self.plan['jobs'][0]['job_id'])
        # Validate prerequisites before uploads and before the durable paid reservation.
        if self.plan['continuity_mode']=='continue' and job['index']>1:self.verify_accepted(self.plan['jobs'][job['index']-2]['job_id'])
        self.preflight()
        adapter=self.adapter(phase)
        try:
            payload=self._payload(job,record,phase)
        except (ValueError, TransportError):
            record['pre_submit_diagnostic']={'stage':'asset_preparation','video_post_sent':False}
            self.save()
            raise
        record.pop('pre_submit_diagnostic',None)
        # Serialize before reservation so local type/body errors do not create uncertain paid tasks.
        if len(json.dumps(payload,ensure_ascii=False).encode())>64*1024*1024:raise ValueError('请求体超过 64 MiB')
        attempt=self.reserve(job,phase);record['state']='submitting';record['attempt_index']=len(self.state['attempts'])-1
        attempt['request_digest']=digest_json(payload);self.save()
        try:
            task=adapter.create(payload)
            attempt['http_diagnostic']=adapter.last_diagnostic
            record['task']=task;record['state']='submitted';attempt['state']='submitted';attempt['task_id']=task['id']
        except Exception as exc:
            record['state']='submission_unknown';attempt['state']='submission_unknown'
            attempt['http_diagnostic']=getattr(exc, 'diagnostic', {}) if isinstance(exc, TransportError) else {}
            attempt['diagnostic']=str(exc)[:2000] if isinstance(exc, TransportError) else '本地异常；详情未保存以避免泄露凭据'
            self.save()
            raise TransportError('提交结果未确认，已保留脱敏诊断；禁止重复提交；' + attempt['diagnostic']) from None
        self.save();return {'status':'submitted','job_id':ident,'phase':phase,'task_id':task['id']}
    def recover(self,ident,evidence):
        job,record=self.job(ident)
        if record['state'] not in {'submitting','submission_unknown'}:raise ValueError('只有未知提交可以恢复任务 ID')
        attempt=self.state['attempts'][record['attempt_index']]
        if (evidence.get('contract_digest')!=self.contract['digest'] or evidence.get('request_digest')!=attempt['request_digest']
            or evidence.get('provider_console_checked') is not True or not evidence.get('approved_by')):
            raise ValueError('恢复需要绑定合同、请求指纹并确认已在服务商核对该任务')
        task=evidence['task']
        result=self.adapter(record['phase']).poll(task)
        record['recovery_evidence']=evidence
        record['task']=task;record['state']='submitted';self.state['attempts'][record['attempt_index']].update(state='submitted',task_id=task['id'])
        self.save();return {'status':'recovered','remote_state':result['state']}
    def poll(self,ident):
        job,record=self.job(ident)
        if record['state']!='submitted':raise ValueError('只能查询已记录的任务；未知提交应先恢复')
        adapter=self.adapter(record['phase']);result=adapter.poll(record['task'])
        if result['state']=='pending':return {'status':'pending'}
        attempt=self.state['attempts'][record['attempt_index']]
        if result['state']=='failed':
            record['state']='failed';attempt['state']='failed';self.save();return {'status':'failed','retry_allowed':False}
        path=self.root/'clips'/f"{ident}-r{record['revision']}-{record['phase']}.mp4"
        # A download failure retains submitted task; a later poll downloads the SAME job only.
        adapter.download(result['url'],path)
        record['clip']=str(path);record['clip_sha256']=sha256_file(path)
        try:record['technical']=check_clip(path,job,self.plan,self.config,lip=record['phase']=='lip')
        except ValueError:
            record['state']='technical_failed';attempt['state']='technical_failed';self.save();raise
        record['state']='base_review_pending' if job['requires_lip_sync'] and record['phase']=='video' else 'review_pending'
        attempt['state']='downloaded';self.save();return {'status':record['state'],'clip':str(path)}
    def inspect(self,ident):
        final=ident=='final'
        if final:
            if not self.state.get('final'):raise ValueError('尚未合成全片')
            video=Path(self.state['final']['video']);sha=self.state['final']['video_sha256'];checks=FINAL_CHECKS
        else:
            job,record=self.job(ident)
            if record['state'] not in {'base_review_pending','review_pending'}:raise ValueError('此片段尚未准备好验收')
            video=Path(record['clip']);sha=record['clip_sha256']
            checks=CHECKS + (AUDIO_CHECKS if record['state']=='review_pending' and job['kind']=='talking_head' else ())
        if self.plan.get('ad_contract'):checks=tuple(dict.fromkeys(checks+VIDEO_CHECKS))
        if sha256_file(video)!=sha:raise ValueError('视频已经改变')
        folder=self.root/'reviews'/f'{ident}-{sha[:16]}';extract_review_frames(video,folder)
        script = ''.join(item['script'] for item in self.plan['jobs']) if final else job['script']
        audio_file=folder/'audio-qc.json'
        audio_report=read_json(audio_file) if audio_file.exists() else None
        if not audio_report or not evidence_valid(audio_report,video,script):
            audio_report=inspect_audio(video,folder,script,transcribe=not self.loopback)
        if not final:
            record['audio_qc_file']=str(audio_file);record['audio_qc_sha256']=sha256_file(audio_file)
            self.save()
        manifest=folder/'manifest.json'
        template={'contract_digest':self.contract['digest'],'video_sha256':sha,'reviewer':None,'review_seconds':0,
                  'full_playback':False,'checks':{k:{'status':'pending','observations':''} for k in checks},
                  'audio_qc_file':str(audio_file), 'tail_action':'auto',
                  'transcript':'','cues':[], 'text_review':{'manifest_file':str(manifest),'manifest_sha256':sha256_file(manifest),
                    'reviewer':None,'full_video_viewed':False,'all_sampled_frames_viewed':False,'last_frame_viewed':False,
                    'text_present':None,'observations':''}}
        if not final and job['index']>1:
            template['baseline_sha256']=self.verify_accepted(self.plan['jobs'][0]['job_id'])['clip_sha256']
        if final:
            template['all_segments_and_seams_reviewed']=False
        ad=self.plan.get('ad_contract')
        if ad and ad['source_text']:template['text_review'].update(source_text_digest=digest_json(ad['source_text']),source_text_matches='pending',unapproved_text_present=None)
        target=folder/'review-template.json'
        if not target.exists():atomic_write_json(target,template)
        return {'review_template':str(target),'video':str(video),'checks':list(checks),
                'audio_qc_file':str(audio_file),'automatic_audio_status':audio_report['status'],
                'next_action':'由具备真实看听能力的 AI 完成待检项并继续；不是再次请求制作授权。确实缺能力时只说明缺项。'}
    def review(self,ident,evidence):
        final=ident=='final';job=None
        if final:
            record=self.state.get('final')
            if not record:raise ValueError('尚未合成')
            video=Path(record['video']);sha=record['video_sha256'];checks=FINAL_CHECKS
        else:
            job,record=self.job(ident)
            if record['state'] not in {'base_review_pending','review_pending'}:raise ValueError('此阶段不能提交验收')
            video=Path(record['clip']);sha=record['clip_sha256']
            checks=CHECKS+(AUDIO_CHECKS if record['state']=='review_pending' and job['kind']=='talking_head' else ())
        if self.plan.get('ad_contract'):checks=tuple(dict.fromkeys(checks+VIDEO_CHECKS))
        if evidence.get('contract_digest')!=self.contract['digest'] or evidence.get('video_sha256')!=sha or sha256_file(video)!=sha:
            raise ValueError('验收未绑定当前合同和当前视频')
        if job and job['index']>1 and evidence.get('baseline_sha256')!=self.verify_accepted(self.plan['jobs'][0]['job_id'])['clip_sha256']:
            raise ValueError('验收未绑定当前首段的人物与声音基准')
        if evidence.get('reviewer') not in {'human_user','agent_audiovisual'} or evidence.get('full_playback') is not True:raise ValueError('需要实际完整播放及看听检查')
        number(evidence.get('review_seconds'),'实际验收用时',1)
        review_items=[evidence.get('checks',{}).get(k,{}) for k in checks]
        if all(isinstance(item,dict) and item.get('status') in {'pass','fail'} and isinstance(item.get('observations'),str) and len(item['observations'].strip())>=8 for item in review_items):
            self.state.setdefault('review_events',[]).append({'job_id':ident,'video_sha256':sha,'at':utc_stamp(),
                'stage':'final' if final else record['state'],'declared_checks_passed':all(i['status']=='pass' for i in review_items),
                'review_seconds':evidence['review_seconds'],'failed_checks':[k for k in checks if evidence['checks'][k]['status']=='fail']})
            self.save()
        for key in checks:
            item=evidence.get('checks',{}).get(key,{})
            if not isinstance(item,dict) or item.get('status')!='pass' or not isinstance(item.get('observations'),str) or len(item['observations'].strip())<8:
                raise ValueError('验收未明确通过：'+key)
        validate_text_review(evidence,video,{'video_prompt_spec':{'ad_contract':self.plan.get('ad_contract')},'settings':{'purpose':self.plan['purpose']}})
        if job and record['state']=='review_pending' and job['native_audio_policy']=='retain_and_review':
            if _normalized(evidence.get('transcript',''))!=_normalized(job['script']):raise ValueError('实际听写台词与确认稿不符')
            # Cues are required here so final subtitles never use estimated speech times.
            temporary=self.root/'reviews'/f'{ident}-{sha[:16]}.srt'
            write_subtitles(evidence.get('cues'),record['technical']['seconds'],job['script'],temporary)
        edit=None
        if job and record['state']=='review_pending' and self.contract['version']=='studio-1.1-tail-qc':
            action=evidence.get('tail_action','auto')
            if action not in {'auto','preserve'}:raise ValueError('tail_action 只能为 auto 或 preserve')
            if action=='preserve' and len(evidence.get('tail_observations','').strip())<8:
                raise ValueError('保留有意设计的结束镜头须记录具体观察')
            audio_report=None
            if record.get('audio_qc_file'):
                audio_path=Path(record['audio_qc_file'])
                if sha256_file(audio_path)!=record['audio_qc_sha256']:raise ValueError('自动音频报告已变化，请重新 inspect')
                audio_report=read_json(audio_path)
            edit=tail_decision(video,job['script'],evidence.get('cues'),audio_report,
                preserve=bool(self.plan['audio_plan']) or job['kind']!='talking_head' or job['native_audio_policy']!='retain_and_review' or action=='preserve')
        if final:
            if evidence.get('all_segments_and_seams_reviewed') is not True:raise ValueError('全片与全部接缝尚未验收')
            record['review']=evidence;record['review_digest']=digest_json(evidence);record['business_accepted']=True;record['status']='accepted';self.state['status']='delivered'
        elif record['state']=='base_review_pending':
            record.update(base_clip=record['clip'],base_sha256=sha,base_review=evidence,state='lip_ready',phase='lip',task=None)
        else:
            record.update(review=evidence,review_digest=digest_json(evidence),state='accepted')
            if edit:record.update(tail_edit=edit,tail_edit_digest=digest_json(edit))
        self.save();return {'status':record.get('state',record.get('status'))}
    def review_production(self, ident, evidence):
        if self.plan.get('review_mode') != 'continuous_candidate':raise ValueError('计划未确认连续候选制作；历史项目不能自动变更审核方式')
        job, record = self.job(ident)
        if record['state'] != 'review_pending':raise ValueError('当前片段没有待执行的继续制作检查')
        if any(event.get('job_id') == ident and event.get('video_sha256') == record['clip_sha256'] and event.get('declared_checks_passed') is False for event in self.state.get('review_events', [])):
            raise ValueError('同一原片已有明确失败的实际验收，不能改成主观待检继续')
        from production_review import validate
        result = validate(evidence, Path(record['clip']), job['script'], self.contract['digest'])
        if job['index'] > 1 and evidence.get('baseline_sha256') != self.verify_accepted(self.plan['jobs'][0]['job_id'])['clip_sha256']:
            raise ValueError('继续制作观察未绑定当前首段基准')
        record.update(state='production_checked', production_review=evidence,
            production_review_digest=digest_json(evidence), tail_edit=result['tail_edit'],
            tail_edit_digest=digest_json(result['tail_edit']), machine_cues=result['cues'],
            perceptual_review='pending', business_accepted=False, low_confidence_words=result['low_confidence_words'])
        self.save()
        return {'status':'production_checked','business_accepted':False,'next_action':'继续 advance，不询问下一段或拼接许可'}
    def revise(self,ident,evidence):
        job,record=self.job(ident);approval=self.current_approval()
        if record['state'] in {'submitted','submitting','submission_unknown','planned','lip_ready'}:raise ValueError('任务未收敛，不能重复生成；先恢复或验收已有任务')
        if (evidence.get('contract_digest')!=self.contract['digest'] or evidence.get('job_id')!=ident
            or evidence.get('video_sha256')!=record.get('clip_sha256') or evidence.get('approved_by')!=approval['approved_by']
            or not isinstance(evidence.get('reason'),str) or len(evidence['reason'].strip())<8):raise ValueError('重做须明确绑定片段、原视频、批准者和原因')
        used=max((a['approval_number'] for a in self.state['attempts'] if a['job_id']==ident),default=0)
        if len(self.state['approvals'])<=used:raise ValueError('重做需要用户确认新的制作范围，不能复用上次试片授权；无需报价')
        downstream=[]
        if self.plan['continuity_mode']=='continue':
            downstream=[j['job_id'] for j in self.plan['jobs'] if j['index']>job['index']]
            if any(self.state['jobs'][k]['state'] in {'submitted','submitting','submission_unknown'} for k in downstream):raise ValueError('下游仍有未收敛任务，请先查询并保存结果')
        self.state['revisions'].append({'at':utc_stamp(),'evidence':evidence,'record':copy.deepcopy(record),'invalidated':downstream})
        for key in [ident]+downstream:
            old=self.state['jobs'][key]
            if key!=ident:self.state['revisions'].append({'at':utc_stamp(),'reason':'upstream_revised','record':copy.deepcopy(old),'job_id':key})
            self.state['jobs'][key]={'state':'planned','phase':'video','task':None,'clip':None,'revision':old['revision']+1}
        if self.plan['continuity_mode']=='cut' and job['index']==1:
            for other in self.plan['jobs'][1:]:
                old=self.state['jobs'][other['job_id']]
                if old['state'] in {'accepted','production_checked'}:
                    old['prior_reviews']=old.get('prior_reviews',[])+[old.get('review') or old.get('production_review')]
                    old['state']='review_pending';old.pop('review',None);old.pop('review_digest',None)
        if self.state['final']:
            self.state.setdefault('final_history',[]).append(self.state['final']);self.state['final']=None
        self.state['status']='authorized';self.save();return {'status':'revision_planned','invalidated':downstream}
    def assemble(self):
        self.assets_unchanged();clips=[];edits=[]
        for job in self.plan['jobs']:
            record=self.verify_accepted(job['job_id']);clips.append(Path(record['clip']))
            # Historical contracts keep their timeline until explicitly re-reviewed/migrated.
            edits.append(record.get('tail_edit') or tail_decision(Path(record['clip']),preserve=True))
        folder=self.root/'outputs'/f'render-{time.time_ns()}'
        result=render(self.plan,clips,folder,edits=edits)
        target=self.plan.get('target_seconds')
        if target and self.plan.get('duration_mode') != 'reference' and not self.plan['audio_plan']:
            from studio_media import info
            tolerance=1/info(clips[0])['fps']+1e-6
            if ((self.plan.get('duration_mode')=='exact' and abs(result['duration_seconds']-target)>tolerance)
                or result['duration_seconds']>target+tolerance):
                raise ValueError('实际合成时长与用户目标不符，不能交付或自动裁剪台词')
        if self.plan['audio_plan']:cues=self.plan['audio_cues']['cues']
        else:
            cues=[]
            for job,segment in zip(self.plan['jobs'],result['timeline']):
                record=self.state['jobs'][job['job_id']]
                review=record.get('review') or {'cues':record.get('machine_cues',[])}
                for cue in review.get('cues',[]) if job['kind']=='talking_head' else []:
                    cues.append({**cue,'start_seconds':cue['start_seconds']+segment['start_seconds'],'end_seconds':cue['end_seconds']+segment['start_seconds']})
        script=''.join(job['script'] for job in self.plan['jobs'])
        write_subtitles(cues,max(result['duration_seconds'],self.plan.get('audio_plan',{}).get('delivery_audio_seconds',0) if self.plan.get('audio_plan') else 0),script,folder/'subtitles.srt',machine_candidate=self.plan.get('review_mode')=='continuous_candidate')
        if result.get('lossless_audio_video'):
            result['additional_file_hashes']={str(p):sha256_file(p) for p in [Path(result['lossless_audio_video']),folder/'master.wav']}
        result['subtitles']=str(folder/'subtitles.srt');result['subtitles_sha256']=sha256_file(folder/'subtitles.srt')
        if self.state['final']:self.state.setdefault('final_history',[]).append(self.state['final'])
        self.state['final']=result;self.state['status']='awaiting_final_review';self.save();return result
    def status(self):
        attempts=self.state['attempts'];reviews=self.state.get('review_events',[])
        pilot=[r for r in reviews if r['job_id']==self.plan['jobs'][0]['job_id'] and r['stage']=='review_pending']
        return {'status':self.state['status'],'jobs':{k:v['state'] for k,v in self.state['jobs'].items()},
                'attempts_reserved':len(attempts),'seconds_reserved':sum(x['seconds'] for x in attempts),
                'cost_reserved':sum(x['reserved_cost'] for x in attempts) if attempts and all(x.get('reserved_cost') is not None for x in attempts) else None,'currency':self.config.get('currency'),
                'provider_billed_cost':'未查询；不影响已授权制作，不代表免费','revisions':len(self.state['revisions']),
                'review_seconds':sum(x.get('review_seconds',0) for x in reviews),'pilot_first_review_checks_passed':pilot[0]['declared_checks_passed'] if pilot else None,'test_mode':self.loopback,
                'real_media_quality_verified':False if self.loopback else self.state['status']=='delivered'}
    def deliver(self):
        final=self.state.get('final')
        self.assets_unchanged()
        if self.state['status']!='delivered' or not final or not final.get('business_accepted'):raise ValueError('全片技术检查与实际看听、商业验收尚未全部通过')
        if digest_json(final['review'])!=final.get('review_digest'):raise ValueError('最终验收记录已经变化')
        for job in self.plan['jobs']:self.verify_accepted(job['job_id'])
        if sha256_file(Path(final['video']))!=final['video_sha256'] or sha256_file(Path(final['subtitles']))!=final['subtitles_sha256']:raise ValueError('交付文件已经变化')
        files=[{'path':final['video'],'sha256':final['video_sha256']},{'path':final['subtitles'],'sha256':final['subtitles_sha256']}]
        if final.get('lossless_audio_video'):
            for path in [Path(final['lossless_audio_video']),Path(final['video']).parent/'master.wav']:
                expected=final.get('additional_file_hashes',{}).get(str(path))
                if sha256_file(path)!=expected:raise ValueError('无损交付文件已经变化')
                files.append({'path':str(path),'sha256':expected})
        result={'status':'synthetic_test_delivery' if self.loopback else 'delivered','contract_digest':self.contract['digest'],
                'files':files,'metrics':self.status(),'review':final['review'],'advertising_performance':'需实际投放数据验证'}
        atomic_write_json(self.root/'delivery.json',result);return result

    def advance(self,review_dir=None):
        """Advance existing authorization to the next genuine external/review boundary."""
        review_dir=Path(review_dir).resolve() if review_dir else self.root/'completed-reviews'
        for _ in range(len(self.plan['jobs'])*8+8):
            pending=next((j for j in self.plan['jobs'] if self.state['jobs'][j['job_id']]['state'] not in {'accepted','production_checked'}),None)
            if pending:
                ident=pending['job_id'];record=self.state['jobs'][ident];state=record['state']
                if state in {'planned','lip_ready'}:
                    self.submit(ident)
                    continue
                if state=='submitted':
                    result=self.poll(ident)
                    if result['status']=='pending':return {'status':'waiting_provider','job_id':ident,'next_action':'稍后再次 advance；只查询原任务，不增加确认或重复提交'}
                    continue
                if state in {'review_pending','base_review_pending'}:
                    if self.plan.get('review_mode') == 'continuous_candidate' and state == 'review_pending':
                        evidence=review_dir/f"production-{ident}-{record['clip_sha256'][:16]}.json"
                        if evidence.is_file():
                            self.review_production(ident,read_json(evidence));continue
                        from production_review import inspect
                        result=inspect(Path(record['clip']),pending['script'],self.root/'production-qc'/ident,self.contract['digest'])
                        if pending['index'] > 1:
                            path=Path(result['production_review_template']);template=read_json(path)
                            template['baseline_sha256']=self.verify_accepted(self.plan['jobs'][0]['job_id'])['clip_sha256']
                            atomic_write_json(path,template)
                        return {**result,'job_id':ident,'evidence_destination':str(evidence)}
                    evidence=review_dir/f"{ident}-{record['clip_sha256'][:16]}.json"
                    if evidence.is_file():
                        self.review(ident,read_json(evidence))
                        continue
                    return {'status':'awaiting_agent_review','job_id':ident,'evidence_destination':str(evidence),**self.inspect(ident)}
                return {'status':'blocked','job_id':ident,'reason':state,'retry_allowed':False}
            if not self.state.get('final'):
                self.assemble()
                continue
            if self.state['final'].get('business_accepted'):return self.deliver()
            sha=self.state['final']['video_sha256']
            evidence=review_dir/f'final-{sha[:16]}.json'
            if evidence.is_file():
                self.review('final',read_json(evidence))
                continue
            return {'status':'awaiting_agent_review','job_id':'final','evidence_destination':str(evidence),**self.inspect('final')}
        raise ValueError('推进次数超过预期，请检查状态；未自动重试付费任务')


def main():
    parser=argparse.ArgumentParser(description='多模型生产执行；一次提交、显式授权、逐段验收')
    parser.add_argument('command',choices=['prepare','authorize','submit','poll','recover','context','inspect','review','review-production','revise','assemble','status','deliver','preflight','advance'])
    parser.add_argument('--project',type=Path,required=True);parser.add_argument('--spec',type=Path);parser.add_argument('--config',type=Path)
    parser.add_argument('--job');parser.add_argument('--evidence',type=Path);parser.add_argument('--env-file',type=Path);parser.add_argument('--bindings',type=Path)
    parser.add_argument('--review-dir',type=Path)
    args=parser.parse_args()
    try:
        if args.command=='prepare':
            if not args.spec or not args.config:raise ValueError('prepare 需要 --spec 与 --config')
            result=prepare(args.spec,args.config,args.project)
        else:
            with project_lock(args.project):
                studio=Studio(args.project,args.env_file,args.bindings)
                if args.command=='advance':result=studio.advance(args.review_dir)
                elif args.command in {'submit','poll','context','inspect'}:
                    if not args.job:raise ValueError('需要 --job')
                    result=getattr(studio,args.command)(args.job)
                elif args.command in {'authorize','recover','review','review-production','revise'}:
                    if not args.evidence:raise ValueError('需要 --evidence 文件')
                    evidence=read_json(args.evidence)
                    if args.command=='authorize':result=studio.authorize(evidence)
                    else:
                        if not args.job:raise ValueError('需要 --job')
                        result=getattr(studio,args.command.replace('-', '_'))(args.job,evidence)
                else:result=getattr(studio,args.command)()
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except (ValueError,OSError,KeyError,TransportError) as exc:
        print('ERROR: '+str(exc),file=sys.stderr);return 1

if __name__=='__main__':raise SystemExit(main())
