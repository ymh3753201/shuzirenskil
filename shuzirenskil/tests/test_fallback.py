import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from text_policy import guard_prompt
from fallback import definite_rejection, submit_with_fallback, validate_apiaw, can_resume_rejected_fallback
from provider import ProviderError
from workflow import _load_video_config, default_config_path

class FallbackTests(unittest.TestCase):
    def test_quota_rejection_is_explicit_but_generic_403_is_not(self):
        error='Provider HTTP 403: ' + json.dumps({'code':'insufficient_user_quota','message':'预扣费额度失败','data':None})
        self.assertTrue(definite_rejection(error))
        self.assertFalse(definite_rejection('Provider HTTP 403: Forbidden'))
        self.assertFalse(definite_rejection('Provider HTTP 502: '+error))
        job,count,_=self.run_case([ProviderError(error,ambiguous=True),'task_backup'])
        self.assertEqual(count,2)
        self.assertEqual(job['task_id'],'task_backup')

    def test_resume_rejection_preserves_attempt_and_only_posts_backup(self):
        from _common import digest_json
        error='Provider HTTP 403: '+json.dumps({'code':'insufficient_user_quota','message':'预扣费额度失败','data':None})
        backup=_load_video_config(default_config_path(),'apiaw')
        policy={'providers':[backup],'max_providers_per_segment':2}
        project={'provider_input_digest':'digest','production_authorization':{'fallback_policy':policy,'fallback_policy_digest':digest_json(policy)}}
        job={'shot_id':'shot_001','status':'submission_unknown','task_id':None,'provider_attempts':[{'provider':'91topgo','status':'submission_unknown','error':error}]}
        self.assertTrue(can_resume_rejected_fallback(job,project))
        with tempfile.TemporaryDirectory() as temp, patch('fallback.create_task',return_value='task_backup') as post, patch('fallback.apiaw_payload',return_value={'prompt':guard_prompt('test')}), patch('workflow._load_private_provider_inputs',return_value={'shot_001':{}}):
            submit_with_fallback(job,{'jobs':[job]},Path(temp)/'ledger.json',None,{'prompt':guard_prompt('test')},project,argparse.Namespace(project_dir=Path(temp),timeout=3),Mock(return_value='test'),Mock(),Mock())
        self.assertEqual(post.call_count,1)
        self.assertEqual(job['provider_attempts'][0]['previous_status'],'submission_unknown')
        self.assertEqual(job['provider_attempts'][0]['error'],error)
        self.assertEqual(job['provider_attempts'][1]['provider'],'apiaw')
        job['task_id']=None
        self.assertFalse(can_resume_rejected_fallback(job,project))

    def run_case(self, errors):
        chain=json.loads(default_config_path().read_text())
        primary=_load_video_config(default_config_path(), '91topgo')
        backup=_load_video_config(default_config_path(), 'apiaw')
        job={'shot_id':'shot_001'};ledger={'jobs':[job]}
        project={'provider_input_digest':'digest','production_authorization':{'fallback_policy':{'providers':[backup],'max_providers_per_segment':2}}}
        from _common import digest_json
        project['production_authorization']['fallback_policy_digest']=digest_json(project['production_authorization']['fallback_policy'])
        keys=[]
        def key(c,a):keys.append(c['api_key_env']);return 'test-only'
        with tempfile.TemporaryDirectory() as temp, patch('fallback.create_task',side_effect=errors) as post, patch('fallback.apiaw_payload',return_value={'prompt':guard_prompt('test')}), patch('workflow._load_private_provider_inputs',return_value={'shot_001':{}}):
            args=argparse.Namespace(project_dir=Path(temp),timeout=3)
            try:submit_with_fallback(job,ledger,Path(temp)/'ledger.json',primary,{'prompt':guard_prompt('test')},project,args,key,Mock(),Mock())
            except ProviderError:pass
            self.assertTrue((Path(temp)/'ledger.json').exists())
            return job,post.call_count,keys
    def test_explicit_model_rejection_falls_back_once(self):
        error=ProviderError('Provider HTTP 400: unknown provider for model model_not_found',ambiguous=True)
        job,count,keys=self.run_case([error,'task_backup'])
        self.assertEqual(count,2);self.assertEqual(job['task_id'],'task_backup')
        self.assertEqual(keys,['SHUZIRENSKIL_91TOPGO_API_KEY','SHUZIRENSKIL_APIAW_API_KEY'])
        self.assertEqual(len(job['provider_attempts']),2)
    def test_unknown_submission_never_falls_back(self):
        for error in ['Provider HTTP 502: upstream failed','网络超时','缺少任务编号']:
            job,count,_=self.run_case([ProviderError(error,ambiguous=True)])
            self.assertEqual(count,1);self.assertEqual(job['status'],'submission_unknown')
    def test_success_does_not_fallback(self):
        job,count,_=self.run_case(['task_primary'])
        self.assertEqual(count,1)
    def test_all_providers_failed_stops(self):
        job,count,_=self.run_case([ProviderError('rejected'),ProviderError('rejected')])
        self.assertEqual(count,2);self.assertEqual(job['status'],'failed')
    def test_apiaw_failure_uses_91topgo_protocol(self):
        from _common import digest_json
        primary=_load_video_config(default_config_path(),'apiaw')
        backup=_load_video_config(default_config_path(),'91topgo')
        policy={'providers':[backup],'max_providers_per_segment':2}
        project={'provider_input_digest':'digest','production_authorization':{'fallback_policy':policy,'fallback_policy_digest':digest_json(policy)}}
        job={'shot_id':'shot_001'};ledger={'jobs':[job]}
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);dry=root/'requests/dry-run';dry.mkdir(parents=True)
            (dry/'shot_001.json').write_text(json.dumps({'payload':{'seconds':'15'}}))
            payload={'model':primary['model'],'prompt':guard_prompt('confirmed'),'seconds':'15','aspect_ratio':'9:16','resolution':'720p','image':'data:image/jpeg;base64,fixture'}
            refs={'shot_001':{'mode':'reference-to-video','reference_images':[{'local_path':'fixture'}]}}
            with patch('fallback.create_task',side_effect=[ProviderError('rejected'),'task_backup']) as post, patch('workflow._load_private_provider_inputs',return_value=refs), patch('workflow._provider_reference_payload',return_value={'url':'data:image/jpeg;base64,fixture'}):
                submit_with_fallback(job,ledger,root/'jobs.json',primary,payload,project,argparse.Namespace(project_dir=root,timeout=3),Mock(return_value='test'),Mock(),Mock())
                outgoing=post.call_args.args[3]
                self.assertEqual(job['provider_config']['provider_id'],'91topgo')
                self.assertNotIn('image',outgoing)
                self.assertEqual(outgoing['reference_images'],[{'url':'data:image/jpeg;base64,fixture'}])
                self.assertEqual(outgoing['seconds'],'15')

    def test_redirect_strips_cross_host_credentials(self):
        import urllib.request
        from provider import _SafeMediaRedirect
        req=urllib.request.Request('https://newapi.apiaw.com/content', headers={'Authorization':'Bearer test'})
        redirected=_SafeMediaRedirect().redirect_request(req,None,302,'Found',{},'https://cdn.invalid/video.mp4')
        self.assertFalse(redirected.has_header('Authorization'))
    def test_download_metadata_url(self):
        from provider import extract_video_url
        self.assertEqual(extract_video_url({'metadata':{'url':'https://cdn.invalid/video.mp4'}}),'https://cdn.invalid/video.mp4')
    def test_payload_keeps_seconds_string_and_single_image(self):
        from fallback import apiaw_payload
        from _common import sha256_file
        config=_load_video_config(default_config_path(),'apiaw')
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'ref.png';path.write_bytes(b'test-fixture')
            ref={'local_path':str(path),'sha256':sha256_file(path)}
            payload={'model':config['model'],'seconds':'15','resolution':'720p','aspect_ratio':'9:16','prompt':guard_prompt('confirmed')}
            with patch('fallback.local_image_data_uri',return_value='data:image/jpeg;base64,abc'):
                out=apiaw_payload(payload,{'mode':'reference-to-video','reference_images':[ref]},config)
                self.assertEqual(out['seconds'],'15');self.assertNotIn('reference_images',out)
                with self.assertRaises(ValueError):
                    apiaw_payload(payload,{'mode':'reference-to-video','reference_images':[ref,ref]},config)
    def test_host_and_key_are_bound(self):
        config=_load_video_config(default_config_path(),'apiaw');validate_apiaw(config)
        config['base_url']='https://other.invalid'
        with self.assertRaises(ValueError):validate_apiaw(config)

if __name__=='__main__':unittest.main()
