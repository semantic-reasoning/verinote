# SPDX-License-Identifier: MPL-2.0
"""Orchestration: extract -> (review) -> compile -> check.

The pipeline is intentionally thin glue over store/, llm/ and engine/. The human
review gate sits between extract and compile and is driven from the web UI.
"""

from verinote.pipeline.extract import (
    ChunkedExtractionResult,
    SyncResult,
    create_chunked_extraction_job,
    extract_source,
    process_extraction_job,
    sync_sources,
)
from verinote.pipeline.ingest import (
    IngestError,
    ingest_bytes,
    ingest_file,
    store_source,
    supported_suffixes,
)
from verinote.pipeline.query import translate_questions, write_query_file
from verinote.pipeline.repair import repair_questions
from verinote.pipeline.trust import fact_trust_summary
from verinote.pipeline.verify import verify

__all__ = [
    "extract_source",
    "create_chunked_extraction_job",
    "process_extraction_job",
    "sync_sources",
    "SyncResult",
    "ChunkedExtractionResult",
    "verify",
    "ingest_bytes",
    "ingest_file",
    "store_source",
    "supported_suffixes",
    "IngestError",
    "translate_questions",
    "write_query_file",
    "repair_questions",
    "fact_trust_summary",
]
