"""CLI telemetry, pinned user rates and bounded accounting for retained jobs."""
import copy
import json
import math
from pathlib import Path
import re

AGENTS = {'agy', 'claude', 'copilot', 'cursor', 'codex'}
TOKEN_KEYS = ('input', 'output', 'cache_read', 'cache_write', 'reasoning')
PRICE_KEYS = ('input', 'output', 'cache_read', 'cache_write')
MAX_METADATA = 256 * 1024


def number(value, integer=False):
    if type(value) not in (int, float) or not 0 <= value <= 10**15 or not math.isfinite(value):
        return None
    return int(value) if not integer or int(value) == value else None


def amount(value):
    if type(value) not in (int, float) or not 0 <= value <= 10**12 or not math.isfinite(value):
        return None
    return value


def validate_rates(value):
    if not isinstance(value, dict) or set(value) - AGENTS:
        raise ValueError('단가는 에이전트별 객체여야 합니다.')
    for rate in value.values():
        if not isinstance(rate, dict) or set(rate) != {'model', 'input', 'output', 'cache_read', 'cache_write', 'credit_unit', 'usd_per_credit'}:
            raise ValueError('단가에는 모델, 입력·출력·캐시 단가, 크레딧 단위와 USD 환산값이 필요합니다.')
        if not isinstance(rate['model'], str) or len(rate['model']) > 150 or any(ord(c) < 32 for c in rate['model']):
            raise ValueError('단가 모델 ID 형식 오류')
        if any(amount(rate[key]) is None or rate[key] > 10**6 for key in PRICE_KEYS):
            raise ValueError('토큰 단가는 0~1,000,000 사이의 유한한 USD 숫자여야 합니다.')
        unit = rate['credit_unit']
        if not isinstance(unit, str) or len(unit) > 60 or any(ord(c) < 32 for c in unit):
            raise ValueError('크레딧 단위는 60자 이하 문자열이어야 합니다.')
        if unit:
            if amount(rate['usd_per_credit']) is None or not 0 < rate['usd_per_credit'] <= 10**6:
                raise ValueError('크레딧 환산값은 양수 USD여야 합니다.')
        elif rate['usd_per_credit'] is not None:
            raise ValueError('환산값을 지정하려면 크레딧 단위도 입력하세요.')
    return copy.deepcopy(value)


def empty_usage(agent, model, rate=None):
    return {'schema_version': 1, 'agent': agent, 'requested_model': model,
            'resolved_model': None, 'tokens': dict.fromkeys(TOKEN_KEYS),
            'reported_usd': None, 'reported_credits': None, 'credit_unit': None,
            'estimated_usd': None, 'estimated_credits': None, 'estimated_credit_unit': None,
            'rate_snapshot': copy.deepcopy(rate), 'rate_basis': None,
            'complete': False, 'status': 'starting', 'warnings': [], 'source': None}


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2), encoding='utf-8')
    temp.replace(path)


def _text_model(value):
    return value if isinstance(value, str) and 0 < len(value) <= 150 else None


def _tokens(raw, keys, additive=False, no_write=False):
    values = {key: number(raw.get(source), True) for key, source in keys.items()}
    values = dict.fromkeys(TOKEN_KEYS) | values
    if no_write:
        values['cache_write'] = 0  # Provider has no separately billable cache-write partition.
    if additive:
        parts = [values[k] for k in ('input', 'cache_read', 'cache_write')]
        values['input'] = sum(parts) if all(v is not None for v in parts) else None
    known = [values[k] for k in ('input', 'cache_read', 'cache_write')]
    if all(v is not None for v in known) and known[1] + known[2] > known[0]:
        values['input'] = None
    if values['reasoning'] is not None and values['output'] is not None and values['reasoning'] > values['output']:
        values['reasoning'] = None
    return values


