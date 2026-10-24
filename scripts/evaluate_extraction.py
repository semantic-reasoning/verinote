"""Evaluate saved synthetic extraction results offline; never invokes an agent."""
import argparse
import json
from pathlib import Path

from verinote.extraction_evaluation import (
    _read,
    evaluate_case,
    load_case,
    load_manifest,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', required=True, help='bundled case ID')
    parser.add_argument('--result', required=True, type=Path, help='JSON {case_id, chunks}')
    parser.add_argument('--output', type=Path, help='write evaluation JSON; default stdout')
    args = parser.parse_args(argv)
    try:
        result = _read(args.result, 4*1024*1024)
        if not isinstance(result, dict) or set(result) != {'case_id', 'chunks'} or result['case_id'] != args.case:
            raise ValueError('평가 결과 파일에는 일치하는 case_id와 chunks가 필요합니다.')
        report = evaluate_case(load_case(args.case), result['chunks'])
        report['dataset_human_review'] = load_manifest()['human_review']
        rendered = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)+'\n'
        if args.output is None: print(rendered, end='')
        else: args.output.write_text(rendered, encoding='utf-8')
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        parser.error('평가 파일을 읽을 수 없거나 자료·결과 형식이 올바르지 않습니다.')


if __name__ == '__main__': main()
