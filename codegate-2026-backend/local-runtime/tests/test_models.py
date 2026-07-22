from __future__ import annotations

import pytest
from pydantic import ValidationError

from llm_wiki_local.models import SourceEvent, SourceEventType


def test_event_alias_and_windows_relative_path_are_normalized() -> None:
    event = SourceEvent(
        event_id="event-1",
        event_type="create",
        source_id="source-1",
        sequence=1,
        relative_path=r"regulations\security.docx",
        source_path="/tmp/security.docx",
    )
    assert event.event_type == SourceEventType.CREATED
    assert event.relative_path == "regulations/security.docx"


def test_update_requires_base_hash() -> None:
    with pytest.raises(ValidationError, match="base_source_sha256"):
        SourceEvent(
            event_id="event-1",
            event_type="updated",
            source_id="source-1",
            sequence=2,
            relative_path="security.docx",
            source_path="/tmp/security.docx",
        )


def test_parent_traversal_is_rejected() -> None:
    with pytest.raises(ValidationError, match="safe relative path"):
        SourceEvent(
            event_id="event-1",
            event_type="created",
            source_id="source-1",
            sequence=1,
            relative_path="../security.docx",
            source_path="/tmp/security.docx",
        )
