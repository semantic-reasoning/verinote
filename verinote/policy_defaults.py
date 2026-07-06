# SPDX-License-Identifier: MPL-2.0
"""Default user-visible policy files for new knowledge bases."""

RELATION_ALIASES_RELPATH = "policy/relation-aliases.md"
TYPED_RELATIONS_RELPATH = "policy/typed-relations.md"

DEFAULT_RELATION_ALIASES = """# Relation aliases map alternate labels to the canonical relation label.
# Format: - `raw relation` -> `canonical relation`
#
# These defaults keep common Korean provide-related labels configurable instead
# of hard-coding them into query intent parsing.
- `제공기능` -> `제공`
- `제공 기능` -> `제공`
- `제공서비스` -> `제공`
- `제공 서비스` -> `제공`
- `제공요소` -> `제공`
- `제공 요소` -> `제공`
"""
