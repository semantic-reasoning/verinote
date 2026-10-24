"""Budget and isolation guarantees without model/network access."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from verinote.agents import CLI
from verinote.extraction_benchmark import (
    Budget,
    BudgetCaller,
    FakeCLI,
    Limits,
    ReportWriter,
    Stopped,
    call_metrics,
    distribution,
    measure,
)
from verinote.extraction_evaluation import load_case
from verinote.usage import atomic_json


class Clock:
    def __init__(self): self.value = 0
    def __call__(self): return self.value


class Benchmark(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name).resolve()
    def tearDown(self): self.temp.cleanup()
    def run_measure(self, name='out', **kwargs):
        return measure(self.root/name, limits=kwargs.pop('limits', Limits(repetitions=1)), **kwargs)

    def test_normal_fresh_repeats_all_cases_quality_and_no_user_store(self):
        sentinel = self.root/'user.sqlite3'; sentinel.write_bytes(b'PRIVATE_USER_DATA')
        roots = []
        class Observed(FakeCLI):
            def complete(fake, agent, model, prompt, folder, **kwargs):
                roots.append(Path(folder).parents[2])
                return super().complete(agent, model, prompt, folder, **kwargs)
        result = self.run_measure(cases=['short','long','dense','none','boundary'], limits=Limits(repetitions=2),
                                  caller_factory=lambda c: Observed(c, 'normal'))
        self.assertEqual(result['status'], 'completed'); self.assertTrue(result['synthetic_responses'])
        self.assertEqual(len(result['repetitions']), 10); self.assertEqual(len(set(roots)), 10)
        self.assertTrue(all(not r.exists() for r in roots)); self.assertEqual(sentinel.read_bytes(), b'PRIVATE_USER_DATA')
        self.assertTrue(all(r['quality']['complete'] and r['quality']['evaluation']['passed'] for r in result['repetitions']))
        self.assertTrue(all(r['document_timing']['available'] for r in result['repetitions']))
        self.assertTrue(all(r['document_timing']['summary']['new_call_attempts'] == r['attempt_count'] for r in result['repetitions']))
        self.assertEqual(result['summary']['reported_usd']['value'], None)
        self.assertIsNone(result['conditions']['cli_version'])
        self.assertEqual(result, json.loads((self.root/'out'/'report.json').read_text()))

    def test_repair_and_failure_attempts_include_original_and_clone(self):
        for scenario, calls, status in [('normal',1,'success'),('repair',2,'success'),('failure',1,'error'),('timeout',1,'error')]:
            result = self.run_measure(scenario, cases=['short'], scenario=scenario)
            self.assertEqual(result['budget']['requests_started'], calls)
            self.assertEqual(result['repetitions'][0]['status'], status)
            self.assertEqual(result['summary']['repair_attempts'], scenario == 'repair')
            if status == 'error': self.assertFalse(result['repetitions'][0]['quality']['complete'])
        budget = Budget(Limits(requests=1)); fake = FakeCLI(load_case('short'),'normal')
        wrapped = BudgetCaller(fake,budget); limited = wrapped.limited_timeout('agy',1)
        self.assertIs(limited.budget, wrapped.budget)
        limited.complete('agy','', 'p',self.root/'1')
        with self.assertRaises(Stopped): wrapped.complete('agy','', 'p',self.root/'2')
        self.assertEqual(fake.state['calls'], 1)

    def test_request_cap_blocks_repair_and_partial_long_keeps_full_gold_denominator(self):
        repaired = self.run_measure('repair',cases=['short'], scenario='repair', limits=Limits(requests=1,repetitions=2))
        self.assertEqual(repaired['stop_reason'], 'request_limit'); self.assertEqual(len(repaired['attempts']),1)
        self.assertEqual(repaired['summary']['repair_attempts'],0)
        partial = self.run_measure('partial',cases=['long'], limits=Limits(requests=1,repetitions=2))
        quality = partial['repetitions'][0]['quality']
        self.assertFalse(quality['complete']); self.assertEqual(quality['validated_chunks'],1)
        self.assertEqual(quality['evaluation']['recall']['value'], .5)
        self.assertEqual(quality['evaluation']['gold_count'],2)
        self.assertEqual(partial['budget']['requests_started'],1)

    def test_token_overshoot_last_call_not_dropped_and_no_followup(self):
        result = self.run_measure(cases=['short'],scenario='repair',limits=Limits(tokens=1,repetitions=4))
        self.assertEqual(result['stop_reason'],'token_limit'); self.assertEqual(len(result['attempts']),1)
        self.assertEqual(result['budget']['observed_tokens_lower_bound'],120)
        self.assertFalse(result['budget']['token_hard_cap_guaranteed'])
        self.assertEqual(result['attempts'][0]['tokens']['input'],100)

    def test_unknown_and_partial_usage_stop_or_explicit_unverified_continue(self):
        class Missing(FakeCLI):
            def complete(fake,*args,**kwargs):
                result=super().complete(*args,**kwargs)
                path=Path(args[3])/'usage.json'; value=json.loads(path.read_text())
                value['tokens']['output']=None; value['complete']=False; atomic_json(path,value)
                return result
        for policy, count in [('stop',1),('continue',2)]:
            result=self.run_measure(policy,cases=['short'],limits=Limits(repetitions=2,missing_usage=policy),caller_factory=lambda c:Missing(c,'normal'))
            self.assertEqual(len(result['attempts']),count)
            self.assertEqual(result['budget']['observed_tokens_lower_bound'],count*100)
            self.assertFalse(result['budget']['token_usage_verified'])
            self.assertEqual(result['budget']['unknown_usage_calls'],count)

    def test_deadline_shared_and_rechecked_after_last_call_and_setup(self):
        clock=Clock()
        class Late(FakeCLI):
            def complete(fake,*args,**kwargs):
                result=super().complete(*args,**kwargs); clock.value=5; return result
        result=self.run_measure(cases=['long'],limits=Limits(seconds=1,repetitions=2),clock=clock,caller_factory=lambda c:Late(c,'normal'))
        self.assertEqual(result['stop_reason'],'time_limit'); self.assertEqual(len(result['attempts']),1)
        budget=Budget(Limits(seconds=1),clock=clock); clone=BudgetCaller(FakeCLI(load_case('short'),'normal'),budget).limited_timeout('agy',2)
        clock.value+=2
        with self.assertRaises(Stopped): clone.complete('agy','','p',self.root/'1')
        self.assertEqual(budget.attempts,[])

    def test_usage_identity_missing_stale_and_no_nested_secret_export(self):
        class Unsafe(FakeCLI):
            def complete(fake,*args,**kwargs):
                result=super().complete(*args,**kwargs); folder=Path(args[3]); path=folder/'usage.json'; value=json.loads(path.read_text())
                value.update(warnings=['PRIVATE_NESTED'],resolved_model='PRIVATE_NESTED',source='PRIVATE_NESTED',credit_unit='PRIVATE_NESTED',reported_credits=1)
                value['tokens']['unexpected']='PRIVATE_NESTED'; atomic_json(path,value)
                result['facts'].append({'subject':'PRIVATE_NESTED','predicate':'calls','object':'Billing','quote':'Atlas directly calls Billing.'})
                return result
        result=self.run_measure(cases=['short'],caller_factory=lambda c:Unsafe(c,'normal'))
        self.assertNotIn('PRIVATE_NESTED', json.dumps(result))
        folder=self.root/'raw'; folder.mkdir(); fake=FakeCLI(load_case('short'),'normal'); fake.complete('agy','m','p',folder/'1')
        value=json.loads((folder/'1'/'run.json').read_text()); value['cli_call_id']='a'*32; atomic_json(folder/'1'/'run.json',value)
        self.assertFalse(call_metrics(folder/'1','agy','m')['usage_complete'])

    def test_report_write_failure_cancels_before_repair_and_preserves_previous(self):
        original=ReportWriter.write
        def fail(writer,report):
            if report['attempts']: raise OSError('PRIVATE_WRITE')
            original(writer,report)
        fake=FakeCLI(load_case('short'),'repair')
        with patch.object(ReportWriter,'write',fail):
            with self.assertRaises(Stopped): self.run_measure(cases=['short'],caller_factory=lambda _:fake)
        self.assertEqual(fake.state['calls'],1)
        saved=json.loads((self.root/'out'/'report.json').read_text())
        self.assertEqual(saved['status'],'running'); self.assertNotIn('PRIVATE_WRITE',json.dumps(saved))

    def test_interrupt_closes_admission_before_worker_join_and_cleanup(self):
        started=threading.Event(); release=threading.Event(); roots=[]
        class Slow(FakeCLI):
            def complete(fake,*args,**kwargs):
                roots.append(Path(args[3]).parents[2]); started.set(); release.wait(2)
                return super().complete(*args,**kwargs)
        fake=Slow(load_case('long'),'normal')
        original=time.sleep
        def interrupted(seconds):
            if started.wait(2):
                release.set(); raise KeyboardInterrupt
            original(seconds)
        with patch('verinote.extraction_benchmark.time.sleep',interrupted):
            result=self.run_measure(cases=['long'],caller_factory=lambda _:fake)
        self.assertEqual(result['stop_reason'],'interrupted'); self.assertEqual(fake.state['calls'],1)
        self.assertTrue(all(not p.exists() for p in roots)); self.assertEqual(result['attempts'][0]['status'],'success')
        self.assertEqual(result['status'],'stopped')
        self.assertEqual(result['repetitions'][0]['quality']['validated_chunks'],1)
        self.assertFalse(result['repetitions'][0]['quality']['complete'])
        self.assertEqual(result['repetitions'][0]['quality']['evaluation']['recall']['value'],.5)
        self.assertTrue(result['repetitions'][0]['document_timing']['available'])

    def test_stale_matching_usage_is_not_counted_twice(self):
        folder=self.root/'1'; fake=FakeCLI(load_case('short'),'normal'); fake.complete('agy','m','p',folder)
        class Stale(CLI):
            def complete(self,*args,**kwargs): raise ValueError('PRIVATE_FAILED_BEFORE_WRITE')
        budget=Budget(Limits())
        with self.assertRaises(ValueError): BudgetCaller(Stale(),budget).complete('agy','m','p',folder)
        self.assertEqual(budget.observed_tokens,0); self.assertEqual(budget.reason,'usage_unknown')
        self.assertIsNone(budget.attempts[0]['cli_call_id'])

    def test_nonzero_and_parse_failure_are_budgeted_and_cli_linked(self):
        for index,body in enumerate(["import sys; sys.exit(7)","print('INVALID_JSON_PRIVATE')"]):
            script=self.root/f'failure{index}.py'; script.write_text(body)
            with patch('verinote.agents.command',return_value=([sys.executable,str(script)],'')), patch('verinote.agents.executable',return_value=sys.executable):
                result=self.run_measure(str(index),cases=['short'],mode='cli',model='synthetic',limits=Limits(repetitions=3),caller_factory=lambda _:CLI())
            self.assertEqual(len(result['attempts']),1)
            self.assertEqual(result['attempts'][0]['status'],'error')
            self.assertIsNotNone(result['attempts'][0]['cli_timing'])
            self.assertEqual(result['stop_reason'],'usage_unknown')
            self.assertNotIn('INVALID_JSON_PRIVATE',json.dumps(result))

    def test_token_metadata_inconsistency_and_missing_log_keep_unknown_state(self):
        fake=FakeCLI(load_case('short'),'normal'); folder=self.root/'1'; fake.complete('agy','m','p',folder)
        path=folder/'usage.json'; usage=json.loads(path.read_text()); usage['tokens']['reasoning']=999; atomic_json(path,usage)
        self.assertFalse(call_metrics(folder,'agy','m')['usage_complete'])
        path.unlink(); metrics=call_metrics(folder,'agy','m')
        self.assertFalse(metrics['usage_complete']); self.assertIsNone(metrics['tokens']['input'])

    def test_examples_are_labeled_partial_and_cost_unknown(self):
        for path in Path('benchmarks/extraction/examples').glob('benchmark-fake-*.json'):
            report=json.loads(path.read_text())
            self.assertEqual(report['mode'],'fake'); self.assertTrue(report['synthetic_responses'])
            self.assertIsNone(report['summary']['reported_usd']['value'])
            self.assertTrue(all(r['quality']['passed'] == (r['quality']['complete'] and r['quality']['evaluation']['passed']) for r in report['repetitions']))
        with patch('verinote.extraction_benchmark.MAX_REPORT_BYTES',10):
            with self.assertRaises(Stopped): self.run_measure('too-big')
        self.assertFalse((self.root/'too-big'/'report.json').exists())

    def test_output_paths_and_limits_and_stats(self):
        existing=self.root/'existing'; existing.mkdir()
        with self.assertRaises(ValueError): self.run_measure('existing')
        link=self.root/'link'; link.symlink_to(existing,target_is_directory=True)
        with self.assertRaises(ValueError): self.run_measure('link')
        with self.assertRaises(ValueError): self.run_measure('link/child')
        writer=ReportWriter(self.root/'safe'); target=self.root/'untouched'; target.write_text('sentinel')
        writer.path.symlink_to(target)
        with self.assertRaises(ValueError): writer.write({})
        self.assertEqual(target.read_text(),'sentinel')
        for args in ({'requests':0},{'seconds':float('nan')},{'tokens':True},{'repetitions':101},{'missing_usage':'guess'}):
            with self.assertRaises(ValueError): Limits(**args)
        self.assertIsNone(distribution([])['mean']); self.assertEqual(distribution([2])['population_stddev'],0)
        self.assertEqual(distribution([1,3])['mean'],2)

    @unittest.skipIf(os.name=='nt','POSIX synthetic subprocess tree')
    def test_real_cli_synthetic_subprocess_timeout_cleanup_and_timing(self):
        script=self.root/'child.py'; pid=self.root/'pid'
        script.write_text("import subprocess,sys,time\np=subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)'])\nopen(sys.argv[1],'w').write(str(p.pid))\nprint('PRIVATE_STDOUT',flush=True)\ntime.sleep(20)\n")
        with patch('verinote.agents.command',return_value=([sys.executable,str(script),str(pid)],'')), patch('verinote.agents.executable',return_value=sys.executable):
            start=time.monotonic()
            result=self.run_measure(cases=['long'],mode='cli',model='synthetic-model',timeout=.25,
                                    limits=Limits(repetitions=2,seconds=1),caller_factory=lambda _:CLI())
        self.assertLess(time.monotonic()-start,4); self.assertEqual(len(result['attempts']),1)
        self.assertEqual(result['repetitions'][0]['status'],'error')
        self.assertIsNotNone(result['attempts'][0]['cli_timing'])
        self.assertEqual(result['attempts'][0]['cli_timing']['status'],'timeout')
        # ps excludes dead/zombie descendants, which cannot make another request.
        child=int(pid.read_text()); state=subprocess.run(['ps','-o','stat=','-p',str(child)],capture_output=True,text=True).stdout.strip()
        self.assertTrue(not state or state.startswith('Z'),state)
        self.assertNotIn('PRIVATE_STDOUT',json.dumps(result))


if __name__=='__main__': unittest.main()
