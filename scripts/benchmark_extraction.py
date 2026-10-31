"""Repeat isolated synthetic extraction; defaults to fake responses, never user data."""
import argparse
from pathlib import Path

from verinote.agents import CLI_PROVIDERS
from verinote.extraction_benchmark import Limits, measure


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path, help='new output directory; parent must exist')
    parser.add_argument('--cases', nargs='+', default=['short', 'long'])
    parser.add_argument('--mode', choices=['fake', 'cli'], default='fake')
    parser.add_argument('--allow-model-calls', action='store_true', help='explicit consent for this CLI invocation')
    parser.add_argument('--agent', choices=sorted(CLI_PROVIDERS), default='agy')
    parser.add_argument('--model', default='')
    parser.add_argument('--process-mode', choices=['oneshot', 'document', 'batch'], default='oneshot')
    parser.add_argument('--extraction-concurrency', type=int, choices=[1, 2], help='experimental bounded scheduler; Claude document/batch only')
    parser.add_argument('--cli-version', help='operator-supplied version, not independently verified')
    parser.add_argument('--scenario', choices=['normal', 'repair', 'failure', 'timeout'], default='normal')
    parser.add_argument('--repetitions', type=int, default=2)
    parser.add_argument('--max-requests', type=int, default=40)
    parser.add_argument('--max-seconds', type=float, default=120)
    parser.add_argument('--max-tokens', type=int, default=100000)
    parser.add_argument('--call-timeout', type=float, default=30)
    parser.add_argument('--missing-usage', choices=['stop', 'continue'], default='stop')
    args = parser.parse_args(argv)
    if args.mode == 'cli' and not args.allow_model_calls:
        parser.error('실제 CLI 호출에는 별도 승인 후 --allow-model-calls를 지정하세요.')
    try:
        report = measure(args.output, cases=args.cases, scenario=args.scenario, mode=args.mode,
                         agent=args.agent, model=args.model, timeout=args.call_timeout,
                         process_mode=args.process_mode, cli_version=args.cli_version, extraction_concurrency=args.extraction_concurrency,
                         limits=Limits(args.max_requests, args.repetitions, args.max_seconds, args.max_tokens, args.missing_usage))
    except (OSError, ValueError, RuntimeError):
        parser.exit(2, '측정 설정 또는 결과 저장 실패. 기존 부분 report.json이 있으면 확인하세요.\n')
    print(f"{report['mode']} {report['status']}: {report['budget']['requests_started']} attempts")
    return 0 if report['status'] == 'completed' and not report['summary']['failed_repetitions'] else 1


if __name__ == '__main__': raise SystemExit(main())
