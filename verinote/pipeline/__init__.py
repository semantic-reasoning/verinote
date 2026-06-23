# SPDX-License-Identifier: MPL-2.0
"""Orchestration: extract -> (review) -> compile -> check.

The pipeline is intentionally thin glue over store/, llm/ and engine/. The human
review gate sits between extract and compile and is driven from the web UI.
"""

from verinote.pipeline.extract import SyncResult, extract_source, sync_sources
from verinote.pipeline.ingest import (
    IngestError,
    ingest_bytes,
    ingest_file,
    store_source,
    supported_suffixes,
)
from verinote.pipeline.query import translate_questions, write_query_file
from verinote.pipeline.verify import verify

__all__ = [
    "extract_source",
    "sync_sources",
    "SyncResult",
    "verify",
    "ingest_bytes",
    "ingest_file",
    "store_source",
    "supported_suffixes",
    "IngestError",
    "translate_questions",
    "write_query_file",
]
