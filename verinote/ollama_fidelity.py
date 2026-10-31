"""Offline extraction-contract diagnosis; never generates text or repairs semantics."""
from pathlib import Path

from .agents import parse_response
from .extraction_ab_runner import file_index
from .extraction_coverage import repair_missing_indices
from .extraction_evaluation import DATA_ROOT, _read, evaluate_case, load_case
from .extraction_ollama_benchmark import evaluate
from .ontology import dictionary_fact, extraction_quote_issues
from .service import validate_page

REPO = Path(__file__).resolve().parents[1]
HISTORIES = {
    'ollama-qwen25-7b-baseline': '731ce57244b8139c870f2d13d2c3745ac9fc512c122743970d93d849f8b659cb',
    'ollama-qwen38-27b-mlx-baseline': '93bcebcb870385b42807617b6d552920bdcbf7f7576643970ce1396d7d96db20',
}


def assess_output(case, output):
    """Keep production rejection distinct from the unchanged finite gold checks."""
    changes = []
    try:
        extraction_quote_issues(output, case['source'], case['dictionary'], contract_version=2)
        output, changes = repair_missing_indices(output, case['dictionary'])
        issues = extraction_quote_issues(output, case['source'], case['dictionary'], require_coverage=True, contract_version=2)
        if issues:
            return {'production_passed': False, 'production_error': '원문 인용 보정 요청 필요',
                    'quote_repair_required': True, 'accounting_changes': changes, 'finite_gold': None,
                    'scope': 'quote repair requires a new model call; not attempted by offline diagnosis'}
        validate_page(output, case['source'])
        for fact in output['facts']: dictionary_fact(fact, case['source'], case['dictionary'])
    except (ValueError, TypeError, KeyError) as exc:
        return {'production_passed': False, 'production_error': str(exc), 'finite_gold': None,
                'quote_repair_required': False, 'accounting_changes': changes,
                'scope': 'production preflight rejected; semantic annotation diagnosis not completed'}
    gold = evaluate_case(case, [{'index': 1, 'output': output}])
    return {'production_passed': True, 'production_error': None, 'finite_gold': gold,
            'quote_repair_required': False, 'accounting_changes': changes,
            'scope': 'finite fixture/evidence checks, not general natural-language entailment'}


def replay(root):
    root = Path(root).resolve()
    if root.name not in HISTORIES: raise ValueError('지원하는 보존 실험이 아닙니다.')
    before = file_index(root)
    if before.get('report.json', {}).get('sha256') != HISTORIES[root.name]: raise ValueError('보존 report identity 오류')
    evidence = evaluate(root)
    if evidence.get('unverified_other_records') is not False or evidence.get('verdict') != 'failed':
        raise ValueError('보존 실험의 원본 검증 실패')
    report = _read(root/'report.json')
    row = report['documents'][0]
    if row['case_id'] != 'short' or row['attempt_count'] != 1: raise ValueError('단일 준비 응답이 필요합니다.')
    folder = root/row['artifact']/'runs'/row['run_id']/'1'
    raw = _read(folder/'response.json')
    output = parse_response('ollama', raw['response'])[0]
    case = load_case('short', root/'fixtures')
    if case != load_case('short', DATA_ROOT/'v2'): raise ValueError('진단 fixture identity 오류')
    result = assess_output(case, output)
    result.update(model=raw['model'], report_sha256=HISTORIES[root.name], raw_fact_count=len(output.get('facts', [])),
                  approved_relations=[r['id'] for r in case['dictionary']['relations']],
                  returned_relations=[r.get('predicate') for r in output.get('coverage', [])],
                  source_sha256=case['source_sha256'], evidence_verdict=evidence['verdict'])
    if before != file_index(root): raise ValueError('진단 중 원본이 변경됐습니다.')
    return result


def diagnose():
    return {'schema_version': 1, 'cases': {name: replay(REPO/'benchmarks/extraction/results'/name) for name in HISTORIES},
            'generation_requests': 0, 'candidate_evaluated': False,
            'limits': 'Exact failures are reproducible; neither JSON shape nor fact recall proves semantic completeness.'}
