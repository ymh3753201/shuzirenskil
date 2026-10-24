"""Offline regression fixtures. Tones and synthetic reviews do not prove listening quality."""
import copy
import json
import os
import platform
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from _common import digest_json, sha256_file, project_lock
from audio_qc import tail_decision, inspect_audio
from studio_media import render, command, context_clip
from runtime_tools import capabilities
import test_studio as studio_fixture


class TailTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.clip=self.root/'中文 音频.mp4'
        self.make(self.clip, 'sine=frequency=440:duration=10:sample_rate=48000')
    def tearDown(self):self.temp.cleanup()
    def make(self,path,audio):
        command(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=blue:s=32x56:r=24:d=14',
            '-f','lavfi','-i',audio,'-af','apad=whole_dur=14','-t','14','-c:v','libx264','-preset','ultrafast','-c:a','aac',str(path)])
    def cues(self,end=10):return [{'start_seconds':0,'end_seconds':end,'text':'你好。'}]
    def report(self,end=10):
        return {'schema_version':'audio-qc-1.0','video_sha256':sha256_file(self.clip),'script_sha256':digest_json('你好。'),
                'recognition':{'status':'completed','transcript':'你好。','speech_regions':[{'start_seconds':0,'end_seconds':end}],
                'words':[{'start_seconds':0,'end_seconds':end,'text':'你好','probability':0.95}]}}
    def test_silent_tails_removed_without_changing_source(self):
        before=sha256_file(self.clip);edit=tail_decision(self.clip,'你好。',self.cues())
        plan={'audio_plan':None,'jobs':[{'job_id':str(n),'script':'你好。','kind':'talking_head','native_audio_policy':'retain_and_review'} for n in range(2)]}
        result=render(plan,[self.clip]*2,self.root/'out',edits=[edit]*2)
        self.assertAlmostEqual(result['duration_seconds'],20.666667,places=4)
        self.assertEqual(result['actual_video_frames'],496)
        self.assertEqual(sha256_file(self.clip),before)
        self.assertEqual(result['status'],'awaiting_final_review')
    def test_reviewed_quiet_word_protects_end(self):
        edit=tail_decision(self.clip,'你好。',self.cues(13.8))
        self.assertFalse(edit['trim'])
    def test_music_tail_needs_matching_asr_vad_and_reviewed_cues(self):
        self.make(self.clip,'sine=frequency=220:duration=14:sample_rate=48000')
        self.assertFalse(tail_decision(self.clip)['trim'])
        report=self.report()
        edit=tail_decision(self.clip,'你好。',self.cues(),report)
        self.assertTrue(edit['trim']);self.assertAlmostEqual(edit['end_seconds'],10.3)
        self.assertFalse(tail_decision(self.clip,'你好。',None,report)['trim'])
    def test_asr_mismatch_or_low_confidence_preserves_background_tail(self):
        self.make(self.clip,'sine=frequency=220:duration=14:sample_rate=48000')
        report=self.report();report['recognition']['transcript']='您好。'
        self.assertFalse(tail_decision(self.clip,'你好。',self.cues(),report)['trim'])
        report=self.report();report['recognition']['words'][0]['probability']=0.3
        self.assertFalse(tail_decision(self.clip,'你好。',self.cues(),report)['trim'])
    def test_vad_disagreement_or_stale_report_not_used(self):
        self.make(self.clip,'sine=frequency=220:duration=14:sample_rate=48000')
        report=self.report();report['recognition']['speech_regions'][0]['end_seconds']=13
        self.assertFalse(tail_decision(self.clip,'你好。',self.cues(),report)['trim'])
        report=self.report();report['video_sha256']='changed'
        self.assertFalse(tail_decision(self.clip,'你好。',self.cues(),report)['trim'])
    def test_intentional_hold_and_no_speech(self):
        self.assertFalse(tail_decision(self.clip,preserve=True)['trim'])
        self.make(self.clip,'anullsrc=r=48000:cl=mono')
        with self.assertRaisesRegex(ValueError,'基本静音'):tail_decision(self.clip)
    def test_invalid_cues_and_vad_never_cut(self):
        with self.assertRaises(ValueError):tail_decision(self.clip,cues=self.cues(float('nan')))
        report=self.report();report['recognition']['speech_regions'][0]['end_seconds']=float('nan')
        with self.assertRaises(ValueError):tail_decision(self.clip,'你好。',self.cues(),report)
    def test_locked_audio_policy_preserves_original(self):
        self.assertEqual(tail_decision(self.clip,preserve=True)['end_seconds'],14)
    def test_context_uses_same_frame_aligned_cut(self):
        result=context_clip(self.clip,self.root/'context.mp4',end_seconds=10.3)
        self.assertAlmostEqual(result['source_end_seconds'],10+8/24)
        with self.assertRaises(ValueError):context_clip(self.clip,self.root/'short.mp4',end_seconds=1)
    def test_missing_asr_still_runs_technical_audio_checks(self):
        with patch('audio_qc.settings',return_value={}):result=inspect_audio(self.clip,self.root/'qc','你好。')
        self.assertEqual(result['recognition']['status'],'unavailable')
        self.assertEqual(result['silence']['tail_silence_seconds'],4)
        self.assertEqual(result['perceptual_review'],'pending')
    def test_no_audio_reports_blocked(self):
        path=self.root/'mute.mp4'
        command(['ffmpeg','-v','error','-y','-i',str(self.clip),'-an','-c:v','copy',str(path)])
        self.assertEqual(inspect_audio(path,self.root/'qc')['status'],'blocked')


