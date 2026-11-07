"""Lossless structural fragments for analysis, never independent evidence."""
import copy
from typing import Any

from .store import digest

MAX_PARTITION_UNITS = 2048


def usable(value):
    return bool(value.get('text')) if value['kind'] in ('wiki', 'source') else bool(value['result']['rows'])


def fragment_units(unit, fits):
    """Pack exact subtrees/character ranges; reserve maximal index/count widths."""
    source = {'source_id': unit['id'], 'source_sha256': unit['sha256'],
              'source_category': unit['category'],
              'source_kind': unit['value'].get('kind') if unit['category'] == 'evidence' else None,
              'source_usable': unit['category'] == 'evidence' and usable(unit['value'])}

    def wrap(parts, index=MAX_PARTITION_UNITS, count=MAX_PARTITION_UNITS):
        value = dict(source, index=index, count=count, parts=parts)
        return {'id': f"{unit['id']}:part:{index}", 'category': 'fragment',
                'value': value, 'sha256': digest(value)}

    def fits_parts(parts):
        return fits([wrap(parts)])

    def pieces(value, path):
        part = {'path': path, 'mode': 'value', 'value': value}
        if fits_parts([part]):
            yield part
        elif isinstance(value, (dict, list)) and value:
            declaration = {'path': path, 'mode': 'value', 'value': {} if isinstance(value, dict) else []}
            if not fits_parts([declaration]):
                raise ValueError('분할 기록의 경로와 필수 지시를 입력 예산에 담을 수 없습니다.')
            yield declaration
            for key, child in (value.items() if isinstance(value, dict) else enumerate(value)):
                yield from pieces(child, [*path, key])
        elif isinstance(value, str) and value:
            offset = 0
            while offset < len(value):
                low, high = 0, len(value) - offset
                while low < high:
                    middle = (low + high + 1) // 2
                    candidate = {'path': path, 'mode': 'text', 'offset': offset,
                                 'total_chars': len(value), 'value': value[offset:offset+middle]}
                    if fits_parts([candidate]): low = middle
                    else: high = middle - 1
                if not low:
                    raise ValueError('분할 기록의 한 글자와 필수 지시를 입력 예산에 담을 수 없습니다.')
                yield {'path': path, 'mode': 'text', 'offset': offset,
                       'total_chars': len(value), 'value': value[offset:offset+low]}
                offset += low
        else:
            raise ValueError('분할 기록과 필수 지시를 입력 예산에 담을 수 없습니다.')

    groups: list[list[dict]] = []
    current: list[dict] = []
    for part in pieces(unit['value'], []):
        if current and not fits_parts([*current, part]):
            groups.append(current); current = []
            if len(groups) >= MAX_PARTITION_UNITS:
                raise ValueError('질문 분할 기록 수 한도 초과')
        current.append(part)
    if current: groups.append(current)
    result = [wrap(parts, i, len(groups)) for i, parts in enumerate(groups, 1)]
    if digest(reconstruct(result)) != unit['sha256']:
        raise ValueError('분할 원본 재구성 무결성 오류')
    return result


def reconstruct(units):
    """Verify ordered fragment lineage and rebuild the exact original JSON value."""
    if not units:
        raise ValueError('분할 기록 없음')
    first = units[0]['value']
    identity = {k: first[k] for k in ('source_id', 'source_sha256', 'source_category', 'source_kind', 'source_usable')}
    root: Any = None
    text_lengths = {}
    for index, unit in enumerate(units, 1):
        value = unit['value']
        if (unit['category'] != 'fragment' or unit['sha256'] != digest(value)
                or unit['id'] != f"{first['source_id']}:part:{index}"
                or value['index'] != index or value['count'] != len(units)
                or any(value[k] != v for k, v in identity.items())):
            raise ValueError('분할 기록 출처/해시/범위 오류')
        for part in value['parts']:
            path = part['path']
            parent = root
            for key in path[:-1]: parent = parent[key]
            if part['mode'] == 'text':
                if not path:
                    old = root if root is not None else ''
                elif isinstance(parent, dict):
                    old = parent.get(path[-1], '')
                else:
                    old = parent[path[-1]] if path[-1] < len(parent) else ''
                if not isinstance(old, str) or len(old) != part['offset']:
                    raise ValueError('분할 문자열 offset 오류')
                item = old + part['value']
                text_lengths[tuple(path)] = part['total_chars']
            elif part['mode'] == 'value':
                item = copy.deepcopy(part['value'])
            else:
                raise ValueError('분할 기록 형식 오류')
            if not path: root = item
            elif isinstance(parent, list) and path[-1] == len(parent): parent.append(item)
            else: parent[path[-1]] = item
    for path, length in text_lengths.items():
        value = root
        for key in path: value = value[key]
        if len(value) != length: raise ValueError('분할 문자열 범위 오류')
    if digest(root) != first['source_sha256']:
        raise ValueError('분할 원본 해시 오류')
    return root
