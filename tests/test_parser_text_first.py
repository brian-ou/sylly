"""Tests for the parser's text-first / PDF-fallback path selection."""
from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import MagicMock

from pypdf import PdfWriter

from app.services.claude_parser import parse_syllabus_pdf

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "claude_response.json"


def _mock_client_with_text(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    response = MagicMock()
    response.content = [block]
    response.usage = MagicMock(input_tokens=1, output_tokens=2)
    client = MagicMock()
    client.messages.create.return_value = response
    return client


def _blank_pdf() -> bytes:
    """A 1-page blank PDF — pypdf will extract empty text from this, forcing
    the fallback path that uploads the PDF document."""
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_blank_pdf_falls_back_to_document_upload():
    fixture_text = FIXTURE_PATH.read_text()
    client = _mock_client_with_text(fixture_text)
    parse_syllabus_pdf(_blank_pdf(), client=client)

    # The fallback path embeds a `document` content block; the text-first path
    # only has `text` blocks. We should see a document because the blank PDF
    # has no extractable text.
    sent = client.messages.create.call_args.kwargs["messages"][0]["content"]
    types = [b.get("type") for b in sent]
    assert "document" in types


def test_text_extraction_skips_pdf_upload(monkeypatch):
    """When pypdf returns enough text, the PDF should NOT be sent."""
    fixture_text = FIXTURE_PATH.read_text()
    client = _mock_client_with_text(fixture_text)

    # Force `extract_pdf_text` to return a long string so the parser takes
    # the text-first path. This avoids needing to construct a PDF that
    # actually contains text content.
    monkeypatch.setattr(
        "app.services.claude_parser.extract_pdf_text",
        lambda _b: "FAKE EXTRACTED TEXT " * 100,
    )

    parse_syllabus_pdf(b"%PDF-1.4 fake", client=client)

    sent = client.messages.create.call_args.kwargs["messages"][0]["content"]
    types = [b.get("type") for b in sent]
    assert "document" not in types
    # The extracted text should appear in one of the text blocks.
    text_blocks = [b.get("text", "") for b in sent if b.get("type") == "text"]
    assert any("FAKE EXTRACTED TEXT" in t for t in text_blocks)
