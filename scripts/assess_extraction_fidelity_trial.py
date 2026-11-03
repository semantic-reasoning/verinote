"""Record the #785 evidence_links_v1 trial: its verdict re-derived offline, its outputs under the retained v1 criteria."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

from verinote.agents import parse_response
from verinote.diagnostics import product_fitness_v1 as pf
from verinote.diagnostics.extraction_fidelity import DEFAULT_ROOT as ROOT_768
from verinote.diagnostics.extraction_fidelity import REPORT_SHA256 as REPORT_768_SHA256
from verinote.diagnostics.extraction_fidelity import assess_outputs
from verinote.diagnostics.extraction_fidelity_artifacts import (
    MODEL,
    decision,
    verify_raw,
)
from verinote.extraction_ab_runner import _hash, file_index
from verinote.extraction_benchmark import MAX_REPORT_BYTES
from verinote.extraction_evaluation import _read, load_case
from verinote.extraction_threshold_ab_runner import verify_budget
from verinote.extraction_threshold_artifacts import verify_document
from verinote.extraction_transport import ExtractionSpec
from verinote.store import canonical
from verinote.usage import decode_transport

REPO = Path(__file__).resolve().parents[1]
RESULTS = REPO/'benchmarks/extraction/results'
TRIAL_ROOT = RESULTS/'785-evidence-links-v1-trial'
REPORT_PATH = RESULTS/'785-evidence-links-v1-assessment/report.json'
CRITERIA_ROOT = RESULTS/'785-evidence-links-v1-assessment/criteria'
INDEX_SHA256 = '302c4973250f8751590fe8958e63ffcda9a417e37b40f13fb773089ca50ff36e'
EVALUATION_SHA256 = '67d91f1b1b182524ff1788dd476b4253ad26e6c59f068788a603a49eac927feb'
CRITERIA_SHA256 = {'registry.json': '0dd3acbd1e4acb057d3c7b0421990ee89f6ad76a94ff5c3bfcf96f081467c0a2',
                   'spec.json': '72cb043cb36d7df703e50cdb8cc514ab8f00bb04f6afa1515ba5f083a0863e2d'}
COMBINATIONS = ((('P-STATUS-HISTORICAL', 'historical_no_evidence_linked', None), ('P-GAP-QUALIFIER-DUP', 'exempt_linked_from_coverage', 'A')),
                (('P-STATUS-HISTORICAL', 'historical_no_evidence_linked', None), ('P-GAP-QUALIFIER-DUP', 'exempt_linked_from_coverage', 'B')))


class TrialRecordError(ValueError): pass


def authenticate(root):
    files = file_index(root)
    if files.get('index.json', {}).get('sha256') != INDEX_SHA256: raise TrialRecordError('trial_index_identity')
    if files.get('evaluation.json', {}).get('sha256') != EVALUATION_SHA256: raise TrialRecordError('trial_evaluation_identity')
    if _read(root/'index.json', MAX_REPORT_BYTES) != {k: v for k, v in files.items() if k not in ('index.json', 'evaluation.json')}:
        raise TrialRecordError('trial_raw_index')
    return _read(root/'report.json', MAX_REPORT_BYTES), _read(root/'evaluation.json', MAX_REPORT_BYTES)


def rederive_verdict(root, report, evaluation):
    """evaluate's verdict chain without verify_trial and the verify_manifest it calls, which compares the trial with the current
    checkout, live dataset v2 and the CLI binary. The chain leaves out checks written inline in evaluate and the omitted
    functions: finalization and terminal accounting, for example, are not re-checked, while authenticate repeats evaluate's
    raw index comparison."""
    row = report['documents'][0]; artifact = root/row['artifact']; case = load_case('three', root/'fixtures')
    spec = ExtractionSpec.from_dict(report['environment']['transport'])
    safety = verify_document(artifact, row, report['attempts'], case, MODEL, spec)
    production = verify_raw(artifact, row, report['attempts'], case, report['environment'])
    verify_budget(report)
    if decision(report, safety, production) != evaluation: raise TrialRecordError('trial_verdict_linkage')
    return case, row


def load_criteria(root):
    for name, expected in CRITERIA_SHA256.items():
        if _hash(Path(root)/name) != expected: raise TrialRecordError('trial_criteria_identity')
    return pf.load_spec(root), pf.load_registry(root)


def pinned_reader(artifact, row):
    """Returns read(relative): the UTF-8 text of artifact/relative, or TrialRecordError('trial_768_file_identity') raised when its
    sha256 is not the one row['artifact_files'] lists for relative."""
    def read(relative):
        if _hash(artifact/relative) != row['artifact_files'].get(relative, {}).get('sha256'):
            raise TrialRecordError('trial_768_file_identity')
        return (artifact/relative).read_text(encoding='utf-8')
    return read


def replay(case, row, attempts, read, label):
    """v2 diagnosis of the raw stdout of each initial call, linked to the checkpoint and the recorded quality."""
    folder = 'runs/'+row['run_id']
    outputs = [{'index': a['chunk'], 'output': parse_response('claude', decode_transport('claude', read(f"{folder}/{a['chunk']}/stdout.txt"))[0])[0]}
               for a in attempts if not a['repair']]
    v2 = assess_outputs(case, outputs)
    checkpoint = json.loads(read(folder+'/extraction-checkpoint.json'))
    if [canonical(c['normalized_output']) for c in v2['chunks']] != [canonical(o) for o in checkpoint['outputs']]:
        raise TrialRecordError(label+'_checkpoint_linkage')
    if v2['finite_gold'] != row['quality']['evaluation']: raise TrialRecordError(label+'_v2_linkage')
    return outputs, v2 | {'source_sha256': case['source_sha256']}


def failure_summary(result):
    return {'layers': sorted({f['layer'] for f in result['failures']}), 'vector': [list(p) for p in pf.failure_vector(result)]}


def _differences(a, b, path=()):
    if isinstance(a, dict) and isinstance(b, dict):
        return [found for key in sorted(set(a) | set(b)) for found in
                (['.'.join(path+(key,))] if key not in a or key not in b else _differences(a[key], b[key], path+(key,)))]
    return [] if a == b else ['.'.join(path)]


def compare_768(env, env_768, prefix, prompts, mapping, mapping_768):
    """prompts: (chunk, trial original prompt, trial effective prompt, 768 prompt) per chunk."""
    errors = []
    for row in mapping['errors']:
        same = [r for r in mapping_768['errors'] if r['frozen_error'] == row['frozen_error']]
        matched = sorted({c for r in same for c in r['causes']})
        errors.append({'id': row['id'], 'equal_768_errors': [r['id'] for r in same], 'causes': row['causes'], 'causes_768': matched,
                       'causes_only_trial': sorted(set(row['causes'])-set(matched)),
                       'causes_only_768': sorted(set(matched)-set(row['causes']))})
    chunks = [{'index': index, 'effective_prompt_equal': effective == prompt_768,
               'original_payload_differences': _differences(json.loads(original[len(prefix):]), json.loads(prompt_768[len(prefix):]))
               if original.startswith(prefix) and prompt_768.startswith(prefix) else None}
              for index, original, effective, prompt_768 in prompts]
    causes, causes_768 = {c['id'] for c in mapping['causes']}, {c['id'] for c in mapping_768['causes']}
    return {'annotation_errors': {'trial': len(mapping['errors']), '768': len(mapping_768['errors'])},
            'invocation_script': {'trial': env['invocation'][0], '768': env_768['invocation'][0]},
            'cli_version_equal': env['cli_version'] == env_768['cli_version'],
            'transport_equal': env['transport'] == env_768['transport'],
            'binary_sha256_recorded': {'trial': 'binary_sha256' in env, '768': 'binary_sha256' in env_768},
            'base_prompt_sha256_equal': {'extraction': env['prompt_hashes']['extraction'] == env_768['prompt_sha256'],
                                         'repair': env['prompt_hashes']['repair'] == env_768['repair_prompt_sha256']},
            'chunks': chunks, 'errors': errors,
            'causes_only_trial': sorted(causes-causes_768), 'causes_only_768': sorted(causes_768-causes),
            'trial_causes_within_768': causes <= causes_768}


def build(root=TRIAL_ROOT, criteria=CRITERIA_ROOT, root_768=ROOT_768):
    root, root_768 = Path(root), Path(root_768)
    report, evaluation = authenticate(root)
    case, row = rederive_verdict(root, report, evaluation)
    artifact = root/row['artifact']; folder = artifact/'runs'/row['run_id']
    outputs, diagnosis = replay(case, row, report['attempts'], lambda rel: (artifact/rel).read_text(encoding='utf-8'), 'trial')
    spec, registry = load_criteria(criteria); entry = registry['cases']['three']
    if entry['case_sha256'] != report['environment']['case_sha256']: raise TrialRecordError('trial_registry_case_identity')
    if _hash(root_768/'report.json') != REPORT_768_SHA256: raise TrialRecordError('trial_768_identity')
    report_768 = _read(root_768/'report.json', MAX_REPORT_BYTES); env, env_768 = report['environment'], report_768['environment']
    if env['case_sha256'] != env_768['cases']['three']: raise TrialRecordError('trial_768_case_identity')
    row_768 = report_768['documents'][0]; read_768 = pinned_reader(root_768/row_768['artifact'], row_768)
    _, diagnosis_768 = replay(case, row_768, report_768['attempts'], read_768, 'trial_768')
    errors = diagnosis['finite_gold']['annotation_errors']
    mapping = pf.map_errors(errors, pf._historical_evaluations(diagnosis, registry, spec))
    mapping_768 = pf.map_errors(diagnosis_768['finite_gold']['annotation_errors'], pf._historical_evaluations(diagnosis_768, registry, spec))
    result = pf.assess(case, outputs, entry, spec, spec['semantic_judgments'])
    combinations = []
    for policies in COMBINATIONS:
        changed = spec
        for policy, choice, label in policies: changed = pf.with_policy(changed, policy, choice, label)
        combined = pf.assess(case, outputs, entry, changed, changed['semantic_judgments'])
        combined_mapping = pf.map_errors(errors, {c['index']: c for c in combined['chunks']})
        combinations.append({'policies': [{'policy': p, 'decision': d, 'label': lab} for p, d, lab in policies],
                             'overall': combined['overall'], 'overall_reasons': combined['overall_reasons'],
                             'deterministic': combined['deterministic'], 'failures': failure_summary(combined),
                             'cause_counts': combined_mapping['cause_counts'],
                             'moved_errors': [after['id'] for before, after in zip(mapping['errors'], combined_mapping['errors'], strict=True)
                                              if before['categories'] != after['categories']]})
    prefix = _read(root/'prompts.json')['extraction']
    prompts = [(i, (folder/str(i)/'original-prompt.txt').read_text(encoding='utf-8'), (folder/str(i)/'prompt.txt').read_text(encoding='utf-8'),
                read_768(f"runs/{row_768['run_id']}/{i}/prompt.txt")) for i in range(1, len(outputs)+1)]
    return {'schema_version': 1, 'issue': 785, 'assessment_model_requests': 0, 'author': 'agent_proposed', 'human_review': 'pending',
            'inputs': {'trial_index_sha256': INDEX_SHA256, 'trial_evaluation_sha256': EVALUATION_SHA256,
                       'trial_report_sha256': _hash(root/'report.json'), 'criteria_sha256': dict(CRITERIA_SHA256),
                       'report_768_sha256': REPORT_768_SHA256},
            'trial': {'revision': env['revision'], 'tree': env['tree'], 'cli_version': env['cli_version'],
                      'binary': env['transport']['binary'], 'binary_sha256': env['binary_sha256'],
                      'candidate_id': env['candidate_id'], 'template_sha256': env['template_sha256'],
                      'status': report['status'], 'stop_reason': report['stop_reason'], 'stop_reasons': report['stop_reasons'],
                      'row_status': row['status'], 'limits': report['limits'], 'budget': report['budget'],
                      'attempts': [{k: a[k] for k in ('chunk', 'repair', 'admission_state', 'resolved_model', 'tokens', 'reported_usd',
                                                      'wrapper_seconds')} for a in report['attempts']]},
            'verdict': evaluation,
            'v2_annotation_errors': errors,
            'v1': {'overall': result['overall'], 'overall_reasons': result['overall_reasons'],
                   'deterministic': result['deterministic'], 'categories': result['categories'],
                   'failures': failure_summary(result),
                   'mapping': mapping, 'sensitivity': pf.sensitivity(spec, registry, diagnosis, mapping),
                   'post_hoc_combinations': combinations},
            'comparison_768': compare_768(env, env_768, prefix, prompts, mapping, mapping_768),
            'scope': ('One three-chunk fixture, one sample. verdict is the trial contract\'s decision, re-derived offline; verdict.reasons '
                      'names its cause. v1 is a separate assessment computed from the retained criteria copies; '
                      'post_hoc_combinations were chosen after the trial.')}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try: result = build()
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError, sqlite3.Error) as exc:
        parser.exit(2, f'Trial assessment failed: {exc}\n')
    sys.stdout.write(pf.render(result))
    return 0


if __name__ == '__main__': raise SystemExit(main())
