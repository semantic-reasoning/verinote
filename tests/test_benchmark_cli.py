"""The benchmark command is offline by default and preserves partial reports."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class Command(unittest.TestCase):
    def invoke(self,root,*args):
        return subprocess.run([sys.executable,'scripts/benchmark_extraction.py','--output',str(root),*args],
                              capture_output=True,text=True,env=os.environ|{'OPENROUTER_API_KEY':'PRIVATE_UNUSED_KEY'})
    def test_default_fake_examples_and_stopped_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve()
            result=self.invoke(root/'normal','--repetitions','2')
            self.assertEqual(result.returncode,0,result.stderr)
            report=json.loads((root/'normal'/'report.json').read_text())
            self.assertEqual(report['mode'],'fake'); self.assertEqual(report['budget']['requests_started'],6)
            self.assertNotIn('PRIVATE_UNUSED_KEY',json.dumps(report)); self.assertIn('fake completed',result.stdout)
            result=self.invoke(root/'stop','--cases','long','--max-requests','1')
            self.assertEqual(result.returncode,1)
            self.assertEqual(json.loads((root/'stop'/'report.json').read_text())['stop_reason'],'request_limit')
            result=self.invoke(root/'fail','--cases','short','--scenario','failure','--repetitions','1')
            self.assertEqual(result.returncode,1)
            self.assertEqual(json.loads((root/'fail'/'report.json').read_text())['summary']['failed_repetitions'],1)
    def test_optin_validation_and_overwrite_no_traceback(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve()
            for args in [('--mode','cli','--model','m'),('--mode','cli','--allow-model-calls'),('--agent','openrouter'),
                         ('--max-requests','0'),('--max-seconds','nan'),('--cases','../private')]:
                result=self.invoke(root/'out',*args)
                self.assertEqual(result.returncode,2)
                self.assertNotIn('Traceback',result.stderr)
                self.assertFalse((root/'out').exists())
            (root/'existing').mkdir(); sentinel=root/'existing'/'report.json'; sentinel.write_text('sentinel')
            self.assertEqual(self.invoke(root/'existing').returncode,2)
            self.assertEqual(sentinel.read_text(),'sentinel')


if __name__=='__main__': unittest.main()
