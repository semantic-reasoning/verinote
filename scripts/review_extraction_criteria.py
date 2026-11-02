"""Print authenticated offline observations and pending agent-proposed criteria review."""
import argparse
import json

from verinote.diagnostics.extraction_criteria_review import review


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try: result = review()
    except (OSError, ValueError, KeyError, TypeError) as exc: parser.exit(2, f'Review evidence verification failed: {exc}\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__': raise SystemExit(main())
