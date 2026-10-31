"""Replay retained Ollama extraction failures without network or model calls."""
import argparse
import json

from verinote.ollama_fidelity import diagnose, replay


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['replay'])
    parser.add_argument('--artifact')
    args = parser.parse_args(argv)
    try: result = replay(args.artifact) if args.artifact else diagnose()
    except (OSError, ValueError, KeyError, TypeError) as exc: parser.exit(2, f'진단 원본 검증 실패: {exc}\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__': raise SystemExit(main())
