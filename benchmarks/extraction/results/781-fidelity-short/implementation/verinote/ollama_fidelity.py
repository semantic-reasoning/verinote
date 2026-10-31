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


CANDIDATE_ID = 'ollama_fidelity_checklist_v1'
TEMPLATE = '''\n추가 점검 순서 (응답 JSON 형태와 검증 기준은 위 지시 그대로 유지):
1. 사실을 고르기 전에 이번 원문의 관련 부정·조건·과거/중단·시점·미등록 어휘 제약을 각각 근거 있는 claim으로 보존하세요. 실제 문장에 없는 제약이나 최소 개수를 만들지 마세요.
2. 입력 사전에서 복사한 승인 관계 ID 목록: {relation_ids}
   위 각 ID마다 정확히 한 coverage 행을 반환하고, 빠짐·중복을 직접 점검하세요. 관계가 없다는 판단도 행을 생략하는 이유가 아닙니다.
3. 직접 근거 자체가 없으면 no_evidence입니다. 근거는 있으나 조건·과거·타입 등으로 승인 사전에 의미를 보존해 표현할 수 없으면 not_representable이며 그 근거 claim을 참조하세요. 일부만 fact로 추출했으면 extracted와 함께 남은 제약의 claim도 참조하세요.
4. 승인된 관계에 현재 직접 사실이 없다는 이유만으로 missing_relation이라고 쓰지 마세요. missing_entity/missing_relation은 실제 사전 어휘가 없을 때만, qualifier_not_representable은 조건·시점 등이 표현되지 않을 때 사용하세요. 각 gap은 해당 제약을 직접 뒷받침하는 claim에 연결하세요.
5. 반환 전 모든 coverage/gap 참조가 관련 claim을 가리키는지 확인하고, 부정·조건·과거를 무조건 fact로 만들지 않았는지 원문과 대조하세요. 원문의 연속 인용을 정확히 복사하고 관련 없는 claim이나 설명으로 근거를 대신하지 마세요.\n'''


def template_hash():
    from .store import digest
    return digest(TEMPLATE.encode())


def compile_prompt(original):
    """Compile only production v2 extraction input; never consult gold or answers."""
    import json

    from .ontology import extraction_prompts
    prefix = extraction_prompts(2)[0]
    if not isinstance(original, str) or not original.startswith(prefix): raise ValueError('후보는 v2 추출 prompt만 지원합니다.')
    payload_text = original[len(prefix):]
    payload = json.loads(payload_text)
    if not isinstance(payload, dict) or set(payload) != {'filename', 'version', 'offset', 'text', 'approved_dictionary'}:
        raise ValueError('후보 입력에는 production 추출 필드만 허용합니다.')
    pin = payload['approved_dictionary']
    if not isinstance(pin, dict) or not isinstance(pin.get('payload'), dict): raise ValueError('승인 사전이 필요합니다.')
    relations = pin['payload'].get('relations')
    if not isinstance(relations, list): raise ValueError('승인 관계 배열이 필요합니다.')
    ids = [r.get('id') if isinstance(r, dict) else None for r in relations]
    if any(not isinstance(k, str) or not k for k in ids) or len(ids) != len(set(ids)):
        raise ValueError('승인 관계 ID가 누락·중복되었습니다.')
    marker = '입력(JSON):\n'
    return prefix[:-len(marker)] + TEMPLATE.format(relation_ids=json.dumps(ids, ensure_ascii=False)) + marker + payload_text


def candidate_options(base_snapshot=None):
    from .ollama import options
    value = options(base_snapshot)
    value.update(schema_version=2, candidate_id=CANDIDATE_ID, candidate_template_sha256=template_hash())
    return options(value)