def decode_transport(agent, stdout, provider_usage=None):
    """Only outer CLI envelopes are telemetry. Never inspect the generated answer."""
    result = empty_usage(agent, '')
    answer, terminal_error = None, None
    raw = None
    try:
        if agent == 'copilot':
            answer = stdout
            raw = provider_usage if isinstance(provider_usage, dict) else {}
            details = raw.get('tokenDetails', {})
            if isinstance(details, dict):
                counts = {k: v.get('tokenCount') for k, v in details.items() if isinstance(v, dict)}
                result['tokens'] = _tokens(counts, {'input': 'input', 'output': 'output', 'cache_read': 'cache_read', 'cache_write': 'cache_write'}, additive=True)
            result['resolved_model'] = _text_model(raw.get('currentModel'))
            nano = amount(raw.get('totalNanoAiu'))
            result['reported_credits'] = nano / 10**9 if nano is not None else None
            result['credit_unit'] = 'GitHub AI credits' if nano is not None else None
            result['complete'] = raw.get('aiCreditsStatus') == 'complete'
            result['source'] = 'copilot --usage-output-file'
        elif agent in ('claude', 'agy'):
            raw = json.loads(stdout)
            if not isinstance(raw, dict):
                raise ValueError('CLI 응답 객체가 없습니다.')
            if agent == 'claude':
                if raw.get('type') != 'result':
                    raise ValueError('Claude result envelope가 없습니다.')
                answer = raw.get('result')
                stats = raw.get('usage', {})
                if isinstance(stats, dict):
                    result['tokens'] = _tokens(stats, {'input': 'input_tokens', 'output': 'output_tokens', 'cache_read': 'cache_read_input_tokens', 'cache_write': 'cache_creation_input_tokens'}, additive=True)
                    detail = stats.get('output_tokens_details', {})
                    if isinstance(detail, dict):
                        result['tokens']['reasoning'] = number(detail.get('thinking_tokens'), True)
                        if result['tokens']['reasoning'] is not None and result['tokens']['output'] is not None and result['tokens']['reasoning'] > result['tokens']['output']:
                            result['tokens']['reasoning'] = None
                models = raw.get('modelUsage', {})
                result['resolved_model'] = _text_model(next(iter(models))) if isinstance(models, dict) and len(models) == 1 else None
                if isinstance(models, dict) and len(models) > 1:
                    result['resolved_model'] = '(multiple models)'
                result['reported_usd'] = amount(raw.get('total_cost_usd'))
                result['complete'] = not raw.get('is_error', False)
                if raw.get('is_error'):
                    terminal_error = 'Claude 실행 실패'
            else:
                if 'status' not in raw or not isinstance(raw.get('response'), str):
                    raise ValueError('Antigravity response envelope가 없습니다.')
                answer = raw['response']
                stats = raw.get('usage', {})
                if isinstance(stats, dict):
                    # agy input_tokens excludes cache_read_tokens; output includes thinking.
                    result['tokens'] = _tokens(stats, {'input': 'input_tokens', 'output': 'output_tokens', 'cache_read': 'cache_read_tokens', 'reasoning': 'thinking_tokens'}, additive=True, no_write=True)
                result['resolved_model'] = _text_model(raw.get('model'))
                result['complete'] = raw.get('status') == 'SUCCESS'
                if not result['complete']:
                    terminal_error = 'Antigravity 실행 실패'
            result['source'] = agent + ' JSON result'
        else:
            events = []
            for line in stdout.splitlines():
                try:
                    event = json.loads(line)
                    if isinstance(event, dict):
                        events.append(event)
                except ValueError:
                    continue  # A killed process may leave one incomplete final line.
            if agent == 'cursor':
                for event in events:
                    if event.get('type') == 'system' and event.get('subtype') == 'init':
                        model = _text_model(event.get('model'))
                        result['resolved_model'] = model if model and model.lower() != 'auto' else None
                    if event.get('type') == 'result':
                        raw = event
                if raw is not None:
                    answer = raw.get('result')
                    stats = raw.get('usage', {})
                    if isinstance(stats, dict):
                        result['tokens'] = _tokens(stats, {'input': 'inputTokens', 'output': 'outputTokens', 'cache_read': 'cacheReadTokens', 'cache_write': 'cacheWriteTokens'}, additive=True)
                    result['complete'] = raw.get('subtype') == 'success' and not raw.get('is_error')
                    if not result['complete']:
                        terminal_error = 'Cursor 실행 실패'
            elif agent == 'codex':
                for event in events:
                    if event.get('type') == 'item.completed' and isinstance(event.get('item'), dict) and event['item'].get('type') == 'agent_message':
                        answer = event['item'].get('text')
                    if event.get('type') == 'turn.completed':
                        raw = event  # One prompt/turn per process; final counter, not deltas.
                    if event.get('type') == 'turn.failed':
                        terminal_error = 'Codex 실행 실패'
                if raw is not None and isinstance(raw.get('usage'), dict):
                    # Codex input includes cached input; cache writes are billed as input.
                    result['tokens'] = _tokens(raw['usage'], {'input': 'input_tokens', 'output': 'output_tokens', 'cache_read': 'cached_input_tokens', 'reasoning': 'reasoning_output_tokens'}, no_write=True)
                    result['complete'] = terminal_error is None
            result['source'] = agent + ' JSON events'
    except (ValueError, TypeError, KeyError):
        result['warnings'].append('구조화된 CLI 사용량을 읽지 못했습니다.')
    if not isinstance(answer, str):
        answer = None
    if not any(v is not None for v in result['tokens'].values()):
        result['warnings'].append('제공자가 토큰 사용량을 보고하지 않았습니다.')
    result['complete'] = result['complete'] and all(result['tokens'][k] is not None for k in PRICE_KEYS)
    return answer, result, terminal_error


