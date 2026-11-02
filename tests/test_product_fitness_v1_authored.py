"""Product-fitness v1 (#788) on one entity-substituted, line-reordered variant of short.

The variant's labels mirror the short registry row. For this variant, verdict parity
on every recipe shows that v1 verdicts did not change under its line positions, entity
names and line order. It shows nothing about other variants, and it is not an
agreement between independently judged labels.
"""
import ast
import copy
import json
import re
import unittest
from pathlib import Path

from verinote.diagnostics import product_fitness_v1 as pf
from verinote.documents import chunks
from verinote.extraction_evaluation import DATA_ROOT, ideal_outputs, load_case

PROSE_FIELDS = ('heading', 'text', 'reason', 'detail')
TEST_FILES = (Path(__file__), Path(__file__).with_name('test_product_fitness_v1.py'))


def _strings(value):
    if isinstance(value, str): yield value
    elif isinstance(value, dict):
        for item in value.values(): yield from _strings(item)
    elif isinstance(value, list):
        for item in value: yield from _strings(item)


def allowlist():
    """Whitespace tokens of existing generic synthetic vocabulary in dataset v2."""
    tokens = set()
    for case_id in ('short', 'three', 'four', 'eight'):
        case = load_case(case_id, DATA_ROOT/'v2')
        tokens.update(case['source'].split())
        tokens.update(json.dumps(case['dictionary'], ensure_ascii=False).split())
        for text in _strings(ideal_outputs(case, contract_version=2)): tokens.update(text.split())
    return tokens


def authored_texts(doc):
    yield doc['case']['source']
    yield json.dumps(doc['case']['dictionary'], ensure_ascii=False)
    for obligation in doc['chunk']['obligations']:
        # Markers are sub-token fragments; load_authored validates each as a
        # substring of its canonical text, which is scanned here.
        yield obligation['canonical']['text']
        for substitute in obligation['substitutes']: yield substitute['text']
    for fact in doc['facts']: yield from (fact[key] for key in ('subject', 'predicate', 'object', 'quote'))
    for judgment in doc['semantic_judgments']: yield from judgment['claim'].values()


def recipe_prose(outputs):
    for output in outputs.values():
        for row in output['claims']+output['coverage']+output['dictionary_gaps']:
            yield from (row[key] for key in PROSE_FIELDS if key in row)


def hangul_literals(path):
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and any(0xAC00 <= ord(c) <= 0xD7A3 for c in node.value):
            yield node.value


def unknown_tokens(texts, allowed):
    return sorted({token for text in texts for token in text.split() if token not in allowed})


