"""One explicitly authorized fidelity candidate trial, or offline reassessment."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

from verinote.ollama_fidelity_evaluation import LIMITS, MODEL, evaluate, plan, run


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('plan')
    assess = commands.add_parser('evaluate'); assess.add_argument('output', type=Path)
    execute = commands.add_parser('run')
    execute.add_argument('--output', type=Path, required=True)
    execute.add_argument('--model', required=True)
    execute.add_argument('--max-requests', type=int, required=True)
    execute.add_argument('--max-seconds', type=float, required=True)
    execute.add_argument('--max-tokens', type=int, required=True)
    execute.add_argument('--allow-model-calls', action='store_true')
    execute.add_argument('--allow-fidelity-candidate', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'plan': result = plan()
        elif args.command == 'evaluate': result = evaluate(args.output)
        else:
            if (not args.allow_model_calls or not args.allow_fidelity_candidate or args.model != MODEL
                    or args.max_requests != LIMITS['requests'] or args.max_seconds != LIMITS['seconds']
                    or args.max_tokens != LIMITS['tokens']):
                parser.error('명시적인 fidelity 후보·모델·요청·시간·토큰 승인 인자가 필요합니다.')
            _, result = run(args.output, invocation=list(sys.argv if argv is None else [parser.prog, *argv]))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        parser.exit(2, 'Ollama fidelity 평가 설정/기록 오류. 부분 원본을 보존하고 확인하세요.\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if args.command == 'plan' or result['verdict'] in ('accepted_for_further_testing', 'rejected') else 1


if __name__ == '__main__': raise SystemExit(main())
