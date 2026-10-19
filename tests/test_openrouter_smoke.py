"""Opt-in CLI contract is tested offline; importing it never sends a request."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import unittest
from unittest.mock import patch

from scripts.openrouter_smoke import main
from verinote.transports import HTTPResult, SafeCallResult


class Smoke(unittest.TestCase):
    def test_missing_explicit_opt_in_never_loads_or_calls(self):
        with patch('scripts.openrouter_smoke.load_config') as load, patch('scripts.openrouter_smoke.OpenRouter') as adapter:
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(['--config','unused','--model','vendor/model'])
            load.assert_not_called(); adapter.assert_not_called()

    def test_single_synthetic_request_safe_output_and_failure_exit(self):
        from verinote.config import Credentials
        for text, code in [('{"ok":true}',0),('{"ok":1}',1),('{"ok":1.0}',1),('{"unexpected":"not printed"}',1)]:
            output=io.StringIO()
            result=HTTPResult(SafeCallResult('fixture','success',{'http_status':200,'usage':{'cost':0}},True),text)
            with patch('scripts.openrouter_smoke.load_config',return_value=Credentials('sk-or-smoke-sentinel')), \
                 patch('scripts.openrouter_smoke.OpenRouter') as adapter, redirect_stdout(output):
                adapter.return_value.complete.return_value=result
                self.assertEqual(main(['--allow-network','--config','unused','--model','vendor/model']),code)
                adapter.return_value.complete.assert_called_once()
                args,kwargs=adapter.return_value.complete.call_args
                self.assertEqual(args[:2],('openrouter','vendor/model'))
                self.assertNotIn('sk-or-smoke-sentinel',args[2]); self.assertEqual(kwargs['options'].max_tokens,256)
                self.assertEqual(kwargs['options'].timeout,60)
            self.assertNotIn('sk-or-smoke-sentinel',output.getvalue())
            self.assertNotIn('not printed',output.getvalue()); self.assertEqual(json.loads(output.getvalue())['http_status'],200)

    def test_config_error_is_static(self):
        from verinote.config import ConfigError
        output=io.StringIO()
        with patch('scripts.openrouter_smoke.load_config',side_effect=ConfigError('private diagnostic')), redirect_stdout(output):
            self.assertEqual(main(['--allow-network','--config','unused','--model','vendor/model']),1)
        self.assertNotIn('private diagnostic',output.getvalue())
