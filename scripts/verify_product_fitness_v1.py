"""Print the offline product-fitness v1 fixture verdicts and differences from dataset v2 (#788)."""
import argparse
import json

from verinote.diagnostics.product_fitness_v1 import verify_proposal


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try: result = verify_proposal()
    except (OSError, ValueError, KeyError, TypeError) as exc: parser.exit(2, f'Product-fitness verification failed: {exc}\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__': raise SystemExit(main())
