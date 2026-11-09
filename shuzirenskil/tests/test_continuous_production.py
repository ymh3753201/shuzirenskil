"""Synthetic local provider/ASR/OCR fixtures; never real media acceptance."""
import copy
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from _common import atomic_write_json, read_json, sha256_file, digest_json
from media import extract_review_frames
from audio_qc import inspect_audio
import production_review as pr
import test_studio as fixture
import test_guarded_api as legacy
import workflow


def machine_fixture(video, script, folder, binding):
    """Replace recognition engines only; preserve real decode, frame and hash guards."""
    folder = Path(folder);folder.mkdir(parents=True, exist_ok=True)
    extract_review_frames(video, folder)
    audio = inspect_audio(video, folder, script, transcribe=False)
    end = audio['technical']['duration_seconds'] - 0.2
    audio.update(status='automatic_checks_completed', recognition={'status':'completed','transcript':script,
        'cues':[{'text':script,'start_seconds':0.1,'end_seconds':end}],
        'words':[{'text':script,'start_seconds':0.1,'end_seconds':end,'probability':0.99}],
        'speech_regions':[{'start_seconds':0.1,'end_seconds':end}]})
    atomic_write_json(folder/'audio-qc.json', audio)
    manifest = read_json(folder/'manifest.json')
    atomic_write_json(folder/'text-qc.json', {'schema_version':'text-qc-1.0', 'video_sha256':sha256_file(video),
        'sampling':'every_decoded_frame','status':'no_text_detected','detections':[],
        'decoded_frames':manifest['total_video_frames']})
    return ORIGINAL_INSPECT(video, script, folder, binding)


ORIGINAL_INSPECT = pr.inspect


def visual_fixture(result):
    e = read_json(Path(result['production_review_template']))
    manifest = read_json(Path(e['manifest_file']))
    e.update(reviewer='agent_visual', reviewed_frame_indices=[f['frame_index'] for f in manifest['frames']],
             text_present=False, visible_defect_present=False,
             observations='模拟取样观察，仅验证程序关卡，不代表真实人物看听。')
    return e


class ContinuousTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.StudioTests();self.f.setUp()
        self.f.spec['review_mode'] = pr.MODE
        self.mock = patch('production_review.inspect', side_effect=machine_fixture);self.mock.start()
    def tearDown(self):
        self.mock.stop();self.f.tearDown()
    def start(self):
        s = self.f.prepare();self.f.approve(s)
        result = s.advance()
        return s, visual_fixture(result)
    def test_two_segments_finish_candidate_without_perceptual_or_new_approval(self):
        self.exercise(2)
    def test_three_segments_finish_candidate_without_perceptual_or_new_approval(self):
        self.exercise(3)
    def test_20_seconds_without_per_segment_listening(self):self.exercise(2, 10)
    def test_30_seconds_without_per_segment_listening(self):self.exercise(2, 15)
    def exercise(self, count, seconds=None):
        if seconds:
            from production_plan import build_plan
            for length in range(2,100):
                script='好'*length+'。';self.f.spec['script']=script*2
                for shot in self.f.spec['shots']:shot['script']=script
                plan=build_plan(self.f.spec,self.f.root)
                if len(plan['jobs'])==2 and all(j['request_seconds']==seconds for j in plan['jobs']):break
            else:self.fail('没有匹配的测试时长')
        if count == 3:
            self.f.spec['shots'].append({'id':'three','kind':'talking_head','script':'再见。','visual_brief':'人物自然告别'})
            self.f.spec['script'] += '再见。'
        s = self.f.prepare();self.f.approve(s)
        for index in range(count):
            result = s.advance();ident=f'segment_{index+1:04d}'
            self.assertEqual(result['job_id'], ident)
            evidence = visual_fixture(result)
            self.assertEqual(evidence['perceptual_review'], 'pending')
            self.assertNotIn('full_playback', evidence)
            s.review_production(ident, evidence)
            self.assertEqual(s.state['jobs'][ident]['state'], 'production_checked')
        final = s.advance()
        self.assertEqual(final['job_id'], 'final')
        self.assertTrue(Path(s.state['final']['video']).is_file())
        if seconds:self.assertAlmostEqual(s.state['final']['duration_seconds'], seconds*count)
        self.assertEqual(len(s.state['approvals']), 1)
        self.assertEqual(len(self.f.remote.calls), count)
        with self.assertRaises(ValueError):s.deliver()
        self.f.accept(s, 'final')
        self.assertEqual(s.advance()['status'], 'synthetic_test_delivery')
    def test_partial_frames_and_known_fail_block_next_paid_segment(self):
        s,e = self.start()
        for update in ({'reviewed_frame_indices': e['reviewed_frame_indices'][:2]},
                       {'text_present':True}, {'visible_defect_present':True},
                       {'checks':{'lip_sync':{'status':'fail'}}}, {'business_accepted':True}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                s.review_production('segment_0001', {**e,**update})
        self.assertEqual(len(self.f.remote.calls), 1)
    def test_missing_ocr_frame_or_detected_text_cannot_pass(self):
        s,e = self.start();path=Path(e['text_qc_file']);original=read_json(path)
        for update in ({'decoded_frames':1}, {'status':'incomplete'},
                       {'status':'text_detected','detections':[{'frame_index':0,'text':'字幕'}]}):
            atomic_write_json(path,{**original,**update});e['text_qc_sha256']=sha256_file(path)
            with self.assertRaises(ValueError):s.review_production('segment_0001',e)
        self.assertEqual(len(self.f.remote.calls),1)
    def test_false_positive_requires_frame_and_specific_observation(self):
        s,e = self.start();path=Path(e['text_qc_file']);r=read_json(path)
        r.update(status='text_detected',detections=[{'frame_index':0,'text':'9'}]);atomic_write_json(path,r)
        e['text_qc_sha256']=sha256_file(path)
        with self.assertRaises(ValueError):s.review_production('segment_0001',e)
        e['ocr_findings_review']=[{'detection_index':0,'verdict':'false_positive','observations':'仅模拟误检核验：此测试帧实际为纯蓝色画面。'}]
        s.review_production('segment_0001',e)
    def test_stale_audio_and_invalid_words_block(self):
        s,e = self.start();path=Path(e['audio_qc_file']);original=read_json(path)
        variants=[]
        for field,value in [('video_sha256','changed'),('script_sha256','changed')]:
            variants.append({**original,field:value})
        for field,value in [('transcript','实际漏词。'),('words',[{'probability':float('nan')}]),('cues',[])]:
            r=copy.deepcopy(original);r['recognition'][field]=value;variants.append(r)
        for r in variants:
            atomic_write_json(path,r);e['audio_qc_sha256']=sha256_file(path)
            with self.assertRaises(ValueError):s.review_production('segment_0001',e)
    def test_exact_transcript_low_confidence_is_deferred_not_fabricated_pass(self):
        s,e=self.start();path=Path(e['audio_qc_file']);r=read_json(path)
        r['recognition']['words'][0]['probability']=0.3;atomic_write_json(path,r)
        e['audio_qc_sha256']=sha256_file(path);s.review_production('segment_0001',e)
        record=s.state['jobs']['segment_0001']
        self.assertTrue(record['low_confidence_words'])
        self.assertEqual(record['perceptual_review'],'pending')
        self.assertFalse(record['business_accepted'])
    def human_review_fixture(self):
        s,e = self.start();path=Path(e['audio_qc_file']);audio=read_json(path)
        script=s.plan['jobs'][0]['script']
        cues=copy.deepcopy(audio['recognition']['cues'])
        audio['recognition']['transcript']='模拟机器误识别。'
        audio['recognition']['cues'][0]['text']='模拟机器误识别。'
        atomic_write_json(path,audio);e['audio_qc_sha256']=sha256_file(path)
        e['human_transcript_review']={'reviewer':'human_user', 'video_sha256':e['video_sha256'],
            'audio_qc_sha256':e['audio_qc_sha256'], 'contract_digest':e['contract_digest'],
            'full_playback':True,'script_verified':True,'transcript':script,'cues':cues,
            'user_statement':'模拟用户：已经实听核对，原片台词正确，可以继续。',
            'observations':'仅测试真实用户证据绑定，不代表任何真实视频已通过。'}
        return s,e,path
    def test_user_verified_transcript_keeps_raw_asr_and_does_not_trim(self):
        s,e,path=self.human_review_fixture();original=path.read_bytes()
        s.review_production('segment_0001',e)
        self.assertEqual(path.read_bytes(),original)
        record=s.state['jobs']['segment_0001']
        self.assertFalse(record['tail_edit']['trim'])
        self.assertFalse(record['business_accepted'])
        self.assertEqual(record['perceptual_review'],'pending')
    def test_user_review_cannot_bypass_bindings_timing_text_or_failures(self):
        s,e,path=self.human_review_fixture()
        for key,value in [('reviewer','agent_visual'),('video_sha256','stale'),
                          ('audio_qc_sha256','stale'),('contract_digest','stale'),
                          ('full_playback',False),('script_verified',False),('transcript','错误内容')]:
            changed=copy.deepcopy(e);changed['human_transcript_review'][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):s.review_production('segment_0001',changed)
        changed=copy.deepcopy(e);changed['human_transcript_review']['cues'][0]['start_seconds']+=0.01
        with self.assertRaises(ValueError):s.review_production('segment_0001',changed)
        for update in [{'text_present':True},{'checks':{'lip_sync':{'status':'fail'}}}]:
            with self.assertRaises(ValueError):s.review_production('segment_0001',{**e,**update})
    def test_user_auto_tail_requires_uncorrected_strong_ending(self):
        s,e,path=self.human_review_fixture();r=read_json(path)
        review=e['human_transcript_review'];script=review['transcript']
        start=0.1;end=r['recognition']['cues'][-1]['end_seconds'];mid=(start+end)/2
        r['recognition']['cues']=[{'start_seconds':start,'end_seconds':mid,'text':'误识'},
                                 {'start_seconds':mid,'end_seconds':end,'text':script[1:]}]
        r['recognition']['transcript']='误识'+script[1:]
        r['recognition']['words']=[{'text':'模拟','start_seconds':start+(end-start)*i/3,
            'end_seconds':start+(end-start)*(i+1)/3,'probability':0.99} for i in range(3)]
        review.update(tail_action='auto',cues=[{'start_seconds':start,'end_seconds':mid,'text':script[:1]},
                                             {'start_seconds':mid,'end_seconds':end,'text':script[1:]}])
        for weak in (False,True):
            r['recognition']['words'][-1]['probability']=0.3 if weak else 0.99
            atomic_write_json(path,r);e['audio_qc_sha256']=sha256_file(path)
            review['audio_qc_sha256']=e['audio_qc_sha256']
            with patch('production_review.tail_decision',wraps=pr.tail_decision) as decision:
                pr.validate(e,Path(read_json(Path(e['manifest_file']))['video']),script,e['contract_digest'])
                self.assertEqual(decision.call_args.kwargs['preserve'],weak)
    def test_old_contract_cannot_use_candidate_clearance(self):
        self.f.spec['review_mode']='full_per_segment';s=self.f.prepare();self.f.approve(s);s.advance()
        with self.assertRaisesRegex(ValueError,'未确认'):s.review_production('segment_0001',{})
    def test_new_narration_defaults_to_continuous_before_approval(self):
        self.f.spec.pop('review_mode');s=self.f.prepare()
        self.assertEqual(s.plan['review_mode'],pr.MODE)
        self.assertIn('统一验收',s.plan['review_notice'])
    def test_changed_cues_or_evidence_cannot_stitch(self):
        s,e=self.start();s.review_production('segment_0001',e)
        s.state['jobs']['segment_0001']['machine_cues'][0]['text']='被修改'
        with self.assertRaises(ValueError):s.verify_accepted('segment_0001')
    def test_unknown_submission_still_never_resends(self):
        s=self.f.prepare();self.f.approve(s);self.f.remote.fail_next=True
        with self.assertRaises(Exception):s.advance()
        self.assertEqual(s.advance()['status'],'blocked')
        self.assertEqual(len(self.f.remote.calls),1)
    def test_continuous_mode_cannot_exceed_authorized_request_count(self):
        s=self.f.prepare();a=self.f.approval(s);a['max_attempts']=1;s.authorize(a)
        result=s.advance();s.review_production('segment_0001',visual_fixture(result))
        with self.assertRaisesRegex(ValueError,'上限'):s.advance()
        self.assertEqual(len(self.f.remote.calls),1)


class LegacyContinuousTests(unittest.TestCase):
    def test_original_gateway_advances_all_authorized_segments_and_stitches(self):
        f=legacy.GuardedApiTests();f.setUp()
        try:
            root=f.root/'continuous'
            f.run_cli(root,'prepare','--name','test','--script-file',str(f.script),'--duration','35','--review-mode','auto')
            f.run_cli(root,'confirm-plan','--approved-by','user')
            f.run_cli(root,'bind-image','--image',str(f.image),'--continuity-spec',str(f.continuity),'--provider-image-url',f.provider_image_url)
            project=read_json(root/'project.json');count=len(project['plan']['segments'])
            f.run_cli(root,'authorize','--approved-by','user','--confirmation-intent','confirm_images_and_start','--max-paid-submissions',str(count),'--acknowledge-long-video-risk')
            args=workflow.build_parser().parse_args(['--project-dir',str(root),'preflight','--config',str(f.config_path)])
            with patch('production_review.require_runtime'):workflow.command_preflight(args)
            clip=f.root/'fixture.mp4';fixture.video(clip,2,dimensions='720x1280',fps=24)
            legacy.FakeHandler.video_bytes=clip.read_bytes()
            f.run_cli(root,'submit',api_key=True)
            polled=f.run_cli(root,'poll','--max-polls','1',api_key=True)
            self.assertIn('继续制作检查',polled.stdout)
            self.assertNotIn('review-pilot',polled.stdout)
            project=read_json(root/'project.json')
            def clear(index):
                job=read_json(root/'jobs.json')['jobs'][index]
                result=machine_fixture(Path(job['clip_path']),project['plan']['segments'][index]['script'],root/'production-qc'/job['shot_id'],project['contract_digest'])
                evidence=f.root/'evidence.json';atomic_write_json(evidence,visual_fixture(result))
                f.run_cli(root,'review-production','--shot',job['shot_id'],'--evidence-file',str(evidence))
            clear(0)
            def advance():
                args=workflow.build_parser().parse_args(['--project-dir',str(root),'advance'])
                with patch('production_review.inspect',side_effect=machine_fixture), patch.dict(os.environ,{'SHUZIRENSKIL_MIKUAPI_API_KEY':'test-only-not-a-real-key'}):
                    workflow.command_advance(args)
            advance()
            # advance may already prepare remaining checks; inspect was local, no new authorization.
            for i in range(1,count):clear(i)
            advance()
            self.assertEqual(legacy.FakeHandler.post_count,count)
            self.assertEqual(read_json(root/'project.json')['state']['stage'],'clean_candidate_ready')
            self.assertFalse((root/'delivery-manifest.json').exists())
        finally:f.tearDown()
