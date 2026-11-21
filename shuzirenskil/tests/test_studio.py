"""Synthetic local HTTP/media tests. These are NOT real model or listening acceptance."""
import base64
import copy
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import wave
from datetime import datetime,timedelta,timezone
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from _common import atomic_write_json,sha256_file,digest_json,read_json
from studio import Studio,prepare,config_check,CHECKS,AUDIO_CHECKS
from studio_transport import Adapter,TransportError,json_request
from studio_assets import resolve
from studio_media import command,render,write_subtitles
from production_plan import build_plan
from speech_worker import alignment_to_cues


def wav(path,seconds):
    with wave.open(str(path),'wb') as f:
        f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000);f.writeframes(b'\x10\x00'*round(seconds*16000))


def video(path,seconds,dimensions='32x56',fps=8):
    command(['ffmpeg','-v','error','-y','-f','lavfi','-i',f'color=c=blue:s={dimensions}:r={fps}',
             '-f','lavfi','-i','sine=frequency=440:sample_rate=16000','-t',str(seconds),'-c:v','libx264',
             '-preset','ultrafast','-pix_fmt','yuv420p','-c:a','aac','-shortest',str(path)])


class LocalProvider:
    def __init__(self,root):
        self.root=root;self.calls=[];self.tasks={};self.fail_next=False;self.redirect=False
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def send(self,obj,code=200):
                raw=obj if isinstance(obj,bytes) else json.dumps(obj).encode()
                self.send_response(code);self.end_headers();self.wfile.write(raw)
            def do_POST(self):
                payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])));owner.calls.append((self.path,payload,dict(self.headers)))
                if owner.fail_next:
                    owner.fail_next=False;self.send({'error':'simulated timeout'},503);return
                ident=str(len(owner.tasks)+1);seconds=payload.get('duration') or payload.get('parameters',{}).get('duration') or payload.get('parameters',{}).get('durationSeconds') or 4
                if self.path.startswith('/fal-ai/'):
                    raw=base64.b64decode(payload['audio_url'].split(',',1)[1])
                    with wave.open(io.BytesIO(raw)) as f:seconds=f.getnframes()/f.getframerate()
                path=root/f'{ident}.mp4';video(path,seconds);owner.tasks[ident]=path
                if '/video-synthesis' in self.path:self.send({'output':{'task_id':ident}})
                elif self.path.startswith('/v2/'):self.send({'task_id':ident})
                elif self.path.startswith('/fal-ai/'):self.send({'request_id':ident,'status_url':owner.url+'/status/'+ident,'response_url':owner.url+'/result/'+ident})
                elif ':predictLongRunning' in self.path:self.send({'name':'operations/'+ident})
                else:self.send({'id':ident})
            def do_GET(self):
                ident=self.path.split('/')[-1];url=owner.url+'/media/'+ident
                if owner.redirect:
                    self.send_response(302);self.send_header('Location',owner.url+'/leaked');self.end_headers();return
                if self.path.startswith('/media/'):self.send(owner.tasks[ident].read_bytes())
                elif self.path.startswith('/status/'):self.send({'status':'COMPLETED'})
                elif self.path.startswith('/result/'):self.send({'video':{'url':url}})
                elif self.path.startswith('/api/v1/tasks/'):self.send({'output':{'task_status':'SUCCEEDED','video_url':url}})
                elif self.path.startswith('/v2/'):self.send({'task':{'status':'succeeded','content':{'url':url}}})
                elif self.path.startswith('/v1beta/'):self.send({'done':True,'response':{'generateVideoResponse':{'generatedSamples':[{'video':{'uri':url}}]}}})
                elif self.path.startswith('/api/v3/'):self.send({'status':'succeeded','content':{'video_url':url}})
                else:self.send({'status':'completed','video_url':url})
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.url=f'http://127.0.0.1:{self.server.server_port}'
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def close(self):self.server.shutdown();self.server.server_close();self.thread.join()