def calculate(usage):
    rate = usage.get('rate_snapshot')
    if not rate:
        return usage
    actual, requested = usage.get('resolved_model'), usage.get('requested_model', '')
    if actual == '(multiple models)':
        usage['warnings'].append('여러 모델의 합산 사용량에는 단일 모델 단가를 적용하지 않습니다.')
        return usage
    scope = rate['model']
    if scope:
        if (actual or requested) != scope:
            usage['warnings'].append('사용 모델과 설정 단가의 모델이 달라 비용을 계산하지 않았습니다.')
            return usage
        usage['rate_basis'] = 'resolved_model' if actual else 'requested_model_assumption'
    else:
        if requested:
            return usage
        usage['rate_basis'] = 'configured_default_assumption'
    t = usage['tokens']
    if any(t.get(k) is None for k in PRICE_KEYS):
        return usage
    uncached = t['input'] - t['cache_read'] - t['cache_write']
    if uncached < 0:
        return usage
    weighted = uncached * rate['input'] + t['output'] * rate['output'] + t['cache_read'] * rate['cache_read'] + t['cache_write'] * rate['cache_write']
    cost = weighted / 10**6
    usage['estimated_usd'] = amount(cost) if not (weighted > 0 and cost == 0) else None
    if usage['estimated_usd'] is None:
        usage['warnings'].append('추정 비용이 지원하는 숫자 범위를 벗어나 미확인으로 남겼습니다.')
        return usage
    if rate['credit_unit']:
        converted = cost / rate['usd_per_credit']
        usage['estimated_credits'] = amount(converted) if not (cost > 0 and converted == 0) else None
        if usage['estimated_credits'] is None:
            usage['warnings'].append('크레딧 환산 결과가 지원하는 숫자 범위를 벗어나 미확인으로 남겼습니다.')
        usage['estimated_credit_unit'] = rate['credit_unit']
    return usage


def safe_file(root, relative, limit=MAX_METADATA):
    """No caller-supplied path and no symlink traversal, including log parents."""
    path = Path(root)
    if path.is_symlink():
        return None
    for part in Path(relative).parts:
        if part in ('.', '..') or Path(part).is_absolute():
            return None
        path = path / part
        if path.is_symlink():
            return None
    try:
        if not path.is_file() or path.stat().st_size > limit:
            return None
        with path.open('rb') as stream:
            data = stream.read(limit + 1)
            return data if len(data) <= limit else None
    except OSError:
        return None


