# SPDX-License-Identifier: MPL-2.0
"""Marks tests/contract as a package.

Without this, pytest (prepend import mode) would put this directory on
``sys.path`` and import ``conftest.py`` here under the bare name ``conftest`` —
shadowing the root ``tests/conftest.py`` that ``tests/test_env_isolation.py``
imports directly. The package marker gives this conftest the dotted name
``contract.conftest`` instead, so the two never collide. ``tests/`` itself stays
unpackaged so ``import conftest`` / ``import env_sandbox`` keep resolving there.
"""
