"""Text chunking.

Nothing clever here on purpose — chunking is commodity and every RAG project
has an opinion. This is a reasonable default you can replace: paragraph-aware
splitting with character budgets and overlap, no tokeniser dependency.

If you already have a chunker you trust, use it. The parts of this project
worth copying are the event classification and the permission filter.
"""

from __future__ import annotations

import re
from itertools import pairwise

#: Paragraph break: a blank line, however it is spelled.
_PARAGRAPH = re.compile(r"\n\s*\n+")
#: Sentence end followed by whitespace. Good enough for Western prose.
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
#: Alfresco's PDF extraction pads with tabs and exotic space characters
#: where the original had letter spacing. Those characters in the pattern
#: below are deliberate, hence the noqa.
_WHITESPACE = re.compile(r"[ \t   ]+")  # noqa: RUF001


def normalise(text: str) -> str:
    """Collapse the whitespace artefacts text extraction leaves behind.

    Alfresco's PDF extractor emits runs of tabs and non-breaking spaces where
    the original had letter spacing. Left alone they waste chunk budget and
    pollute embeddings.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WHITESPACE.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(
    text: str,
    max_chars: int = 1200,
    overlap_chars: int = 150,
    min_chars: int = 80,
) -> list[str]:
    """Split ``text`` into overlapping chunks, respecting structure.

    Paragraph boundaries first, sentence boundaries when a paragraph exceeds
    the budget, hard character cuts only as a last resort.

    Args:
        max_chars: upper bound per chunk.
        overlap_chars: tail of each chunk repeated at the head of the next, so
            a sentence split across the boundary stays retrievable.
        min_chars: chunks shorter than this get merged into the previous one,
            which avoids a trailing fragment that embeds to noise.

    Returns:
        Chunks in document order. Empty list for empty input.
    """
    text = normalise(text)
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    units = _split_to_units(text, max_chars)
    chunks = _pack(units, max_chars)
    chunks = _apply_overlap(chunks, overlap_chars, max_chars)
    return _merge_runts(chunks, min_chars)


def _split_to_units(text: str, max_chars: int) -> list[str]:
    """Break text into pieces that each fit the budget."""
    units: list[str] = []
    for paragraph in _PARAGRAPH.split(text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= max_chars:
            units.append(paragraph)
            continue
        for sentence in _SENTENCE.split(paragraph):
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) <= max_chars:
                units.append(sentence)
            else:
                units.extend(
                    sentence[i : i + max_chars] for i in range(0, len(sentence), max_chars)
                )
    return units


def _pack(units: list[str], max_chars: int) -> list[str]:
    """Greedily fill chunks up to the budget."""
    chunks: list[str] = []
    current: list[str] = []
    length = 0

    for unit in units:
        addition = len(unit) + (2 if current else 0)
        if current and length + addition > max_chars:
            chunks.append("\n\n".join(current))
            current, length = [unit], len(unit)
        else:
            current.append(unit)
            length += addition

    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _apply_overlap(chunks: list[str], overlap: int, max_chars: int) -> list[str]:
    """Prepend the tail of each chunk to the next one."""
    if overlap <= 0 or len(chunks) < 2:
        return chunks

    out = [chunks[0]]
    for previous, chunk in pairwise(chunks):
        tail = previous[-overlap:].lstrip()
        candidate = f"{tail}\n\n{chunk}" if tail else chunk
        out.append(candidate if len(candidate) <= max_chars + overlap else chunk)
    return out


def _merge_runts(chunks: list[str], min_chars: int) -> list[str]:
    """Fold chunks below ``min_chars`` into their predecessor."""
    if len(chunks) < 2:
        return chunks

    out: list[str] = []
    for chunk in chunks:
        if out and len(chunk) < min_chars:
            out[-1] = f"{out[-1]}\n\n{chunk}"
        else:
            out.append(chunk)
    return out
