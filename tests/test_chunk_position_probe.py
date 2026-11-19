import json
import re
import tempfile
import unittest
from pathlib import Path

from verinote.diagnostics import chunk_position_probe as probe

SENTENCES = [x.strip() for x in re.findall(r'[^.]+\.', probe.TARGET)]


def fake(recovered_for, quote=None, answer=None, **fields):
    """Fake copilot: finds the target offset in the prompt and answers with `recovered_for(position, rep)` triples."""
    state = {'n': 0}

    def complete(agent, model, prompt, folder):
        assert (agent, model) == ('copilot', probe.MODEL)
        folder = Path(folder); folder.mkdir(parents=True, exist_ok=True)
        position = folder.name.split('-', 2)[2]
        rep = state['n'] // 3
        state['n'] += 1
        count = recovered_for(position, rep)
        usage = {'reported_credits': 1.0, 'complete': True, 'resolved_model': probe.MODEL, 'tokens': {'input': 1, 'cache_read': 0}} | fields
        (folder / 'usage.json').write_text(json.dumps(usage), encoding='utf-8')
        (folder / 'provider-usage.json').write_text('{}', encoding='utf-8')
        if answer is not None:
            return answer
        facts = [{'subject': s, 'predicate': p, 'object': o, 'quote': quote if quote is not None else SENTENCES[i]}
                 for i, (s, p, o) in enumerate(probe.EXPECTED[:count])]
        coverage = [{'predicate': r['id'], 'fact_indices': [k for k, f in enumerate(facts) if f['predicate'] == r['id']],
                     'claim_indices': [], 'status': 'extracted' if any(f['predicate'] == r['id'] for f in facts) else 'no_evidence',
                     'reason': '측정'} for r in probe.dictionary_payload()['relations']]
        return {'title': '측정', 'claims': [{'heading': '측정', 'text': '측정 문단', 'quote': SENTENCES[0]}], 'facts': facts,
                'coverage': coverage, 'dictionary_gaps': []}
    return complete


