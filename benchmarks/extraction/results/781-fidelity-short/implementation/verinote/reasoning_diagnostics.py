"""Numeric observations, never a guarantee of provider reasoning enforcement."""
from .transports import ExecutionOptions


def _count(value):
    return value if type(value) is int and 0 <= value <= 10**15 else None


def non_reasoning_output(tokens):
    if not isinstance(tokens, dict):
        return None
    output, reasoning = _count(tokens.get('output')), _count(tokens.get('reasoning'))
    if output is None or reasoning is None or reasoning > output:
        return None
    return output - reasoning


def reasoning_diagnostic(call):
    tokens = call.get('tokens', {})
    tokens = tokens if isinstance(tokens, dict) else {}
    output, reasoning = _count(tokens.get('output')), _count(tokens.get('reasoning'))
    remainder = non_reasoning_output(tokens)
    result = {'status': 'not_applicable', 'total_limit': None, 'requested_reasoning': None,
              'answer_target': None, 'reported_reasoning': reasoning, 'reported_output': output,
              'non_reasoning_output': remainder, 'excess_tokens': None}
    if type(call.get('adapter_contract_version')) is not int or call['adapter_contract_version'] != 2:
        return result
    result['status'] = 'unknown'
    try:
        options = ExecutionOptions(2, call.get('max_tokens'), reasoning_max_tokens=call.get('reasoning_max_tokens'),
                                   answer_reserve_tokens=call.get('answer_reserve_tokens'))
    except (ValueError, TypeError):
        return result
    result.update(total_limit=options.max_tokens, requested_reasoning=options.reasoning_max_tokens,
                  answer_target=options.answer_reserve_tokens)
    if remainder is None:
        return result
    excess = max(0, reasoning - options.reasoning_max_tokens)
    result.update(status='exceeded' if excess else 'within_budget', excess_tokens=excess)
    return result
