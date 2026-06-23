# SPDX-License-Identifier: Apache-2.0
"""Allow `python -m verinote`."""

from verinote.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
