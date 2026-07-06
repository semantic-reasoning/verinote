# SPDX-License-Identifier: MPL-2.0
"""Deterministic quality policies for query candidate answers."""

from __future__ import annotations

from dataclasses import dataclass
import unicodedata

from verinote.pipeline.query_planner import QueryCandidateFamily

BROAD_RELATION_DISCOVERY_FAMILIES = frozenset(
    {
        QueryCandidateFamily.SUBJECT_RELATION_DISCOVERY,
        QueryCandidateFamily.OBJECT_RELATION_DISCOVERY,
    }
)

LOW_SIGNAL_RELATION_LABELS = frozenset(
    {
        "column",
        "field",
        "metadata",
        "row",
        "source",
        "value",
    }
)


@dataclass(frozen=True)
class RelationDiscoveryQualityDecision:
    allowed: bool
    reason: str | None = None
    normalized_label: str | None = None


def evaluate_relation_discovery_label(
    label: str | None,
) -> RelationDiscoveryQualityDecision:
    """Return a deterministic broad-discovery quality decision for a relation label."""
    if label is None or not label.strip():
        return RelationDiscoveryQualityDecision(
            allowed=False,
            reason="relation discovery candidate lacks a relation label",
        )
    normalized = normalize_relation_quality_label(label)
    if normalized in LOW_SIGNAL_RELATION_LABELS:
        return RelationDiscoveryQualityDecision(
            allowed=False,
            reason=f"relation label requires review: {normalized}",
            normalized_label=normalized,
        )
    return RelationDiscoveryQualityDecision(
        allowed=True,
        normalized_label=normalized,
    )


def normalize_relation_quality_label(label: str) -> str:
    return unicodedata.normalize("NFC", label).strip().casefold()