def job_calls(root, kind, owner):
    key = owner['id']
    if kind not in ('runs', 'questions') or not re.fullmatch(r'[a-f0-9]{32}', key):
        return []
    paths = [(f'llm-{i}', 'question') for i in range(1, 7)] if kind == 'questions' else [(p, stage) for i in range(1, 17) for p, stage in ((str(i), owner.get('stage', 'legacy')), (f'{i}/quote-repair', 'quote_repair'))]
    calls = []
    for path, stage in paths:
        base = f'{kind}/{key}/{path}'
        data = safe_file(root, base + '/usage.json')
        meta = safe_file(root, base + '/run.json')
        if data is None and meta is None:
            continue
        usage = None
        try:
            value = json.loads(data) if data else None
            if isinstance(value, dict) and type(value.get('schema_version')) is int and value['schema_version'] == 1 and value.get('agent') == owner['agent'] and value.get('status') in ('starting', 'running', 'success', 'error', 'not_started') and isinstance(value.get('tokens'), dict):
                usage = value
        except (ValueError, UnicodeError):
            pass
        if usage is None:
            usage = empty_usage(owner['agent'], owner.get('model', ''))
            usage.update(status='unknown', warnings=['이 호출에는 읽을 수 있는 사용량 기록이 없습니다.'])
        # Only allowlisted metrics reach the API. Raw stdout/prompts are never returned.
        clean = {'schema_version': 1, 'agent': owner['agent'], 'complete': usage.get('complete') is True}
        for k in ('requested_model', 'resolved_model', 'status', 'started_at', 'finished_at', 'source', 'rate_basis'):
            value = usage.get(k)
            clean[k] = value if isinstance(value, str) and len(value) <= 200 else None
        warnings = usage.get('warnings')
        clean['warnings'] = [w[:500] for w in warnings[:8] if isinstance(w, str)] if isinstance(warnings, list) else []
        try:
            metadata = json.loads(meta) if meta else None
            warning = metadata.get('usage_warning') if isinstance(metadata, dict) else None
            if isinstance(warning, str):
                clean['warnings'].append(warning[:500])
        except (ValueError, UnicodeError):
            pass
        if clean['status'] in ('starting', 'running') and owner['status'] not in ('queued', 'running'):
            clean['complete'] = False
            clean['warnings'].append('작업은 종료되었지만 호출의 최종 사용량 기록을 확인하지 못했습니다.')
        try:
            clean['rate_snapshot'] = validate_rates({owner['agent']: usage['rate_snapshot']})[owner['agent']] if usage.get('rate_snapshot') else None
        except ValueError:
            clean['rate_snapshot'] = None
        clean['tokens'] = {k: number(usage['tokens'].get(k), True) for k in TOKEN_KEYS}
        if any(clean['tokens'][k] is None for k in PRICE_KEYS):
            clean['complete'] = False
        for k in ('reported_usd', 'reported_credits', 'estimated_usd', 'estimated_credits'):
            clean[k] = amount(usage.get(k))
        for k in ('credit_unit', 'estimated_credit_unit'):
            unit = usage.get(k)
            clean[k] = unit if isinstance(unit, str) and len(unit) <= 60 else None
        clean.update(call_id=f'{kind}/{key}/{path}', stage=stage, call_count_known=True)
        calls.append(clean)
    if not calls and owner['status'] != 'queued':
        missing = empty_usage(owner['agent'], owner.get('model', ''))
        missing.update(call_id=f'{kind}/{key}/unknown', stage='question' if kind == 'questions' else owner.get('stage', 'legacy'), status='unknown', call_count_known=False, warnings=['호출 로그가 없어 사용량과 호출 횟수를 확인할 수 없습니다.'])
        calls.append(missing)
    return calls


def summarize(calls):
    def subtotal(values):
        known = [v for v in values if v is not None]
        return {'value': sum(known) if known else None, 'known_calls': len(known)}
    credits = []
    for provenance in ('reported', 'estimated'):
        units = {c.get('credit_unit' if provenance == 'reported' else 'estimated_credit_unit') for c in calls}
        for unit in sorted(u for u in units if isinstance(u, str) and u):
            key = 'credit_unit' if provenance == 'reported' else 'estimated_credit_unit'
            subset = [c for c in calls if c.get(key) == unit]
            credits.append({'unit': unit, 'provenance': provenance, **subtotal([c.get(provenance + '_credits') for c in subset])})
    return {'calls': len(calls), 'unknown_calls': sum(c.get('status') == 'unknown' for c in calls),
            'unknown_call_count_jobs': sum(c.get('call_count_known') is False for c in calls),
            'incomplete_calls': sum(not c.get('complete') for c in calls),
            'tokens': {k: subtotal([c['tokens'].get(k) for c in calls]) for k in TOKEN_KEYS},
            'reported_usd': subtotal([c.get('reported_usd') for c in calls]),
            'estimated_usd': subtotal([c.get('estimated_usd') for c in calls]), 'credits': credits}
