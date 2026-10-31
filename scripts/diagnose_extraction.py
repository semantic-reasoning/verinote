"""Diagnose preserved A/B evidence without subprocesses, model calls or raw writes."""
import argparse
import json
from pathlib import Path

from verinote.extraction_diagnostics import diagnose_saved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.output and args.source.resolve() in (args.output.resolve(), *args.output.resolve().parents):
        parser.error('output must be outside original evidence')
    try:
        result = json.dumps(diagnose_saved(args.source), ensure_ascii=False, indent=2)+'\n'
        if args.output:
            with args.output.open('x') as stream: stream.write(result)
        else: print(result, end='')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f'diagnosis failed: {type(exc).__name__}\n')


if __name__ == '__main__': main()
