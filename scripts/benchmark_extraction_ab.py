"""Plan, run with explicit approval, or independently evaluate frozen extraction A/B."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

from verinote.extraction_ab import experiment_plan
from verinote.extraction_ab_runner import evaluate_saved, run_experiment
from verinote.extraction_benchmark import Limits


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('plan', help='print the frozen 85-document plan without subprocess/model calls')
    evaluate = commands.add_parser('evaluate', help='verify retained artifacts and recompute verdict without model calls')
    evaluate.add_argument('output', type=Path)
    run = commands.add_parser('run', help='execute only after separately approved model/budget limits')
    run.add_argument('--output', type=Path, required=True)
    run.add_argument('--model', required=True)
    run.add_argument('--max-requests', type=int, required=True)
    run.add_argument('--max-seconds', type=float, required=True)
    run.add_argument('--max-tokens', type=int, required=True)
    run.add_argument('--allow-model-calls', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'plan': result = experiment_plan()
        elif args.command == 'evaluate': result = evaluate_saved(args.output)
        else:
            if not args.allow_model_calls: parser.error('실제 호출은 별도 예산 승인과 --allow-model-calls가 필요합니다.')
            _, result = run_experiment(args.output, model=args.model,
                limits=Limits(args.max_requests, 1, args.max_seconds, args.max_tokens, 'stop'),
                invocation=list(sys.argv if argv is None else [parser.prog, *argv]))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        parser.exit(2, 'A/B 설정 또는 기록 실패. 새 출력 폴더의 부분 원본을 보존하고 확인하세요.\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if args.command=='plan' or result['verdict'] in ('accepted', 'rejected') else 1


if __name__ == '__main__': raise SystemExit(main())