class AdvanceTests(unittest.TestCase):
    def setUp(self):
        self.fixture=studio_fixture.StudioTests();self.fixture.setUp()
    def tearDown(self):self.fixture.tearDown()
    def test_one_approval_advance_all_segments_and_final(self):
        f=self.fixture;s=f.prepare();f.approve(s)
        for ident in ('segment_0001','segment_0002','final'):
            pending=s.advance()
            self.assertEqual(pending['status'],'awaiting_agent_review');self.assertEqual(pending['job_id'],ident)
            before=len(f.remote.calls)
            self.assertEqual(s.advance()['status'],'awaiting_agent_review')
            self.assertEqual(len(f.remote.calls),before)
            f.accept(s,ident)
        self.assertEqual(s.advance()['status'],'synthetic_test_delivery')
        self.assertEqual(len(s.state['approvals']),1);self.assertEqual(len(f.remote.calls),2)
    def test_advance_no_authorization_cannot_submit(self):
        f=self.fixture;s=f.prepare()
        with self.assertRaises(ValueError):s.advance()
        self.assertEqual(len(f.remote.calls),0)
    def test_advance_unknown_result_never_resubmits(self):
        f=self.fixture;s=f.prepare();f.approve(s);f.remote.fail_next=True
        with self.assertRaises(Exception):s.advance()
        calls=len(f.remote.calls)
        self.assertEqual(s.advance()['status'],'blocked');self.assertEqual(len(f.remote.calls),calls)
    def test_changed_edit_blocks_assembly(self):
        f=self.fixture;s=f.prepare();f.approve(s)
        s.advance();f.accept(s,'segment_0001')
        s.state['jobs']['segment_0001']['tail_edit']['end_seconds']=1
        with self.assertRaisesRegex(ValueError,'裁尾证据'):s.verify_accepted('segment_0001')
    def test_pending_check_never_advances_next_paid_job(self):
        f=self.fixture;s=f.prepare();f.approve(s);result=s.advance()
        e=json.loads(Path(result['review_template']).read_text());e['full_playback']=True;e['review_seconds']=4
        with self.assertRaises(ValueError):s.review('segment_0001',e)
        self.assertEqual(len(f.remote.calls),1)
    def exercise_duration(self,seconds):
        from production_plan import build_plan
        f=self.fixture
        for count in range(2,100):
            script='好'*count+'。'
            f.spec['script']=script*2
            for shot in f.spec['shots']:shot['script']=script
            plan=build_plan(f.spec,f.root)
            if len(plan['jobs'])==2 and all(j['request_seconds']==seconds for j in plan['jobs']):break
        else:self.fail('测试稿未找到对应自然时长')
        s=f.prepare();f.approve(s)
        for ident in ('segment_0001','segment_0002','final'):
            self.assertEqual(s.advance()['job_id'],ident);f.accept(s,ident)
        result=s.advance()
        self.assertEqual(result['status'],'synthetic_test_delivery')
        self.assertEqual(s.state['final']['duration_seconds'],seconds*2)
        self.assertEqual(len(s.state['approvals']),1);self.assertEqual(len(f.remote.calls),2)
    def test_20_seconds_one_authorization(self):self.exercise_duration(10)
    def test_30_seconds_one_authorization(self):self.exercise_duration(15)


class EnvironmentTests(unittest.TestCase):
    def test_cache_verifies_files_platform_and_model_revision(self):
        from setup_environment import verify_cache, MODEL_REVISION
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'fixture.whl').write_bytes(b'test')
            manifest={'platform':platform.system(),'machine':platform.machine(),'python':'3.12',
                      'model_revision':MODEL_REVISION,'files':{'fixture.whl':sha256_file(root/'fixture.whl')}}
            (root/'CACHE.json').write_text(json.dumps(manifest)); verify_cache(root)
            (root/'unexpected.whl').write_bytes(b'changed')
            with self.assertRaises(ValueError):verify_cache(root)
            (root/'unexpected.whl').unlink();(root/'fixture.whl').write_bytes(b'changed')
            with self.assertRaises(ValueError):verify_cache(root)
            (root/'fixture.whl').write_bytes(b'test');manifest['platform']='different-system'
            (root/'CACHE.json').write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):verify_cache(root)
    def test_absent_runtime_does_not_claim_asr_or_perception(self):
        with patch('runtime_tools.settings',return_value={}):result=capabilities()
        self.assertFalse(result['automatic_audio_ready'])
        self.assertEqual(result['agent_audio_input']['status'],'unverified')
    def test_project_lock_blocks_other_process_and_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=str(Path(__file__).resolve().parents[1]/'scripts')
            code='import sys;sys.path.insert(0,sys.argv[1]);from _common import project_lock;from pathlib import Path;\nwith project_lock(Path(sys.argv[2])): print("locked")'
            with project_lock(root):
                result=subprocess.run([sys.executable,'-c',code,source,str(root)],capture_output=True)
                self.assertNotEqual(result.returncode,0)
            self.assertEqual(subprocess.run([sys.executable,'-c',code,source,str(root)],capture_output=True).returncode,0)


if __name__=='__main__':unittest.main()
