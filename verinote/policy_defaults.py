# SPDX-License-Identifier: MPL-2.0
"""Default user-visible policy files for new knowledge bases."""

RELATION_ALIASES_RELPATH = "policy/relation-aliases.md"
TYPED_RELATIONS_RELPATH = "policy/typed-relations.md"

DEFAULT_RELATION_ALIASES = """# Relation aliases map alternate labels to the canonical relation label.
# Format: - `raw relation` -> `canonical relation`
#
# These defaults keep common Korean labels configurable while new extraction
# and query planning can use stable English canonical relation labels.
#
# An alias says two labels are the SAME relation, and the logic engine reads
# facts through this table, so aliasing labels that merely sound related merges
# relations that are not. That is how a false conflict is manufactured: with
# `대표` -> `role`, "김철수 역할 PI" and "김철수 대표 Acme" become two values of
# one functional `role` and the KB is reported inconsistent — though holding a
# title and representing a company is no contradiction. The test: both labels
# must take the same kind of object. `역할`/`직책`/`직위` take a title, so they
# are `role`; `대표` takes an organization and `대표이사` takes a person, so
# they are neither — they stay raw until the policy defines a canonical for them.
- `제공` -> `provides`
- `제공기능` -> `provides`
- `제공 기능` -> `provides`
- `제공서비스` -> `provides`
- `제공 서비스` -> `provides`
- `제공요소` -> `provides`
- `제공 요소` -> `provides`
- `목적` -> `purpose`
- `목표` -> `purpose`
- `프로젝트 목적` -> `purpose`
- `사업 목적` -> `purpose`
- `objective` -> `purpose`
- `goal` -> `purpose`
- `역할` -> `role`
- `직책` -> `role`
- `직위` -> `role`
- `소속` -> `affiliation`
#
# Date relations. The default policy declares `established_on`, `born_on` and
# `died_on` functional (at most one value per subject), but a source says
# "설립" or "founded" — these aliases are what let a contradiction in the
# source's words reach a policy written in canonical labels. Only labels that
# denote a *date* belong here: `born_in` (a place) is a different relation, not
# a spelling of `born_on`.
- `설립` -> `established_on`
- `설립일` -> `established_on`
- `설립연도` -> `established_on`
- `창립` -> `established_on`
- `창립일` -> `established_on`
- `established` -> `established_on`
- `founded` -> `established_on`
- `founded_on` -> `established_on`
- `출생` -> `born_on`
- `출생일` -> `born_on`
- `생년월일` -> `born_on`
- `born` -> `born_on`
- `birth_date` -> `born_on`
- `date_of_birth` -> `born_on`
- `사망` -> `died_on`
- `사망일` -> `died_on`
- `died` -> `died_on`
- `death_date` -> `died_on`
- `date_of_death` -> `died_on`
"""
