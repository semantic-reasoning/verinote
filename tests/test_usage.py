"""Telemetry from real protocol shapes, accounting boundaries and retained history."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import test_workflow as workflow
from test_workflow import SOURCE, PAGE, FACTS, DICTIONARY, legacy_run
from verinote.agents import CLI, parse_response
from verinote.service import Service
from verinote.usage import (MAX_METADATA, atomic_json, calculate, decode_transport,
                            empty_usage, job_calls, summarize, validate_rates)

FIXTURES = Path(__file__).parent / 'fixtures' / 'usage'
RATE = dict(model='', input=2, output=10, cache_read=.2, cache_write=2.5,
            credit_unit='Test credits', usd_per_credit=.01)


def fixture(agent):
    return (FIXTURES / (agent + ('.jsonl' if agent in ('cursor', 'codex') else '.json'))).read_text()


def record(agent='agy', model='', **updates):
    result = empty_usage(agent, model)
    result.update(tokens=dict(input=100, output=20, cache_read=30, cache_write=10, reasoning=5),
                  status='success', complete=True, source='synthetic')
    return result | updates


class Protocols(unittest.TestCase):
    def test_all_five_observed_protocols_and_partition_totals(self):
        expected = {'agy': (12654, 74), 'claude': (7342, 9), 'cursor': (23990, 9), 'codex': (17284, 9), 'copilot': (13646, 9)}
        for agent, totals in expected.items():
            with self.subTest(agent=agent):
                raw = '{"ok":true}' if agent == 'copilot' else fixture(agent)
                sidecar = json.loads(fixture(agent)) if agent == 'copilot' else None
                answer, usage, error = decode_transport(agent, raw, sidecar)
                self.assertEqual(json.loads(answer), {'ok': True})
                self.assertIsNone(error)
                self.assertEqual((usage['tokens']['input'], usage['tokens']['output']), totals)
                self.assertTrue(usage['complete'])
                if agent == 'copilot':
                    self.assertEqual(usage['reported_credits'], .1710175)
                    self.assertEqual(usage['credit_unit'], 'GitHub AI credits')
                if agent == 'claude':
                    self.assertEqual(usage['reported_usd'], .13445225)
                    self.assertEqual(usage['resolved_model'], 'claude-fable-5-1')

    def test_model_generated_usage_is_never_telemetry(self):
        body = json.dumps({'ok': True, 'usage': {'input_tokens': 999999}, 'total_cost_usd': 999})
        for agent, raw in [('agy', json.dumps({'status': 'SUCCESS', 'response': body})),
                           ('claude', json.dumps({'type': 'result', 'result': body})), ('copilot', body)]:
            answer, u, _ = decode_transport(agent, raw)
            self.assertEqual(json.loads(answer)['total_cost_usd'], 999)
            self.assertIsNone(u['reported_usd'])
            self.assertIsNone(u['tokens']['input'])
            self.assertFalse(u['complete'])

    def test_zero_invalid_partial_and_reasoning_are_not_double_counted(self):
        for value in [True, -1, 1.2, float('nan'), float('inf'), 10**400]:
            raw = {'status': 'SUCCESS', 'response': '{}', 'usage': {'input_tokens': value, 'output_tokens': 0, 'cache_read_tokens': 0}}
            _, u, _ = decode_transport('agy', json.dumps(raw))
            self.assertIsNone(u['tokens']['input'])
            self.assertFalse(u['complete'])
        raw = {'status': 'SUCCESS', 'response': '{}', 'usage': {'input_tokens': 278, 'output_tokens': 4, 'cache_read_tokens': 30214, 'thinking_tokens': 3}}
        _, u, _ = decode_transport('agy', json.dumps(raw))
        self.assertEqual(u['tokens']['input'], 30492)
        self.assertEqual(u['tokens']['output'], 4)
        raw['usage'] = {'input_tokens': 0, 'output_tokens': 0, 'cache_read_tokens': 0}
        _, u, _ = decode_transport('agy', json.dumps(raw))
        self.assertTrue(u['complete']); self.assertEqual(u['tokens']['input'], 0)

    def test_final_snapshot_wins_over_intermediate_or_duplicate_events(self):
        raw = fixture('cursor')
        result = json.loads(raw.splitlines()[-1])
        answer, u, _ = decode_transport('cursor', raw + json.dumps(result) + '\n{"truncated":')
        self.assertEqual(u['tokens']['input'], 23990)
        self.assertEqual(json.loads(answer), {'ok': True})
        raw = fixture('codex')
        _, u, _ = decode_transport('codex', raw + raw.splitlines()[-1])
        self.assertEqual(u['tokens']['input'], 17284)
        for agent in ('agy', 'claude', 'cursor', 'codex'):
            answer, u, _ = decode_transport(agent, '{broken')
            self.assertIsNone(answer); self.assertFalse(u['complete'])

    def test_rates_model_matching_snapshot_and_credit_units(self):
        rate = copy.deepcopy(RATE)
        u = record(rate_snapshot=rate)
        calculate(u)
        self.assertAlmostEqual(u['estimated_usd'], .000351)
        self.assertAlmostEqual(u['estimated_credits'], .0351)
        self.assertEqual(u['rate_basis'], 'configured_default_assumption')
        u = record(model='specified', rate_snapshot=copy.deepcopy(RATE))
        calculate(u); self.assertIsNone(u['estimated_usd'])
        for actual, expected in [('specified', .000351), ('different', None), ('(multiple models)', None)]:
            u = record(model='specified', resolved_model=actual, rate_snapshot=RATE | {'model': 'specified'})
            calculate(u); self.assertEqual(u['estimated_usd'], expected)
        u = record(rate_snapshot=RATE, tokens=dict.fromkeys(['input','output','cache_read','cache_write','reasoning']))
        calculate(u); self.assertIsNone(u['estimated_usd'])
        for delta in [{'input': True}, {'output': -1}, {'cache_write': float('inf')}, {'input': 10**400}, {'usd_per_credit': 0}, {'credit_unit': '', 'usd_per_credit': 1}, {'extra': 3}]:
            with self.assertRaises(ValueError): validate_rates({'agy': RATE | delta})
        for invalid in [None, [], {'other': RATE}]:
            with self.assertRaises(ValueError): validate_rates(invalid)

    def test_units_and_unknown_coverage_never_become_zero_or_one_total(self):
        calls = [record(reported_usd=.1, reported_credits=1, credit_unit='A'),
                 record(reported_usd=0, reported_credits=2, credit_unit='B'), empty_usage('agy','')]
        result = summarize(calls)
        self.assertEqual(result['reported_usd'], {'value': .1, 'known_calls': 2})
        self.assertEqual([(c['unit'], c['value']) for c in result['credits']], [('A',1),('B',2)])
        self.assertIsNone(result['estimated_usd']['value'])
        self.assertEqual(result['incomplete_calls'], 1)

    def test_derived_amount_bounds_and_small_nonzero_subtotals(self):
        u=calculate(record(rate_snapshot=RATE|{'usd_per_credit':5e-324}))
        self.assertIsNone(u['estimated_credits']);self.assertTrue(u['warnings'])
        self.assertIsNotNone(u['estimated_usd'])
        u=calculate(record(rate_snapshot=RATE|dict(input=1e-20,output=1e-20,cache_read=1e-20,cache_write=1e-20)))
        self.assertGreater(u['estimated_usd'],0)
        self.assertGreater(summarize([u])['estimated_usd']['value'],0)
        json.dumps(u,allow_nan=False)
        u=calculate(record(rate_snapshot=RATE|dict(input=5e-324,output=5e-324,cache_read=5e-324,cache_write=5e-324)))
        self.assertIsNone(u['estimated_usd']);self.assertTrue(u['warnings'])


class Transport(unittest.TestCase):
    def test_real_synthetic_executable_preserves_answer_and_provider_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for agent in ['agy','claude','cursor','codex','copilot']:
                script = root / ('fake-' + agent)
                raw = '{"ok":true}' if agent == 'copilot' else fixture(agent)
                body = 'import pathlib,sys,json\n'
                if agent == 'copilot':
                    body += f"pathlib.Path(sys.argv[sys.argv.index('--usage-output-file')+1]).write_text({fixture(agent)!r})\n"
                body += f'print({raw!r})\n'
                script.write_text('#!/usr/bin/env python3\n' + body); script.chmod(0o755)
                rates = {agent: copy.deepcopy(RATE)}
                def launching(_):
                    rates[agent]['input'] = 777  # Settings change after the invocation snapshot.
                    return str(script)
                with patch('verinote.agents.executable', side_effect=launching):
                    self.assertEqual(CLI(rates=lambda:rates).complete(agent,'','synthetic',root/agent), {'ok':True})
                u = json.loads((root/agent/'usage.json').read_text())
                self.assertEqual(u['status'],'success'); self.assertTrue(u['complete'])
                self.assertEqual(u['rate_snapshot'],RATE)
                rates[agent]['input']=999
                self.assertEqual(json.loads((root/agent/'usage.json').read_text())['rate_snapshot']['input'],2)

    def test_failures_keep_telemetry_without_changing_original_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); script=root/'fake'
            for name, tail, response, exception in [('json','', 'not json', json.JSONDecodeError), ('exit','sys.exit(7)', '{}', ValueError), ('timeout','time.sleep(5)', '{}', TimeoutError), ('overflow',"print('x'*5000)", '{}', ValueError)]:
                raw=json.dumps({'status':'SUCCESS','response':response,'usage':{'input_tokens':50,'output_tokens':3,'cache_read_tokens':10}})
                script.write_text(f'#!/usr/bin/env python3\nimport sys,time\nprint({raw!r},flush=True)\n{tail}\n');script.chmod(0o755)
                with patch('verinote.agents.executable',return_value=str(script)),self.assertRaises(exception):
                    CLI(timeout=1 if name == 'timeout' else 5,max_output=1000).complete('agy','','synthetic',root/name)
                u=json.loads((root/name/'usage.json').read_text())
                self.assertEqual(u['status'],'error')
                if name in ('json','exit','timeout'):self.assertEqual(u['tokens']['input'],60)
                if name in ('exit','timeout'):self.assertFalse(u['complete'])
            with patch('verinote.agents.executable',return_value=None),self.assertRaises(ValueError):
                CLI().complete('agy','','test',root/'missing')
            u=json.loads((root/'missing/usage.json').read_text());self.assertEqual(u['status'],'not_started');self.assertIsNone(u['tokens']['input'])

    def test_telemetry_write_failure_preserves_primary_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);script=root/'fake'
            script.write_text('#!/usr/bin/env python3\nprint(\'{"status":"SUCCESS","response":"invalid JSON"}\')\n');script.chmod(0o755)
            def write(path,value):
                if Path(path).name=='usage.json' and value.get('finished_at'):
                    raise OSError('disk unavailable')
                atomic_json(path,value)
            with patch('verinote.agents.executable',return_value=str(script)),patch('verinote.agents.atomic_json',side_effect=write),self.assertRaises(json.JSONDecodeError):
                CLI().complete('agy','','synthetic',root/'call')

    def test_credit_conversion_overflow_cannot_fail_cli_or_mask_json_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);script=root/'fake'
            rates=validate_rates({'agy':RATE|{'usd_per_credit':5e-324}})
            for name,body in [('success','{"ok":true}'),('failure','not json')]:
                raw=json.loads(fixture('agy'));raw['response']=body
                script.write_text('#!/usr/bin/env python3\nprint('+repr(json.dumps(raw))+')\n');script.chmod(0o755)
                with patch('verinote.agents.executable',return_value=str(script)):
                    if name=='success':self.assertEqual(CLI(rates=lambda:rates).complete('agy','','synthetic',root/name),{'ok':True})
                    else:
                        with self.assertRaises(json.JSONDecodeError):CLI(rates=lambda:rates).complete('agy','','synthetic',root/name)
                usage=json.loads((root/name/'usage.json').read_text())
                self.assertIsNone(usage['estimated_credits']);self.assertIsNotNone(usage['estimated_usd'])
                self.assertEqual(usage['status'],'success' if name=='success' else 'error')
                self.assertTrue(usage['warnings'])

    def test_telemetry_write_failure_preserves_success_and_exposes_incomplete_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);script=root/'fake';key='a'*32
            script.write_text('#!/usr/bin/env python3\nprint('+repr(fixture('agy'))+')\n');script.chmod(0o755)
            def write(path,value):
                if Path(path).name=='usage.json' and value.get('finished_at'):
                    raise OSError('sidecar storage unavailable')
                atomic_json(path,value)
            folder=root/'runs'/key/'1'
            with patch('verinote.agents.executable',return_value=str(script)),patch('verinote.agents.atomic_json',side_effect=write):
                self.assertEqual(CLI().complete('agy','','synthetic',folder),{'ok':True})
            meta=json.loads((folder/'run.json').read_text())
            self.assertEqual(meta['status'],'success');self.assertIn('불완전',meta['usage_warning'])
            calls=job_calls(root,'runs',dict(id=key,agent='agy',model='',stage='ontology',status='success'))
            self.assertEqual(summarize(calls)['incomplete_calls'],1)
            self.assertIsNone(summarize(calls)['tokens']['input']['value'])
            self.assertIn(meta['usage_warning'],calls[0]['warnings'])


class ResponseFraming(unittest.TestCase):
    def test_recovers_only_known_provider_framing(self):
        body = '{"tools":[{"name":"query_graph","arguments":{}}]}'
        for agent in ('agy', 'claude', 'cursor', 'codex', 'copilot'):
            for raw in (body, '\x1b[32m```json\n' + body + '\n```\x1b[0m'):
                self.assertEqual(parse_response(agent, raw), (json.loads(body), []))
        for raw in (body + body, ' \n' + body + '\n ' + body + '\n'):
            self.assertEqual(parse_response('cursor', raw), (json.loads(body), ['cursor_duplicate_json_object']))
        self.assertEqual(parse_response('copilot', '<final_answer>\n\n</final_answer>' + body),
                         (json.loads(body), ['copilot_empty_final_answer_prefix']))
        literal = {'text': '<final_answer>keep me</final_answer>', 'tools': [{}, {}]}
        self.assertEqual(parse_response('copilot', json.dumps(literal)), (literal, []))

    def test_rejects_ambiguous_or_unrecognized_framing(self):
        body = '{"ok":true}'
        cursor_bad = [body + '{"ok":1}', body + '{ "ok":true}', body * 3, body + '{',
                      body + '[]', body + 'null', body * 2 + ' trailing', '[] []', '1 1',
                      body + '{"ok":false,"ok":true}', 'prose ' + body,
                      '{"a":1,"b":2}{"b":2,"a":1}']
        copilot_bad = ['<final_answer>nonempty</final_answer>' + body,
                       '<final_answer><final_answer></final_answer></final_answer>' + body,
                       '<final_answer></final_answer>' * 2 + body,
                       '<final_answer>' + body, '<final_answer>' + body + '</final_answer>',
                       '<final_answer></final_answer>' + body + ' trailing']
        for agent, values in [('cursor', cursor_bad), ('copilot', copilot_bad)]:
            for raw in values:
                with self.subTest(agent=agent, raw=raw), self.assertRaises(ValueError):
                    parse_response(agent, raw)
        for agent in ('agy', 'claude', 'cursor', 'codex', 'copilot'):
            for raw in ('[]', 'true', '1', 'null'):
                with self.subTest(agent=agent, raw=raw), self.assertRaises(ValueError):
                    parse_response(agent, raw)
            if agent != 'cursor':
                with self.assertRaises(ValueError): parse_response(agent, body * 2)
            if agent != 'copilot':
                with self.assertRaises(ValueError): parse_response(agent, '<final_answer></final_answer>' + body)

    def test_recovery_preserves_terminal_status_raw_logs_and_usage(self):
        body = '{"ok":true}'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for agent in ('cursor', 'copilot'):
                raw = body * 2 if agent == 'cursor' else '<final_answer>\n\n</final_answer>' + body
                sidecar = json.loads(fixture(agent)) if agent == 'copilot' else None
                for state in ('normal', 'recovered', 'exit', 'provider_error'):
                    if agent == 'copilot' and state == 'provider_error': continue
                    output = body if state == 'normal' else raw
                    if agent == 'cursor':
                        events = [json.loads(line) for line in fixture(agent).splitlines()]
                        events[-1]['result'] = output
                        if state == 'provider_error': events[-1].update(subtype='error', is_error=True)
                        output = '\n'.join(json.dumps(event) for event in events)
                    folder = root / (agent + '-' + state)
                    script = root / 'fake-cli'
                    code = '#!/usr/bin/env python3\nimport sys,pathlib\n'
                    if sidecar:
                        code += f"pathlib.Path(sys.argv[sys.argv.index('--usage-output-file')+1]).write_text({json.dumps(sidecar)!r})\n"
                    code += f'print({output!r})\nsys.exit({7 if state == "exit" else 0})\n'
                    script.write_text(code); script.chmod(0o755)
                    with patch('verinote.agents.executable', return_value=str(script)) as launch:
                        if state in ('normal', 'recovered'):
                            self.assertEqual(CLI().complete(agent, '', 'synthetic', folder), {'ok': True})
                        else:
                            with self.assertRaisesRegex(ValueError, 'CLI 종료 코드 7|Cursor 실행 실패'):
                                CLI().complete(agent, '', 'synthetic', folder)
                        self.assertEqual(launch.call_count, 1)
                    self.assertEqual((folder / 'stdout.txt').read_text(), output + '\n')
                    usage = json.loads((folder / 'usage.json').read_text())
                    meta = json.loads((folder / 'run.json').read_text())
                    _, expected, _ = decode_transport(agent, output, sidecar)
                    for key in ('tokens', 'reported_usd', 'reported_credits'):
                        self.assertEqual(usage[key], expected[key])
                    self.assertEqual(usage['status'], 'success' if state in ('normal', 'recovered') else 'error')
                    if state == 'recovered': self.assertEqual(len(meta['response_normalizations']), 1)
                    if state == 'normal': self.assertEqual(meta['response_normalizations'], [])


class Lifecycle(unittest.TestCase):
    setUp=workflow.Workflow.setUp
    tearDown=workflow.Workflow.tearDown

    def run_outputs(self, outputs, *, stage=None):
        pending=iter(outputs)
        class Replay(CLI):
            def complete(adapter,agent,model,prompt,folder,**kwargs):
                folder.mkdir(parents=True,exist_ok=True)
                script=folder/'fake-cli'
                output=next(pending)
                if kwargs.get('phase') == 'ontology':
                    from test_ontology import wire_dictionary
                    output=wire_dictionary(output, prompt)
                raw=json.dumps({'status':'SUCCESS','response':json.dumps(output),
                                'usage':{'input_tokens':50,'output_tokens':3,'cache_read_tokens':10}})
                script.write_text('#!/usr/bin/env python3\nprint('+repr(raw)+')\n');script.chmod(0o755)
                with patch('verinote.agents.executable',return_value=str(script)):
                    return super().complete(agent,model,prompt,folder,**kwargs)
        service=Service(self.store,self.root,Replay(rates=self.store.usage_rates))
        if stage:
            job=service.start(self.doc['id'],'agy','',stage,1,1)
        else:
            job=service.ask({'question':'synthetic','agent':'agy','model':''})
        service.pool.shutdown(wait=True)
        owner=self.store.run(job['id']) if stage else self.store.question(job['id'])
        return service.with_usage('runs' if stage else 'questions',owner)

    def test_failed_validation_retry_repair_and_multi_round_question(self):
        bad=copy.deepcopy(DICTIONARY);bad['entities'][0]['quote']='원문에 없는 내용'
        first=self.run_outputs([bad],stage='ontology')
        self.assertEqual(first['status'],'error')
        self.assertEqual(first['usage']['details'][0]['status'],'success')
        self.assertEqual(first['usage']['tokens']['input']['value'],60)
        retry=self.run_outputs([DICTIONARY],stage='ontology')
        self.assertEqual(retry['status'],'success')
        item=next(i for i in self.store.document(self.doc['id'])['items'] if i['kind']=='ontology')
        self.store.review(item['id'],1,'approved','test','synthetic only')
        output=copy.deepcopy(PAGE)|{'facts':copy.deepcopy(FACTS)};output['facts'][0]['quote']='wrong quote'
        extraction=self.run_outputs([output,{'repairs':[{'path':'facts.0.quote','quote':FACTS[0]['quote']}]}],stage='extract')
        self.assertEqual(extraction['status'],'success',extraction['detail'])
        self.assertEqual(extraction['usage']['tokens']['input']['value'],120)
        self.assertEqual({c['stage'] for c in extraction['usage']['details']},{'extract','quote_repair'})
        question=self.run_outputs([{'tools':[{'name':'query_graph','arguments':{}}]},
                                   {'tools':[{'name':'query_graph','arguments':{'subject':'missing'}}]},
                                   {'answer':{'claims':[],'unknowns':['승인된 사실 없음']}}])
        self.assertEqual(question['status'],'success',question.get('error'))
        self.assertEqual(question['usage']['calls'],3)
        self.assertEqual(question['usage']['tokens']['input']['value'],180)
        self.assertNotIn('usage',self.store.question(question['id']))


class Accounting(unittest.TestCase):
    setUp=workflow.Workflow.setUp
    tearDown=workflow.Workflow.tearDown

    def add_run(self):
        return legacy_run(self.store,self.doc['id'])

    def write_call(self,run,path='1',value=None):
        folder=self.root/'runs'/run['id']/path;folder.mkdir(parents=True,exist_ok=True)
        atomic_json(folder/'run.json',{'agent':'agy'})
        atomic_json(folder/'usage.json',value or record())
        return folder

    def test_all_stages_retries_questions_and_repeat_read(self):
        first=self.add_run();self.store.run_status(first['id'],'error')
        self.write_call(first);self.write_call(first,'2');self.write_call(first,'2/quote-repair')
        second=self.add_run();self.store.run_status(second['id'],'success');self.write_call(second)
        snapshot=self.store.snapshot();job=self.store.create_question('synthetic','agy','',snapshot['id'])
        for i in (1,2,3):
            folder=self.root/'questions'/job['id']/f'llm-{i}';folder.mkdir(parents=True)
            atomic_json(folder/'usage.json',record())
        service=Service(self.store,self.root)
        try:
            a=service.usage_report();self.assertEqual(a,service.usage_report())
            self.assertEqual(a['summary']['calls'],7);self.assertEqual(a['summary']['tokens']['input']['value'],700)
            self.assertEqual({g['stage'] for g in a['groups']},{'legacy','quote_repair','question'})
            self.assertEqual(service.with_usage('runs',self.store.run(first['id']))['usage']['calls'],3)
        finally:service.pool.shutdown()

    def test_missing_corrupt_large_and_symlink_logs_are_unknown(self):
        run=self.add_run();self.store.run_status(run['id'],'error');owner=self.store.run(run['id'])
        folder=self.write_call(run)
        for content in ['bad','{}','x'*(MAX_METADATA+1)]:
            (folder/'usage.json').write_text(content)
            calls=job_calls(self.root,'runs',owner);self.assertIsNone(summarize(calls)['tokens']['input']['value'])
        (folder/'usage.json').unlink()
        outside=self.root/'outside.json';atomic_json(outside,record(tokens=dict(input=999,output=0,cache_read=0,cache_write=0,reasoning=0)))
        (folder/'usage.json').symlink_to(outside)
        self.assertIsNone(summarize(job_calls(self.root,'runs',owner))['tokens']['input']['value'])
        (folder/'usage.json').unlink();(folder/'run.json').unlink();folder.rmdir();folder.symlink_to(outside.parent,target_is_directory=True)
        self.assertEqual(job_calls(self.root,'runs',owner)[0]['status'],'unknown')

    def test_deleted_owners_excluded_even_when_cleanup_fails(self):
        run=self.add_run();self.store.run_status(run['id'],'success');folder=self.write_call(run)
        service=Service(self.store,self.root)
        try:
            self.assertEqual(service.usage_report()['summary']['calls'],1)
            self.store.delete_document(self.doc['id'],False,1,1)
            self.assertTrue(folder.exists())
            self.assertEqual(service.usage_report()['summary']['calls'],0)
            service.cleanup_logs();self.assertFalse(folder.exists())
        finally:service.pool.shutdown()

    def test_report_limit_is_explicit_and_not_recent_list_limit(self):
        with self.store.connect(True) as db:
            for i in range(1002):
                db.execute('INSERT INTO runs(id,document_id,version,agent,model,status,detail,created_at) VALUES(?,?,?,?,?,?,?,?)',(f'{i:032x}',self.doc['id'],1,'agy','','error','','2026-10-15'))
        owners,total=self.store.usage_owners();self.assertEqual((len(owners),total),(1000,1002))
        service=Service(self.store,self.root)
        try:
            result=service.usage_report();self.assertTrue(result['truncated']);self.assertEqual(result['included_jobs'],1000)
        finally:service.pool.shutdown()

    def test_rates_persist_independently_of_agent_settings(self):
        self.store.save_usage_rates({'agy':RATE})
        self.store.save_settings({'selected':'copilot','models':{}})
        self.assertEqual(self.store.usage_rates(),{'agy':RATE})
        self.store.save_usage_rates({});self.assertEqual(self.store.settings()['selected'],'copilot')


class UsageHTTP(unittest.TestCase):
    setUp=workflow.HTTP.setUp
    tearDown=workflow.HTTP.tearDown
    request=workflow.HTTP.request

    def test_endpoints_rates_validation_enrichment_and_origin(self):
        self.assertEqual(self.request('/api/usage-rates'),{})
        self.assertEqual(self.request('/api/usage-rates','PUT',{'agy':RATE}),{'agy':RATE})
        for data,headers,status in [({'agy':RATE|{'input':True}}, {},400),({'agy':RATE},{'Origin':'https://evil.example'},403)]:
            with self.assertRaises(HTTPError) as error:self.request('/api/usage-rates','PUT',data,headers)
            self.assertEqual(error.exception.code,status);error.exception.close()
        doc=self.store.register('synthetic','s.md',SOURCE.encode(),SOURCE);run=legacy_run(self.store,doc['id']);self.store.run_status(run['id'],'error')
        folder=self.service.root/'runs'/run['id']/'1';folder.mkdir(parents=True);atomic_json(folder/'usage.json',record())
        atomic_json(folder/'run.json',{'prompt':'PRIVATE_SENTINEL'})
        result=self.request('/api/runs/'+run['id']);self.assertEqual(result['usage']['tokens']['input']['value'],100)
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(result))
        self.assertEqual(self.request('/api/usage')['summary']['calls'],1)


class UsageUI(unittest.TestCase):
    def test_reported_estimated_unknown_zero_units_and_escaping(self):
        script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('verinote/web/app.js','utf8');
const context=vm.createContext({esc:v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))});
vm.runInContext(source.slice(source.indexOf('function usageStage('),source.indexOf('function downloadJSON(')),context);
context.u={calls:2,unknown_calls:1,incomplete_calls:1,tokens:{input:{value:0,known_calls:1},output:{value:null,known_calls:0}},reported_usd:{value:null,known_calls:0},estimated_usd:{value:0.000351,known_calls:1},credits:[{unit:'<img src=x>',provenance:'estimated',value:0.0351,known_calls:1}],details:[{stage:'quote_repair',status:'error',requested_model:'<script>',warnings:['<bad>']}]};
const html=vm.runInContext('renderUsage(u)',context);assert(html.includes('0 토큰'));assert(html.includes('미확인'));assert(html.includes('설정 단가 추정'));assert(html.includes('인용 보정'));assert(html.includes('&lt;img'));assert(!html.includes('<script>'));
assert.equal(vm.runInContext('usageNumber(null)',context),'미확인');assert.equal(vm.runInContext('usageNumber(0)',context),'0');
assert.equal(vm.runInContext('usageNumber(1e-10)',context),'<0.00000001');
'''
        subprocess.run(['node','-e',script],check=True,capture_output=True,text=True)
