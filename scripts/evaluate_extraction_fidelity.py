"""Plan a bounded evidence-link candidate trial, run explicitly, or verify offline."""
import argparse
import json
import sys
from pathlib import Path

from verinote.diagnostics.extraction_fidelity_artifacts import evaluate, plan
from verinote.diagnostics.extraction_fidelity_trial import run


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('plan')
    offline = commands.add_parser('evaluate'); offline.add_argument('artifact', type=Path)
    generate = commands.add_parser('run'); generate.add_argument('--output', type=Path, required=True)
    generate.add_argument('--allow-model-calls', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'plan': result = plan()
        elif args.command == 'evaluate': result = evaluate(args.artifact)
        else: _, result = run(args.output, allow_model_calls=args.allow_model_calls, invocation=list(sys.argv) if argv is None else list(argv))
    except (OSError, ValueError) as exc: parser.exit(2, f'{exc}\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__': raise SystemExit(main())
