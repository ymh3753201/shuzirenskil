import sys,unittest,tempfile
from pathlib import Path
from unittest.mock import patch,Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import provider,credential_store,private_env
from studio_transport import Adapter,TransportError
from model_registry import get_profile
from request_preview import compile_preview
class CompatibilityTests(unittest.TestCase):
 def test_error_not_id(self):
  for body in [{'request_id':'trace','error':{'message':'secret denied'}},{'request_id':'trace','base_resp':{'status_code':2013}}]:
   with patch.object(provider,'_json_request',return_value=body):
    with self.assertRaises(provider.ProviderError) as c:provider.create_task('https://example.com','/v1/videos','secret',{},60)
    self.assertNotIn('secret',str(c.exception));self.assertIn('业务错误',str(c.exception))
 def test_aihge(self):
  a=Adapter('aihge',{},'secret')
  with patch.object(a,'_call',return_value={'request_id':'task'}):self.assertEqual(a.create({})['id'],'task')
  with patch.object(a,'_call',return_value={'status':'done','video':{'url':'/v1/videos/task/content'}}):self.assertEqual(a.poll({'id':'task'})['state'],'succeeded')
  with self.assertRaises(ValueError):Adapter('aihge',{'base_url':'https://example.com'},'secret')
 def test_other_providers(self):
  for name,body in [('minimax',{'task_id':'123'}),('volcengine',{'id':'123'})]:
   a=Adapter(name,{},'secret')
   with patch.object(a,'_call',return_value=body):self.assertEqual(a.create({})['id'],'123')
   with patch.object(a,'_call',return_value={'error':{'message':'secret denied'},'request_id':'trace'}):
    with self.assertRaises(TransportError) as c:a.create({})
    self.assertNotIn('secret',str(c.exception))
 def test_seedance20(self):
  p=get_profile('seedance20-reference');args=dict(prompt='hello',resolution='720p',ratio='16:9',images=['asset://image'])
  self.assertEqual(compile_preview(p,seconds=15,**args)['payload']['model'],'doubao-seedance-2-0-260128')
  with self.assertRaises(ValueError):compile_preview(p,seconds=16,**args)
  args['images']*=10
  with self.assertRaises(ValueError):compile_preview(p,seconds=15,**args)
 def test_store(self):
  values={};b=Mock();b.get_password.side_effect=lambda s,n:values.get(n);b.set_password.side_effect=lambda s,n,v:values.update({n:v})
  with patch.object(credential_store,'backend',return_value=b):
   credential_store.save('ARK_API_KEY','dummy')
   with tempfile.TemporaryDirectory() as d,patch.dict('os.environ',{},clear=True):self.assertEqual(private_env.load_private_value('ARK_API_KEY',None,Path(d)),'dummy')
   with self.assertRaises(ValueError):credential_store.save('ARK_API_KEY','other')
 def test_reject_plaintext(self):
  fake=Mock();fake.get_keyring.return_value=Mock()
  with patch.dict(sys.modules,{'keyring':fake}):
   with self.assertRaises(ValueError):credential_store.backend()
