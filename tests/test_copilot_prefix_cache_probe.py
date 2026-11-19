import json
import tempfile
import unittest
from pathlib import Path

from verinote.diagnostics import copilot_prefix_cache_probe as probe


def fake(hit_for, **fields):
    """Fake CLI: input tokens grow with the dictionary; cache_read comes from hit_for(folder name, seen_before)."""
    seen = set()

    def complete(agent, model, text, folder):
        assert (agent, model) == ('copilot', probe.MODEL)
        folder = Path(folder); folder.mkdir(parents=True, exist_ok=True)
        dictionary = text.split('\n\n')[0]
        tokens = 15230 + (22000 if text.startswith('{"approved_dictionary"') else 0)
        read = hit_for(folder.name, dictionary in seen) if text.startswith('{"approved') else probe.BASELINE_HIT
        seen.add(dictionary)
        usage = {'reported_credits': 0.3, 'complete': True, 'resolved_model': probe.MODEL, 'tokens': {'input': tokens, 'cache_read': read}} | fields
        (folder / 'usage.json').write_text(json.dumps(usage), encoding='utf-8')
        (folder / 'provider-usage.json').write_text('{}', encoding='utf-8')
        return '{}'
    return complete


def drifted():
    inner = fake(lambda folder, seen: 0)

    def complete(agent, model, text, folder):
        out = inner(agent, model, text, folder)
        path = Path(folder) / 'usage.json'
        usage = json.loads(path.read_text())
        usage['tokens']['cache_read'] = 0
        path.write_text(json.dumps(usage))
        return out
    return complete


class PrefixCacheProbe(unittest.TestCase):
    def run_probe(self, hit_for, **fields):
        with tempfile.TemporaryDirectory() as root:
            return probe.run(root, complete=fake(hit_for, **fields)), sorted(p.name for p in Path(root).glob('call-*.json'))

    def test_dictionaries_differ_per_condition_and_hold_the_stated_size(self):
        a, b = (probe.synthetic_dictionary(c) for c in probe.CONDITIONS)
        self.assertNotEqual(a, b)
        self.assertGreaterEqual(len(a.encode()), probe.DICTIONARY_BYTES)

    def test_rejects_when_neither_condition_beats_the_baseline_hit_by_half_the_dictionary(self):
        result, calls = self.run_probe(lambda folder, seen: probe.BASELINE_HIT)
        self.assertEqual(result['verdict'], 'rejected')
        self.assertEqual(len(calls), 2 + 2 * (1 + probe.REPS))

    def test_a_same_folder_only_hit_puts_folder_pinning_in_scope(self):
        result, _ = self.run_probe(lambda folder, seen: probe.BASELINE_HIT + 20000 if folder == 'work-same_folder' and seen else probe.BASELINE_HIT)
        self.assertEqual(result['verdict'], 'pin_folder_in_scope')

    def test_a_hit_in_both_conditions_is_an_adoption_candidate_and_exactly_half_passes(self):
        result, _ = self.run_probe(lambda folder, seen: probe.BASELINE_HIT + 11000 if seen else probe.BASELINE_HIT)
        self.assertEqual(result['verdict'], 'adopt_candidate')

    def test_unknown_cost_or_a_different_resolved_model_stops_the_run(self):
        for fields, message in (({'reported_credits': None}, 'unknown_usage'), ({'resolved_model': 'auto-picked'}, 'model_mismatch')):
            with self.subTest(message), self.assertRaisesRegex(RuntimeError, message):
                self.run_probe(lambda folder, seen: 0, **fields)

    def test_a_warm_up_that_already_hits_the_dictionary_is_inconclusive_not_rejected(self):
        result, _ = self.run_probe(lambda folder, seen: 20000)
        self.assertEqual(result['verdict'], 'inconclusive')
        self.assertIn('warm-up cache_read', ' '.join(result['reasons']))

    def test_a_drifted_warmed_baseline_is_inconclusive(self):
        with tempfile.TemporaryDirectory() as root:
            result = probe.run(root, complete=fake(lambda folder, seen: probe.BASELINE_HIT + 20000 if seen else probe.BASELINE_HIT))
            self.assertEqual(result['verdict'], 'adopt_candidate')
        for hit in (0, 3000):
            self.assertEqual(probe.decide({c: {'dictionary_tokens': 22000, 'warm_cache_read': hit, 'cache_reads': [hit + 20000] * 5} for c in probe.CONDITIONS}, hit)['verdict'], 'inconclusive')

    def test_a_partial_warm_up_hit_or_drifted_baseline_stops_early_as_inconclusive(self):
        low = {c: {'dictionary_tokens': 22000, 'warm_cache_read': 3000, 'cache_reads': [24000] * 5} for c in probe.CONDITIONS}
        self.assertEqual(probe.decide(low)['verdict'], 'inconclusive')
        with tempfile.TemporaryDirectory() as root:
            result = probe.run(root, complete=drifted())
            self.assertEqual(result['verdict'], 'inconclusive')
            self.assertEqual(result['calls'], 2)

    def test_a_non_empty_output_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / 'old').write_text('x')
            with self.assertRaisesRegex(RuntimeError, 'output_directory_not_empty'):
                probe.run(root, complete=fake(lambda folder, seen: 0))

    def test_a_dictionary_outside_the_token_range_is_inconclusive(self):
        measured = {c: {'dictionary_tokens': 5000, 'warm_cache_read': 0, 'cache_reads': [probe.BASELINE_HIT] * 5} for c in probe.CONDITIONS}
        self.assertEqual(probe.decide(measured)['verdict'], 'inconclusive')

    def test_the_reserved_worst_case_blocks_a_call_that_could_exceed_the_ceiling(self):
        old = probe.MAX_CREDITS
        probe.MAX_CREDITS = 1.2
        try:
            with tempfile.TemporaryDirectory() as root, self.assertRaisesRegex(RuntimeError, 'budget_exhausted'):
                probe.run(root, complete=fake(lambda folder, seen: 0))
        finally:
            probe.MAX_CREDITS = old

    def test_stops_before_exceeding_the_call_budget(self):
        old = probe.MAX_CALLS
        probe.MAX_CALLS = 3
        try:
            with tempfile.TemporaryDirectory() as root, self.assertRaisesRegex(RuntimeError, 'budget_exhausted'):
                probe.run(root, complete=fake(lambda folder, seen: 0))
        finally:
            probe.MAX_CALLS = old


if __name__ == '__main__':
    unittest.main()
