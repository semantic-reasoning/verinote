"""Print the offline product-fitness v1 report (#788) that verify_proposal builds."""
import argparse
import sys

from verinote.diagnostics.product_fitness_v1 import render, verify_proposal


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try: result = verify_proposal()
    except (OSError, ValueError, KeyError, TypeError) as exc: parser.exit(2, f'Product-fitness verification failed: {exc}\n')
    sys.stdout.write(render(result))
    return 0


if __name__ == '__main__': raise SystemExit(main())
