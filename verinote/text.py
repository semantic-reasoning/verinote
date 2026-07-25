# SPDX-License-Identifier: MPL-2.0
"""One Unicode normalizer, so the write and query halves of a comparison agree."""

from __future__ import annotations

import unicodedata


def nfc(value: str) -> str:
    """Return `value` in NFC (canonical composed) form."""
    return unicodedata.normalize("NFC", value)
