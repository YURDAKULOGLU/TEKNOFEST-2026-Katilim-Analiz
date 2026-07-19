from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Literal

from katilim_analiz.contracts import CleanDocument, SourceBlock

BlockKind = Literal["heading", "paragraph", "list_item", "table", "metadata", "other"]


def make_block(
    block_id: str,
    ordinal: int,
    kind: BlockKind,
    text: str,
) -> SourceBlock:
    return SourceBlock(
        id=block_id,
        ordinal=ordinal,
        kind=kind,
        text=text,
        locator=f"main > {kind}:nth-of-type({ordinal + 1})",
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )


def make_document(*texts: tuple[BlockKind, str]) -> CleanDocument:
    blocks = [
        make_block(f"block-{index}", index, kind, text) for index, (kind, text) in enumerate(texts)
    ]
    canonical = "\0".join(block.text_sha256 for block in blocks)
    return CleanDocument(
        id="clean-test-document",
        fetch_artifact_id="fetch-test-document",
        bank_id="test-bank",
        canonical_url="https://www.example.com/kampanya",
        title="Test Kampanyası",
        cleaned_at=datetime(2026, 7, 18, 12, 0, tzinfo=UTC),
        cleaner_version="test-cleaner/1",
        clean_sha256=hashlib.sha256(canonical.encode()).hexdigest(),
        language="tr",
        blocks=blocks,
    )