class StudioTests(unittest.TestCase):
    def setUp(self):
        from text_fixture import synthetic_text_inspection
        self.ocr_mock = patch('studio.inspect_text', side_effect=synthetic_text_inspection)
        self.ocr_mock.start()
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.remote=LocalProvider(self.root)
        command(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=s=32x56','-frames:v','1',str(self.root/'person.png')])
        wav(self.root/'master.wav',7)
        self.spec={'schema_version':'1.0','review_mode':'full_per_segment','name':'离线测试，非模型样片','model_profile':'minimax-h3-reference',
            'voice':{'mode':'model_generated'},'voice_design':'温和自然、清楚有力的中性声音。','script':'你好。欢迎。',
            'assets':{'person':{'path':'person.png','kind':'image','role':'identity'},'voice':{'path':'master.wav','kind':'audio'}},
            'reference_images':['person'],'character_bible':{k:'测试用固定描述。' for k in ['identity','wardrobe','scene','camera','lighting']},
            'aspect_ratio':'9:16','resolution':'768p',
            'shots':[{'id':'one','kind':'talking_head','script':'你好。','visual_brief':'人物自然挥手问候'},
                     {'id':'two','kind':'talking_head','script':'欢迎。','visual_brief':'人物自然介绍服务'}]}
        self.config={'provider':{'base_url':self.remote.url,'key_env':'STUDIO_TEST_KEY'},
                     'expected_dimensions':[32,56],'expected_fps':8,'test_mode':True}
        self.env=patch.dict(os.environ,{'STUDIO_TEST_KEY':'synthetic-local-only'});self.env.start()
    def tearDown(self):self.ocr_mock.stop();self.env.stop();self.remote.close();self.temp.cleanup()
    def prepare(self, project_name="project"):
        atomic_write_json(self.root/'spec.json',self.spec);atomic_write_json(self.root/'config.json',self.config)
        prepare(self.root/'spec.json',self.root/'config.json',self.root/project_name)
        return Studio(self.root/project_name)
    def approval(self,s):
        value=read_json(s.root/'approval-template.json');value.update(approved_by='synthetic_test_runner',
            expires_at=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),max_attempts=20,max_request_seconds=100)
        for k in ('images_reviewed','script_and_voice_approved','asset_upload_approved','audio_rights_confirmed','unverified_provider_pilot_approved'):value[k]=True
        return value
    def approve(self,s):s.authorize(self.approval(s))
    def accept(self,s,ident):
        from text_fixture import viewed_indices
        template=s.inspect(ident);e=read_json(Path(template['review_template']));e.update(reviewer='agent_audiovisual',full_playback=True,review_seconds=10,all_segments_and_seams_reviewed=True)
        for v in e['checks'].values():v.update(status='pass',observations='仅模拟测试检查记录，不代表真实观看与效果验收。')
        e['text_review'].update(reviewer='agent_visual',full_video_viewed=True,all_sampled_frames_viewed=True,last_frame_viewed=True,text_present=False,observations='仅模拟测试检查记录，不代表真实看图结论。')
        e['text_review']['reviewed_frame_indices'] = viewed_indices(e['text_review'])
        if ident!='final':
            job,record=s.job(ident);e['transcript']=job['script'];e['cues']=[{'start_seconds':0.1,'end_seconds':record['technical']['seconds']-0.2,'text':job['script']}]
        s.review(ident,e)
    def test_native_end_to_end_and_unused_audio(self):
        s=self.prepare();self.approve(s)
        self.assertNotIn('max_cost',read_json(s.root/'approval-template.json'))
        self.assertNotIn('currency',read_json(s.root/'approval-template.json'))
        with self.assertRaises(ValueError):s.submit('segment_0002')
        for ident in s.state['jobs']:
            s.submit(ident);s.poll(ident);self.accept(s,ident)
        self.assertNotIn('audio_url',json.dumps(self.remote.calls))
        result=s.assemble();self.assertEqual(result['duration_seconds'],8)
        with self.assertRaises(ValueError):s.deliver()
        self.accept(s,'final');delivery=s.deliver();self.assertEqual(delivery['status'],'synthetic_test_delivery')
        self.assertFalse(delivery['metrics']['real_media_quality_verified'])
        self.assertIsNone(delivery['metrics']['cost_reserved'])
        self.assertIsNone(delivery['metrics']['currency'])
    def test_production_config_needs_no_prices(self):
        plan=build_plan(self.spec,self.root)
        config={'provider':{'base_url':'https://api.minimax.io','key_env':'MINIMAX_API_KEY'},
                'expected_dimensions':[768,1366],'expected_fps':24,'test_mode':False}
        config_check(config,plan)
        self.assertEqual(self.remote.calls,[])
    def test_old_zero_rate_template_is_unknown_not_free(self):
        self.config.update(rates={'video_per_second':0,'lip_sync_per_second':0},currency='USD')
        s=self.prepare();self.approve(s);s.submit('segment_0001')
        self.assertIsNone(s.state['attempts'][0]['reserved_cost'])
        self.assertIsNone(Studio(s.root).status()['cost_reserved'])
        self.assertEqual(len(self.remote.calls),1)
    def test_old_config_does_not_bind_or_request_unused_service(self):
        self.config['lip_sync'] = {'key_env': 'FAL_KEY', 'base_url': 'https://queue.fal.run'}
        del self.spec['voice']
        self.spec['assets']['voice']['path'] = 'unused-recording.wav'
        studio = self.prepare()
        self.assertNotIn('lip_sync', studio.config)
        self.assertNotIn('audio_rights_confirmed', read_json(studio.root/'approval-template.json'))
        with patch('studio.load_private_value', return_value='synthetic') as lookup:
            studio.preflight()
        self.assertEqual([call.args[0] for call in lookup.call_args_list], ['STUDIO_TEST_KEY'])
        self.assertEqual(self.remote.calls, [])

    def test_only_explicit_monetary_cap_checks_cost(self):
        self.config.update(rates={'video_per_second':0.1},currency='USD')
        s=self.prepare();e=self.approval(s);e.update(max_cost=0.3,currency='USD');s.authorize(e)
        with self.assertRaisesRegex(ValueError,'金额上限'):s.submit('segment_0001')
        self.assertEqual(self.remote.calls,[])
        e['max_cost']=0.5;s.authorize(e);s.submit('segment_0001')
        self.assertAlmostEqual(s.state['attempts'][0]['reserved_cost'],0.4)
    def test_user_monetary_cap_is_not_silently_ignored_without_prices(self):
        s=self.prepare();e=self.approval(s);e.update(max_cost=1,currency='USD')
        with self.assertRaises(ValueError):s.authorize(e)
        self.assertEqual(self.remote.calls,[])
    def test_no_price_still_obeys_request_seconds(self):
        s=self.prepare();e=self.approval(s);e['max_request_seconds']=3;s.authorize(e)
        with self.assertRaisesRegex(ValueError,'秒数上限'):s.submit('segment_0001')
        self.assertEqual(self.remote.calls,[])
    def test_legacy_priced_project_resumes_with_cumulative_cap(self):
        self.config.update(rates={'video_per_second':0.1,'lip_sync_per_second':0.2},currency='USD')
        s=self.prepare();e=self.approval(s);e.update(max_cost=0.5,currency='USD');s.authorize(e)
        s.submit('segment_0001');s.poll('segment_0001');self.accept(s,'segment_0001');s=Studio(s.root)
        with self.assertRaisesRegex(ValueError,'金额上限'):s.submit('segment_0002')
        self.assertEqual(len(self.remote.calls),1)
    def test_reference_target_does_not_stretch_assembled_video(self):
        self.spec['target_seconds']=45
        s=self.prepare();self.approve(s)
        self.assertEqual(s.plan['duration_mode'],'reference')
        self.assertEqual(self.spec['voice_design'],s.plan['consistency_contract']['voice_design'])
        for ident in s.state['jobs']:
            s.submit(ident);s.poll(ident);self.accept(s,ident)
        result=s.assemble()
        self.assertEqual(result['duration_seconds'],8)
        self.assertEqual(s.state['status'],'awaiting_final_review')
    def test_studio_rejects_new_external_master_before_creating_project(self):
        self.spec['voice'] = {'mode':'locked_audio', 'audio_asset':'voice'}
        with self.assertRaisesRegex(ValueError, '视频模型'):
            self.prepare()
        self.assertFalse((self.root/'project').exists())
        self.assertEqual(self.remote.calls, [])

    def test_uncertain_post_never_retried_and_recovery(self):
        s=self.prepare();self.approve(s);self.remote.fail_next=True
        with self.assertRaises(TransportError):s.submit('segment_0001')
        self.assertEqual(len(s.state['attempts']),1)
        s=Studio(s.root)
        with self.assertRaises(ValueError):s.submit('segment_0001')
        self.assertEqual(len(self.remote.calls),1)
        path=self.root/'known.mp4';video(path,4);self.remote.tasks['known']=path
        s.recover('segment_0001',{'task':{'id':'known'},'contract_digest':s.contract['digest'],'request_digest':s.state['attempts'][0]['request_digest'],'provider_console_checked':True,'approved_by':'synthetic_test_runner'});s.poll('segment_0001')
        self.assertEqual(len(self.remote.calls),1)
    def test_approval_caps_expiry_and_changed_asset(self):
        s=self.prepare();e=self.approval(s);e['max_attempts']=1;s.authorize(e)
        s.submit('segment_0001');s.poll('segment_0001');self.accept(s,'segment_0001')
        with self.assertRaises(ValueError):s.submit('segment_0002')
        e['expires_at']='2000-01-01T00:00:00+00:00'
        with self.assertRaises(ValueError):s.authorize(e)
        (self.root/'person.png').write_bytes(b'changed')
        with self.assertRaises(ValueError):s.assemble()
    def test_review_fail_does_not_release_pilot(self):
        s=self.prepare();self.approve(s);s.submit('segment_0001');s.poll('segment_0001')
        e=read_json(Path(s.inspect('segment_0001')['review_template']))
        with self.assertRaises(ValueError):s.review('segment_0001',e)
        with self.assertRaises(ValueError):s.submit('segment_0002')
    def test_revision_requires_new_scope_approval_and_preserves_history(self):
        s=self.prepare();self.approve(s);s.submit('segment_0001');s.poll('segment_0001');self.accept(s,'segment_0001')
        e={'contract_digest':s.contract['digest'],'job_id':'segment_0001','video_sha256':s.job('segment_0001')[1]['clip_sha256'],
           'approved_by':'synthetic_test_runner','reason':'测试重做原因，保留旧片段记录。'}
        with self.assertRaises(ValueError):s.revise('segment_0001',e)
        self.approve(s);s.revise('segment_0001',e)
        self.assertEqual(len(s.state['attempts']),1);self.assertEqual(s.job('segment_0001')[1]['revision'],1)
        self.assertGreaterEqual(len(s.state['revisions']),1)
    def test_continuation_uses_reviewed_tail_and_original_anchor(self):
        self.spec['continuity_mode']='continue';s=self.prepare();self.approve(s)
        s.submit('segment_0001');s.poll('segment_0001');self.accept(s,'segment_0001');s.submit('segment_0002')
        content=self.remote.calls[-1][1]['content'];self.assertEqual(sum(x['type']=='video_url' for x in content),1)
        self.assertEqual(sum(x['type']=='image_url' for x in content),1)
        self.assertEqual(s.job('segment_0002')[1]['context']['previous_video_sha256'],s.job('segment_0001')[1]['clip_sha256'])
    def test_all_provider_protocols_create_poll_download(self):
        cases=[('aliyun',{'model':'wan3.0-video','parameters':{'duration':2}}),('volcengine',{'model':'test','duration':2}),
               ('google',{'model':'veo-3.1-generate-preview','parameters':{'durationSeconds':2}}),('91topgo',{'seconds':'2'})]
        for provider,payload in cases:
            with self.subTest(provider=provider):
                a=Adapter(provider,{'base_url':self.remote.url},'synthetic',loopback=True)
                task=a.create(payload);result=a.poll(task);dest=self.root/(provider+'.mp4');a.download(result['url'],dest);self.assertTrue(dest.is_file())
    def test_reference_sample_goes_to_selected_video_model(self):
        self.spec['voice'] = {'mode':'reference_timbre','audio_asset':'voice'}
        for profile, resolution, endpoint in [
                ('minimax-h3-reference', '768p', '/v2/video_generation'),
                ('seedance20-reference', '720p', '/api/v3/contents/generations/tasks')]:
            with self.subTest(profile=profile):
                self.spec.update(model_profile=profile, resolution=resolution)
                studio = self.prepare(profile)
                self.approve(studio)
                count = len(self.remote.calls)
                studio.submit('segment_0001')
                self.assertEqual(len(self.remote.calls), count + 1)
                path, body, _ = self.remote.calls[-1]
                self.assertEqual(path, endpoint)
                sample, = [x for x in body['content'] if x.get('role') == 'reference_audio']
                actual = base64.b64decode(sample['audio_url']['url'].split(',', 1)[1])
                self.assertEqual(actual, (self.root/'master.wav').read_bytes())
                self.assertIn('<SPOKEN_AUDIO_ONLY>\n你好。\n', body['content'][0]['text'])
                self.assertIsNone(studio.plan['audio_plan'])
                self.assertFalse(studio.plan['jobs'][0]['requires_lip_sync'])
                self.assertEqual(studio.plan['jobs'][0]['native_audio_policy'], 'retain_and_review')

    def test_veo_uses_inline_images(self):
        other=copy.deepcopy(self.spec);other['voice']={'mode':'model_generated'};other['model_profile']='veo31-reference';other['resolution']='720p'
        plan=build_plan(other,self.root);self.assertIn('referenceImages',plan['jobs'][0]['request_preview']['payload']['instances'][0])
    def test_legacy_gateway_response_variants_are_preserved(self):
        adapter=Adapter('91topgo',{'base_url':self.remote.url},'synthetic',loopback=True)
        with patch.object(adapter,'_call',return_value={'data':{'task_id':'task-1'}}):
            self.assertEqual(adapter.create({'model':'test'})['id'],'task-1')
        with patch.object(adapter,'_call',return_value={'status':'completed','result':{'download_url':'/media/task-1'}}):
            self.assertEqual(adapter.poll({'id':'task-1'})['url'],self.remote.url+'/media/task-1')

    def test_bad_host_and_redirect_never_send_to_new_origin(self):
        with self.assertRaises(ValueError):Adapter('minimax',{'base_url':'https://evil.example'},'secret')
        a=Adapter('minimax',{'base_url':self.remote.url},'synthetic',loopback=True);self.remote.redirect=True
        with self.assertRaises(TransportError):a.poll({'id':'1'})
    def test_remote_asset_hash_mismatch(self):
        path=self.root/'known.mp4';video(path,2);self.remote.tasks['known']=path
        e={'expires_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),'url':self.remote.url+'/media/known'}
        with self.assertRaises((ValueError,TransportError)):resolve(self.root/'person.png',sha256_file(self.root/'person.png'),{sha256_file(self.root/'person.png'):e},loopback=True)
    def test_compiled_new_providers_end_to_end_and_google_inline(self):
        for profile, resolution in [('wan3-reference','720p'),('seedance25-reference','720p'),('veo31-reference','720p')]:
            with self.subTest(profile=profile):
                spec=copy.deepcopy(self.spec);spec['model_profile']=profile;spec['resolution']=resolution
                atomic_write_json(self.root/'spec2.json',spec);atomic_write_json(self.root/'config2.json',self.config)
                prepare(self.root/'spec2.json',self.root/'config2.json',self.root/profile)
                s=Studio(self.root/profile);self.approve(s);s.submit('segment_0001');s.poll('segment_0001')
                payload=self.remote.calls[-1][1]
                self.assertNotIn('asset://',json.dumps(payload));self.assertNotIn('audio_url',json.dumps(payload))
                if profile.startswith('veo'):self.assertIn('inlineData',payload['instances'][0]['referenceImages'][0]['image'])

    def test_test_mode_cannot_connect_real_provider(self):
        with self.assertRaises(ValueError):Adapter('minimax',{'base_url':'https://api.minimax.io'},'synthetic',loopback=True)
        from studio_transport import safe_url
        with self.assertRaises(ValueError):safe_url('https://example.com/asset',loopback=True)

    def test_failed_review_records_reason_and_blocks_next_job(self):
        s=self.prepare();self.approve(s);s.submit('segment_0001');s.poll('segment_0001')
        e=read_json(Path(s.inspect('segment_0001')['review_template']))
        e.update(full_playback=True,review_seconds=10)
        for k,v in e['checks'].items():v.update(status='pass',observations='模拟测试填写具体观察，不代表真实观看。')
        e['checks']['voice_identity']['status']='fail'
        with self.assertRaises(ValueError):s.review('segment_0001',e)
        self.assertFalse(s.status()['pilot_first_review_checks_passed'])
        with self.assertRaises(ValueError):s.submit('segment_0002')

    def test_cut_pilot_revision_preserves_later_clip_for_review(self):
        s=self.prepare();self.approve(s)
        for ident in s.state['jobs']:s.submit(ident);s.poll(ident);self.accept(s,ident)
        later=s.job('segment_0002')[1]['clip'];self.approve(s)
        s.revise('segment_0001',{'contract_digest':s.contract['digest'],'job_id':'segment_0001',
            'video_sha256':s.job('segment_0001')[1]['clip_sha256'],'approved_by':'synthetic_test_runner','reason':'模拟首段需要重做，保留其余已完成片段。'})
        self.assertEqual(s.job('segment_0002')[1]['clip'],later)
        self.assertEqual(s.job('segment_0002')[1]['state'],'review_pending')
        with self.assertRaises(ValueError):s.submit('segment_0002')

    def test_stale_final_or_review_cannot_deliver(self):
        s=self.prepare();self.approve(s)
        for ident in s.state['jobs']:s.submit(ident);s.poll(ident);self.accept(s,ident)
        s.assemble();self.accept(s,'final');Path(s.state['final']['subtitles']).write_text('changed', encoding="utf-8")
        with self.assertRaises(ValueError):s.deliver()

    def test_contract_tampering(self):
        s=self.prepare();plan=read_json(s.root/'plan.json');plan['jobs'][0]['script']='改写。';atomic_write_json(s.root/'plan.json',plan)
        with self.assertRaises(ValueError):Studio(s.root)


class TimelineTests(unittest.TestCase):
    def test_three_lengths_preserve_all_audio_samples_and_time(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for duration in (30,120,600):
                with self.subTest(duration=duration):
                    audio=root/f'master-{duration}.wav';wav(audio,duration)
                    clip=root/f'clip-{duration}.mp4';video(clip,30,dimensions='16x16',fps=2)
                    jobs=[{'job_id':f'segment_{i:04d}','script':'你好。','start_sample':i*30*16000,'end_sample':(i+1)*30*16000,
                           'request_seconds':30,'use_seconds':30,'native_audio_policy':'discard'} for i in range(duration//30)]
                    plan={'jobs':jobs,'audio_plan':{'master':{'path':str(audio),'sha256':sha256_file(audio),'sample_rate':16000}}}
                    result=render(plan,[clip]*len(jobs),root/f'out-{duration}')
                    self.assertEqual(result['duration_seconds'],duration)
                    self.assertEqual(result['timeline'][-1]['end_seconds'],duration)
    def test_subtitle_cues_cannot_be_estimated_or_omit_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'out.srt'
            with self.assertRaises(ValueError):write_subtitles([],4,'你好。',out)
            with self.assertRaises(ValueError):write_subtitles([{'start_seconds':0,'end_seconds':5,'text':'你好。'}],4,'你好。',out)
            with self.assertRaises(ValueError):write_subtitles([{'start_seconds':0,'end_seconds':3,'text':'欢迎。'}],4,'你好。',out)
    def test_alignment_character_integrity(self):
        result={'segments':[{'chars':[{'char':c,'start':i/2,'end':(i+1)/2} for i,c in enumerate('你好。欢迎。')]}]}
        self.assertEqual(len(alignment_to_cues(result,'你好。欢迎。')),2)
        with self.assertRaises(ValueError):alignment_to_cues(result,'你好。再见。')

if __name__=='__main__':unittest.main()