class AuthoredProductFitness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = pf.load_spec(); cls.registry = pf.load_registry(); cls.doc = pf.load_authored()
        cls.short = load_case('short', DATA_ROOT/'v2'); cls.short_row = cls.registry['cases']['short']['chunks'][0]
        cls.short_outputs = pf.fixtures(cls.short, cls.short_row, ideal_outputs(cls.short, contract_version=2)[0]['output'])
        cls.authored_outputs = pf.fixtures(cls.doc['case'], cls.doc['chunk'], pf.baseline_output(cls.doc['chunk'], cls.doc['facts']))

    def test_16_scoped_vocabulary_uses_only_existing_synthetic_tokens(self):
        allowed = allowlist()
        self.assertEqual(unknown_tokens(authored_texts(self.doc), allowed), [])
        self.assertEqual(unknown_tokens(recipe_prose(self.short_outputs), allowed), [])
        self.assertEqual(unknown_tokens(recipe_prose(self.authored_outputs), allowed), [])
        literals = [text for path in TEST_FILES for text in hangul_literals(path)]
        self.assertTrue(literals)
        self.assertEqual(unknown_tokens(literals, allowed), [])
        # Quotes are not token-scanned: a truncated quote splits source tokens.
        # Each must instead be a literal substring of its chunk.
        for case, outputs in ((self.short, self.short_outputs), (self.doc['case'], self.authored_outputs)):
            text = list(chunks(case['source']))[0][1]
            for fixture_id, output in outputs.items():
                for row in output['claims']+output['facts']:
                    self.assertIn(row['quote'], text, fixture_id)
        injected = copy.deepcopy(self.doc)
        injected['case']['source'] = injected['case']['source'].replace('S4.', 'S4. Atlas', 1)
        self.assertEqual(unknown_tokens(authored_texts(injected), allowed), ['Atlas'])

    def test_authored_case_is_a_template_variant_of_short(self):
        template = self.doc['template_of']
        self.assertEqual((template['case_id'], template['annotation_index']), ('short', 1))
        self.assertEqual(self.doc['chunk']['derived_from']['template_case_id'], 'short')
        short_lines = self.short['source'].split('\n')
        def substitute(text, identifiers):
            return re.sub(r'S\d|Outside\d', lambda m: identifiers.get(m.group(), m.group()), text)
        rebuilt = [substitute(short_lines[row['template_line']], row['identifiers']) for row in template['lines']]
        self.assertEqual('\n'.join(rebuilt), self.doc['case']['source'])
        self.assertEqual(sorted(row['template_line'] for row in template['lines']), list(range(len(short_lines))))
        self.assertNotEqual([row['template_line'] for row in template['lines']], list(range(len(short_lines))))
        self.assertEqual(self.doc['chunk']['coverage'], self.short_row['coverage'])
        self.assertEqual(self.doc['chunk']['gap_pairs'], self.short_row['gap_pairs'])
        mapping = {short_lines[row['template_line']]: row['identifiers'] for row in template['lines']}
        for authored, short in zip(self.doc['chunk']['obligations'], self.short_row['obligations'], strict=True):
            identifiers = mapping[next(line for line in short_lines if short['canonical']['text'] in line)]
            self.assertEqual(authored['id'], short['id'])
            self.assertEqual(authored['canonical']['text'], substitute(short['canonical']['text'], identifiers))
            self.assertEqual(authored['required_markers']['markers'],
                             [substitute(marker, identifiers) for marker in short['required_markers']['markers']])
            self.assertEqual([s['text'] for s in authored['substitutes']],
                             [substitute(s['text'], identifiers) for s in short['substitutes']])

    def test_template_identity_is_checked_on_load(self):
        pf.validate_authored(copy.deepcopy(self.doc))
        changed = copy.deepcopy(self.doc); changed['chunk']['derived_from']['case_sha256'] = '0'*64
        with self.assertRaisesRegex(ValueError, 'product_fitness_authored_template$'):
            pf.validate_authored(changed)
        changed = copy.deepcopy(self.doc)
        changed['template_of']['case_sha256'] = changed['chunk']['derived_from']['case_sha256'] = '0'*64
        with self.assertRaisesRegex(ValueError, 'product_fitness_authored_template_identity$'):
            pf.validate_authored(changed)

    def test_17_this_variant_gets_short_verdicts_for_every_recipe(self):
        facts = [dict(zip(('subject', 'predicate', 'object'), g['variants'][0], strict=True), quote=g['sample_quote']) for g in self.short['gold']]
        self.assertEqual(pf.baseline_output(self.short_row, facts), ideal_outputs(self.short, contract_version=2)[0]['output'])
        self.assertNotEqual(self.doc['case']['source'], self.short['source'])
        authored_order = sorted(self.doc['chunk']['obligations'], key=lambda o: o['canonical']['start'])
        short_order = sorted(self.short_row['obligations'], key=lambda o: o['canonical']['start'])
        self.assertNotEqual([o['id'] for o in authored_order], [o['id'] for o in short_order])
        self.assertEqual(set(self.authored_outputs), set(self.short_outputs))
        def vector(case, row, judgments, output):
            result = pf.assess(case, [{'index': 1, 'output': output}], {'chunks': [row]}, self.spec, judgments)
            return result['production_passed'], result['overall'], pf.failure_vector(result)
        for fixture_id in self.short_outputs:
            with self.subTest(fixture=fixture_id):
                self.assertEqual(vector(self.doc['case'], self.doc['chunk'], self.doc['semantic_judgments'], self.authored_outputs[fixture_id]),
                                 vector(self.short, self.short_row, self.spec['semantic_judgments'], self.short_outputs[fixture_id]))
        for record in [self.doc['authorship']]+self.doc['semantic_judgments']:
            self.assertEqual((record['author'], record['human_review']), ('agent_proposed', 'pending'))
        for obligation in self.doc['chunk']['obligations']:
            for record in [obligation['required_markers']]+[s['review'] for s in obligation['substitutes']]:
                self.assertEqual((record['author'], record['human_review']), ('agent_proposed', 'pending'))
                self.assertTrue(record['rationale'].strip())
        report = pf.verify_proposal()
        self.assertEqual(set(report['fixtures']), {'short', 'authored-pf1'})
        self.assertFalse(any(row['subject'].startswith('fixture:authored-pf1') for row in report['differences_from_v2']))


if __name__ == '__main__': unittest.main()
