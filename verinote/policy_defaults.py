# SPDX-License-Identifier: MPL-2.0
"""Default user-visible policy files for new knowledge bases."""

RELATION_ALIASES_RELPATH = "policy/relation-aliases.md"
TYPED_RELATIONS_RELPATH = "policy/typed-relations.md"

DEFAULT_RELATION_ALIASES = """# Relation aliases map alternate labels to the canonical relation label.
# Format: - `raw relation` -> `canonical relation`
#
# These defaults keep common Korean labels configurable while new extraction
# and query planning can use stable English canonical relation labels.
- `제공` -> `provides`
- `제공기능` -> `provides`
- `제공 기능` -> `provides`
- `제공서비스` -> `provides`
- `제공 서비스` -> `provides`
- `제공요소` -> `provides`
- `제공 요소` -> `provides`
- `역할` -> `role`
- `직책` -> `role`
- `직위` -> `role`
- `소속` -> `affiliation`
"""
