import sys, unittest, tempfile, hashlib, io, urllib.error
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from provider_contract import task_identifier,task_state,image_transport
from studio_transport import Adapter,request,TransportError
from studio_assets import resolve
class ContractTests(unittest.TestCase):
 def test_provider_ids_are_not_interchangeable(self):
  examples={'minimax':{'task_id':'a'},'volcengine':{'id':'a'},'google':{'name':'a'},'aliyun':{'output':{'task_id':'a'}},'fal':{'request_id':'a'},'aihge':{'request_id':'a'},'91topgo':{'data':{'id':'a'}}}
  for name,body in examples.items():
   with self.subTest(name=name):self.assertEqual(task_identifier(name,body),'a')
  for name in ['minimax','volcengine','google','aliyun']:
   self.assertIsNone(task_identifier(name,{'request_id':'trace-only'}))
  self.assertIsNone(task_identifier('aihge',{'id':'unrelated'}))
  self.assertEqual(image_transport('aihge'),'https_required')
  with self.assertRaises(ValueError):image_transport('future-provider')
 def test_state_scope(self):
  self.assertEqual(task_state('aihge','done'),'succeeded')
  self.assertEqual(task_state('volcengine','expired'),'failed')
  self.assertEqual(task_state('minimax','succeeded'),'succeeded')
  for name in ['minimax','volcengine','aliyun']:
   with self.assertRaises(ValueError):task_state(name,'done')
 def test_parser_per_provider(self):
  responses={'minimax':{'task':{'status':'succeeded','content':{'url':'https://example.com/a.mp4'}}},'volcengine':{'status':'succeeded','content':{'video_url':'https://example.com/a.mp4'}},'aliyun':{'output':{'task_status':'SUCCEEDED','video_url':'https://example.com/a.mp4'}}}
  for name,body in responses.items():
   config={'base_url':'https://space.cn-beijing.maas.aliyuncs.com'} if name=='aliyun' else {}
   a=Adapter(name,config,'test')
   with patch.object(a,'_call',return_value=body):self.assertEqual(a.poll({'id':'a'})['state'],'succeeded')
 def test_short_lived_image_stops_before_get(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'image.png';p.write_bytes(b'image');sha=hashlib.sha256(b'image').hexdigest()
   b={sha:{'url':'https://example.com/image.png','expires_at':(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat()}}
   with patch('studio_assets.request') as get:
    with self.assertRaisesRegex(ValueError,'有效期不足'):resolve(p,sha,b)
    get.assert_not_called()
 def test_valid_image_get_is_local_verification_only(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'image.png';p.write_bytes(b'image');sha=hashlib.sha256(b'image').hexdigest()
   b={sha:{'url':'https://example.com/image.png','expires_at':(datetime.now(timezone.utc)+timedelta(hours=2)).isoformat()}}
   with patch('studio_assets.request',return_value=b'image') as get:
    self.assertEqual(resolve(p,sha,b),b[sha]['url']);get.assert_called_once()
 def test_http_error_metadata_and_redaction(self):
  error=urllib.error.HTTPError('https://example.com',502,'gateway',{'x-request-id':'trace-123'},io.BytesIO(b'{"error":{"message":"secret-token"}}'))
  d={}
  with patch('urllib.request.OpenerDirector.open',side_effect=error):
   with self.assertRaises(TransportError) as c:request('POST','https://example.com',headers={'Authorization':'Bearer secret-token'},payload={},diagnostic=d)
  self.assertNotIn('secret-token',str(c.exception));self.assertEqual(d['http_status'],502);self.assertEqual(d['trace_id'],'trace-123');self.assertIn('elapsed_seconds',d)
if __name__=='__main__':unittest.main()
