"""Rebuild the synthetic v2 source and explicit annotations; never call a model."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]/'verinote/benchmark_data/v2'


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def fixture(name, count):
    names = [f'S{i}' for i in range(count+2)]
    entity_quote = '승인된 서비스 개체: '+', '.join(names)+'.'
    definitions = {
        'calls': 'calls는 현재의 무조건적 직접 호출만 나타낸다. 부정·가정·조건·과거·역방향·전이는 제외한다.',
        'archives': 'archives는 현재의 무조건적 직접 보관만 나타낸다. 과거에 종료한 보관은 제외한다.',
        'mirrors': 'mirrors는 현재의 무조건적 직접 복제를 나타낸다. 이 문서에는 직접 복제 근거가 없다.',
    }
    source = ''; gold = []; forbidden = []; annotations = []
    for i in range(count):
        n = i+1; a, b, c = names[i:i+3]
        statement = f'{a}는 {b}를 직접 호출한다.'
        actual = f'구간 {n} 현재 실제 구성: {statement}'
        lines = {
            'negated': f'구간 {n} 부정: {b}는 {a}를 직접 호출하지 않는다.',
            'conditional': f'구간 {n} 조건: 장애가 발생할 때만 {a}는 {c}를 직접 호출한다. 현재 무조건 호출은 아니다.',
            'historical': f'구간 {n} 과거 기록: 2020년에 {a}는 {b}를 직접 보관했다. 2025년부터 그 보관은 중단되었다.',
            'missing_entity': f'구간 {n} 미등록 대상: {a}는 Outside{n}를 직접 호출한다. Outside{n}는 승인 개체에 없다.',
            'missing_relation': f'구간 {n} 소유: {a}는 {b}를 소유한다. 소유는 승인 관계에 없다.',
        }
        offset = len(source)
        block = f'구간 {n}: 현재 구성과 표현 제약을 구분한다.\n'
        if i == 0: block += entity_quote+'\n'+'\n'.join(definitions.values())+'\n'
        block += f'구간 {n} 가정 예시(실제 아님): {statement}\n'+'\n'.join(lines.values())+'\n'
        if name != 'short':
            limit = 8000-len(actual)-1
            j = 0
            while True:
                line = f'중립 기록 {n}-{j:03d}: 점검표의 색상은 회색이며 이 행은 호출·보관·복제의 추가 관계를 진술하지 않는다.\n'
                if len(block)+len(line) > limit: break
                block += line; j += 1
            block = block.ljust(limit)
        block += actual+'\n'
        source += block
        def evidence(text, block=block, offset=offset):
            return {'start': offset+block.index(text), 'end': offset+block.index(text)+len(text), 'text': text}
        gold.append({'id': f'{name}-call-{n}', 'variants': [[a, 'calls', b]], 'evidence': [evidence(actual)], 'sample_quote': actual})
        forbidden.extend([[b, 'calls', a], [a, 'calls', c], [a, 'archives', b]])
        annotations.append({'index': n, 'claims': [{'id': key, 'evidence': evidence(value)} for key, value in lines.items()],
            'coverage': [{'predicate': 'calls', 'status': 'extracted', 'claim_ids': ['conditional', 'missing_entity']},
                         {'predicate': 'archives', 'status': 'not_representable', 'claim_ids': ['historical']},
                         {'predicate': 'mirrors', 'status': 'no_evidence', 'claim_ids': []}],
            'gaps': [{'code': 'qualifier_not_representable', 'claim_ids': ['conditional', 'historical']},
                     {'code': 'missing_entity', 'claim_ids': ['missing_entity']},
                     {'code': 'missing_relation', 'claim_ids': ['missing_relation']}]})
    dictionary = {'entities': [{'id': key, 'name': key, 'type': 'Service', 'quote': entity_quote} for key in names],
                  'aliases': [], 'relations': [{'id': key, 'description': value, 'subject_types': ['Service'], 'object_types': ['Service'], 'quote': value} for key, value in definitions.items()]}
    return {'schema_version': 1, 'dataset_version': 2, 'case_id': name, 'category': 'threshold',
            'source': source, 'source_sha256': sha(source), 'dictionary': dictionary, 'gold': gold,
            'forbidden': forbidden, 'annotations': annotations,
            'notes': 'Agent-authored synthetic evidence. Gold is current unconditional direct calls only; qualifiers and missing vocabulary require grounded claims/gaps. Full actual context disambiguates the hypothetical duplicate. Human review is pending; finite automated checks do not prove arbitrary prose entailment.'}


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    rows = []
    for name, count in [('short', 1), ('three', 3), ('four', 4), ('eight', 8)]:
        value = fixture(name, count)
        (ROOT/f'{name}.json').write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
        rows.append({'id': name, 'file': f'{name}.json', 'sha256': sha(canonical(value))})
    manifest = {'dataset_version': 2, 'chunk_size': 8000, 'extraction_contract': 2,
                'human_review': {'status': 'pending', 'evidence': None}, 'cases': rows}
    (ROOT/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n')


if __name__ == '__main__': main()
