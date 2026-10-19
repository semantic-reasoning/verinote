"""No inference: real bounded subprocesses implement catalog-only fixtures."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import test_workflow as workflow
from verinote.model_catalog import ModelCatalog, _args, _normalize, CatalogError

FIXTURE = r'''
import json,os,sys,time
mode=os.environ.get('CATALOG_CASE','ok')
key=os.environ.get('OPENROUTER_API_KEY')
with open(os.environ['CATALOG_AUDIT'],'w') as f:json.dump({'args':sys.argv[1:],'key_present':bool(key),'pid':os.getpid()},f)
if mode=='timeout':time.sleep(30)
if mode=='stdout':sys.stdout.write('x'*2000000);sys.stdout.flush();time.sleep(30)
if mode=='stderr':sys.stderr.write('private-fixture-secret'*30000);sys.stderr.flush();time.sleep(30)
if mode=='error':print('private-fixture-secret',file=sys.stderr);sys.exit(1)
if mode=='malformed':print('not a model inventory');sys.exit(0)
if 'models' in sys.argv:
 print('vendor/a\tModel A\nvendor/a\tDuplicate\nvendor/b\tModel B');sys.exit(0)
if '--list-models' in sys.argv:
 print('Available models\n\nvendor/a - Model A\nvendor/b - Model B');sys.exit(0)
framed='--stdio' in sys.argv
while True:
 if framed:
  header=sys.stdin.buffer.readline()
  if not header:break
  length=int(header.decode().split(':',1)[1]);sys.stdin.buffer.readline()
  message=json.loads(sys.stdin.buffer.read(length))
 else:
  line=sys.stdin.readline()
  if not line:break
  message=json.loads(line)
 with open(os.environ['CATALOG_MESSAGES'],'a') as f:f.write(json.dumps(message)+'\n')
 if message.get('method')=='initialized':continue
 if 'request_id' in message:
  output={'type':'control_response','response':{'subtype':'success','request_id':message['request_id'],'response':{'models':[{'value':'vendor/a','displayName':'Model A'}],'account':{'private':'not public'}}}}
 else:
  method=message['method']
  if method=='models.list':result={'models':[{'id':'vendor/a','name':'Model A'}]}
  elif method=='model/list':
   cursor=message['params'].get('cursor')
   result={'data':[{'model':'vendor/b' if cursor else 'vendor/a','displayName':'Model'}],'nextCursor':None if cursor else 'next-page'}
  else:result={'protocolVersion':3}
  output={'jsonrpc':'2.0','id':message['id'],'result':result}
 if mode=='rpc_error':output={'id':message.get('id'),'error':{'code':-1,'message':'private-fixture-secret'}}
 raw=json.dumps(output).encode()
 if mode=='frame_size':
  sys.stdout.buffer.write(b'Content-Length: 99999999\r\n\r\n');sys.stdout.buffer.flush();time.sleep(30)
 if framed:sys.stdout.buffer.write(b'Content-Length: '+str(len(raw)).encode()+b'\r\n\r\n'+raw)
 else:sys.stdout.buffer.write(raw+b'\n')
 sys.stdout.buffer.flush()
'''


class Catalog(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.script=self.root/'fixture.py';self.script.write_text(FIXTURE)
        self.env={'CATALOG_AUDIT':str(self.root/'audit.json'),'CATALOG_MESSAGES':str(self.root/'messages.jsonl'),
                  'OPENROUTER_API_KEY':'private-fixture-secret'}

    def discover(self,agent='agy',case='ok',timeout=2):
        with patch.dict(os.environ,self.env|{'CATALOG_CASE':case}), patch('verinote.model_catalog.executable',return_value=sys.executable), \
             patch('verinote.model_catalog._args',return_value=[str(self.script),*_args(agent)]):
            return ModelCatalog(timeout).discover(agent)

    def test_each_protocol_and_pagination_without_prompts_or_key_environment(self):
        for agent in ('agy','cursor','claude','codex','copilot'):
            with self.subTest(agent=agent):
                (self.root/'messages.jsonl').unlink(missing_ok=True)
                result=self.discover(agent)
                self.assertEqual(result['status'],'ok',result)
                self.assertEqual(result['models'][0]['id'],'vendor/a')
                if agent in ('agy','cursor','codex'):self.assertEqual(len(result['models']),2)
                audit=json.loads((self.root/'audit.json').read_text());self.assertFalse(audit['key_present'])
                # Every process is reaped on successful inventory, too.
                if os.name == 'posix':
                    with self.assertRaises(ProcessLookupError):os.kill(audit['pid'],0)
                if (self.root/'messages.jsonl').exists():
                    messages=[json.loads(x) for x in (self.root/'messages.jsonl').read_text().splitlines()]
                    self.assertTrue(all(m.get('method') in (None,'initialize','initialized','model/list','connect','models.list') for m in messages))
                    self.assertNotIn('prompt',json.dumps(messages))
                    if agent=='codex':self.assertEqual([m.get('method') for m in messages],['initialize','initialized','model/list','model/list'])
                self.assertNotIn('account',json.dumps(result));self.assertNotIn('private',json.dumps(result))

    @unittest.skipUnless(os.name == 'nt','Windows command shim test.')
    def test_windows_copilot_cmd_shim(self):
        shim=self.root/'copilot.cmd'
        shim.write_text(f'@echo off\r\n"{sys.executable}" "{self.script}" %*\r\n')
        with patch.dict(os.environ,self.env),patch('verinote.model_catalog.executable',return_value=str(shim)):
            result=ModelCatalog().discover('copilot')
        self.assertEqual(result['status'],'ok',result)
        self.assertEqual(result['models'][0]['id'],'vendor/a')

    def test_deadline_output_bounds_and_safe_errors_reap_children(self):
        for case,status in [('timeout','timeout'),('stdout','too_large'),('stderr','too_large'),('error','unavailable'),('malformed','unavailable')]:
            with self.subTest(case=case):
                start=time.monotonic();result=self.discover('agy',case=case,timeout=.5)
                self.assertEqual(result['status'],status,result);self.assertLess(time.monotonic()-start,1.5)
                self.assertEqual(result['models'],[]);self.assertNotIn('private-fixture-secret',json.dumps(result))
                audit=json.loads((self.root/'audit.json').read_text())
                if os.name == 'posix':
                    with self.assertRaises(ProcessLookupError):os.kill(audit['pid'],0)
        self.assertEqual(self.discover()['status'],'ok')

    def test_normalization_missing_install_unknown_provider_and_bad_shape(self):
        self.assertEqual(_normalize([(' vendor/a ','A\x1b\u200b'),('vendor/a','duplicate'),('bad\nmodel','bad')]),[{'id':'vendor/a','name':'A'}])
        with self.assertRaises(CatalogError):_normalize([('bad\nmodel','bad')])
        self.assertEqual(_normalize([]),[])
        with patch('verinote.model_catalog.executable',return_value=None),patch('verinote.model_catalog._Process') as proc:
            self.assertEqual(ModelCatalog().discover('agy')['status'],'not_installed')
            for name in ('openrouter','bad;cmd'):
                with self.assertRaises(ValueError):ModelCatalog().discover(name)
            proc.assert_not_called()

    def test_framed_errors_and_oversized_headers_remain_safe(self):
        self.assertEqual(self.discover('copilot',case='rpc_error')['status'],'unavailable')
        self.assertEqual(self.discover('copilot',case='frame_size')['status'],'too_large')

    def test_protocol_rejects_errors_wrong_ids_shapes_and_cursor_loops(self):
        from verinote.model_catalog import _read_catalog
        process=Mock()
        process.rpc.return_value={'data':[],'nextCursor':'repeated'}
        with self.assertRaises(CatalogError):_read_catalog('codex',process)
        process.receive.side_effect=[{'type':'control_response','response':{'subtype':'success','request_id':'wrong'}},
                                     {'type':'control_response','response':{'subtype':'success','request_id':'catalog','response':{'models':'invalid'}}}]
        with self.assertRaises(CatalogError):_read_catalog('claude',process)
        self.assertEqual(process.receive.call_count,2)

    def test_concurrent_limits_same_provider_and_capacity_release(self):
        entered=threading.Barrier(3);release=threading.Event();catalog=ModelCatalog()
        def blocked(*args):entered.wait(timeout=2);release.wait(timeout=2);return []
        results=[]
        with patch('verinote.model_catalog.executable',return_value='/fake'),patch('verinote.model_catalog._Process'),patch('verinote.model_catalog._read_catalog',side_effect=blocked):
            threads=[threading.Thread(target=lambda a=a:results.append(catalog.discover(a))) for a in ('agy','cursor')]
            for thread in threads:thread.start()
            entered.wait(timeout=2)
            self.assertEqual(catalog.discover('agy')['status'],'busy')
            self.assertEqual(catalog.discover('codex')['status'],'busy')
            release.set()
            for thread in threads:thread.join(timeout=2);self.assertFalse(thread.is_alive())
        self.assertEqual([r['status'] for r in results],['empty','empty'])
        with patch('verinote.model_catalog.executable',return_value=None):self.assertEqual(catalog.discover('agy')['status'],'not_installed')


class CatalogAPI(unittest.TestCase):
    setUp=workflow.HTTP.setUp
    tearDown=workflow.HTTP.tearDown
    request=workflow.HTTP.request

    def test_only_cli_empty_body_same_origin_and_safe_result(self):
        catalog=self.server.model_catalog
        result={'status':'ok','models':[{'id':'vendor/model','name':'Model'}],'message':'catalog'}
        with patch.object(catalog,'discover',return_value=result) as discover:
            self.assertEqual(self.request('/api/providers/agy/models','POST',{}),result)
            for name,data,headers,code in [('openrouter',{},None,400),('unknown',{},None,400),('agy',{'args':['danger']},None,400),('agy',{}, {'Origin':'https://elsewhere.test'},403)]:
                with self.assertRaises(HTTPError) as caught:self.request('/api/providers/'+name+'/models','POST',data,headers)
                self.assertEqual(caught.exception.code,code);caught.exception.close()
            discover.assert_called_once_with('agy')
