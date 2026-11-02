"""Authenticate and explain the pinned Claude extraction failure, without calls."""
import argparse
import json
from pathlib import Path

from verinote.diagnostics.extraction_fidelity import (
    DEFAULT_ROOT,
    ArtifactVerificationError,
    diagnose,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifact', type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args(argv)
    try:
        result = diagnose(args.artifact)
    except ArtifactVerificationError as exc:
        parser.exit(2, f'Artifact verification failed (not a production diagnosis): {exc}\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__': raise SystemExit(main())