class ChunkPositionProbe(unittest.TestCase):
    def run_probe(self, recovered_for, **fields):
        with tempfile.TemporaryDirectory() as root:
            return probe.run(root, complete=fake(recovered_for, **fields))

    def test_the_fake_quotes_are_sentences_of_the_passage(self):
        self.assertEqual(len(SENTENCES), 4)
        self.assertTrue(all(x in probe.TARGET for x in SENTENCES), SENTENCES)

    def test_chunks_are_identical_except_where_the_target_sits(self):
        texts = {p: probe.chunk_text(p) for p in probe.POSITIONS}
        self.assertEqual([texts[p][0].replace(probe.TARGET, '') .replace('\n', '') == texts['front'][0].replace(probe.TARGET, '').replace('\n', '') for p in probe.POSITIONS], [True] * 3)
        self.assertEqual(texts['front'][1], 0)
        self.assertEqual(texts['back'][1] + len(probe.TARGET), len(texts['back'][0]))
        self.assertTrue(3000 < texts['middle'][1] < 4500)
        self.assertEqual({len(t) for t, _ in texts.values()}.__len__(), 1)
        self.assertTrue(probe.CHUNK_CHARS - 200 < len(texts['front'][0]) <= probe.CHUNK_CHARS)
        for text, offset in texts.values():
            self.assertTrue(offset == 0 or text[offset - 1] == '\n')

    def test_the_prompt_is_the_production_one_within_the_size_range(self):
        with tempfile.TemporaryDirectory() as root:
            prompts = {p: build for p in probe.POSITIONS if (build := probe.build_prompt(p, Path(root) / p)[0])}
        for prompt in prompts.values():
            self.assertTrue(probe.PROMPT_BYTES_RANGE[0] <= len(prompt.encode()) <= probe.PROMPT_BYTES_RANGE[1], len(prompt.encode()))
            self.assertIn('입력(JSON):', prompt)
            self.assertEqual(prompt.count('청람서비스는 은하저장소에 의존한다.'), 1)

    def test_schedule_runs_each_position_three_times_and_rotates_the_order(self):
        order = probe.schedule()
        self.assertEqual(len(order), 9)
        self.assertEqual([order[i][1] for i in (0, 3, 6)], ['front', 'middle', 'back'])
        self.assertEqual({p: sum(1 for _, q in order if q == p) for p in probe.POSITIONS}, {p: 3 for p in probe.POSITIONS})

    def test_a_back_only_loss_beyond_the_spread_is_adopted(self):
        result = self.run_probe(lambda position, rep: 1 if position == 'back' else 4)
        self.assertEqual(result['verdict'], 'adopt_late_loss')
        self.assertEqual(result['calls'], 9)

    def test_variation_as_large_as_the_gap_is_not_a_position_effect(self):
        result = self.run_probe(lambda position, rep: (4, 3, 2)[rep] if position != 'back' else (3, 2, 1)[rep])
        self.assertEqual(result['verdict'], 'rejected')

    def test_no_difference_is_rejected_and_a_front_loss_is_inconclusive(self):
        self.assertEqual(self.run_probe(lambda position, rep: 4)['verdict'], 'rejected')
        result = self.run_probe(lambda position, rep: 0 if position == 'front' else 4)
        self.assertEqual(result['verdict'], 'inconclusive')

    def test_an_ungrounded_quote_or_a_malformed_answer_is_not_evidence(self):
        self.assertEqual(self.run_probe(lambda position, rep: 4, quote='원문에 없는 문장')['verdict'], 'inconclusive')
        self.assertEqual(self.run_probe(lambda position, rep: 4, quote='')['verdict'], 'inconclusive')
        self.assertEqual(self.run_probe(lambda position, rep: 4, answer={'facts': []})['verdict'], 'inconclusive')
        result = self.run_probe(lambda position, rep: 4, answer={'claims': []})
        self.assertEqual((result['verdict'], result['calls']), ('inconclusive', 1))

    def test_a_quote_from_another_triples_sentence_is_ungrounded(self):
        self.assertEqual(self.run_probe(lambda position, rep: 4, quote=SENTENCES[0])['verdict'], 'inconclusive')

    def test_a_quote_naming_only_the_predicate_is_ungrounded_and_stops_the_run(self):
        result = self.run_probe(lambda position, rep: 4, quote='호출한다.')
        self.assertEqual((result['verdict'], result['calls']), ('inconclusive', 1))

    def test_the_synthetic_dictionary_is_one_the_product_accepts(self):
        probe.check_dictionary()
        self.assertTrue(all(r['id'].replace('_', '').isalnum() for r in probe.dictionary_payload()['relations']))

    def test_a_target_that_is_never_extracted_is_inconclusive(self):
        result = self.run_probe(lambda position, rep: 0)
        self.assertEqual(result['verdict'], 'inconclusive')
        self.assertEqual(result['calls'], 9)

    def test_an_out_of_range_prompt_is_inconclusive(self):
        verdict = probe.decide({p: [4, 4, 4] for p in probe.POSITIONS}, [10_000])
        self.assertEqual(verdict['verdict'], 'inconclusive')

    def test_unknown_usage_or_a_different_model_stops_the_run(self):
        for fields, message in (({'reported_credits': None}, 'unknown_usage'), ({'resolved_model': 'auto'}, 'model_mismatch'), ({'complete': False}, 'unknown_usage')):
            with self.subTest(message), self.assertRaisesRegex(RuntimeError, message):
                self.run_probe(lambda position, rep: 4, **fields)

    def test_a_failed_call_keeps_its_cost_and_stops(self):
        def broken(agent, model, prompt, folder):
            raise ValueError('CLI failed')
        with tempfile.TemporaryDirectory() as root, self.assertRaisesRegex(RuntimeError, 'call_failed'):
            probe.run(root, complete=broken)

    def test_the_reserve_blocks_a_call_that_could_exceed_the_ceiling(self):
        old = probe.MAX_CREDITS
        probe.MAX_CREDITS = 2.0
        try:
            with tempfile.TemporaryDirectory() as root, self.assertRaisesRegex(RuntimeError, 'budget_exhausted'):
                probe.run(root, complete=fake(lambda position, rep: 4))
        finally:
            probe.MAX_CREDITS = old

    def test_a_non_empty_output_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / 'old').write_text('x')
            with self.assertRaisesRegex(RuntimeError, 'output_directory_not_empty'):
                probe.run(root, complete=fake(lambda position, rep: 4))


if __name__ == '__main__':
    unittest.main()
