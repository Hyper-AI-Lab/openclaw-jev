"""Structure-aware chunking: markdown headings become the table of contents, and every chunk points at its section."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Tuple

TARGET_CHARS = 1400
MAX_CHARS = 1800
OVERLAP_CHARS = 200
MIN_CHARS = 200
DEFAULT_TITLE = "Content"
INTRO_TITLE = "Introduction"

_ATX = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_SETEXT = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_EMPHASIS = re.compile(r"^(\*\*|__)(.+)\1$")


@dataclass(frozen=True)
class SectionSpec:
    ordinal: int
    title: str
    path: str
    level: int
    text: str


@dataclass(frozen=True)
class ChunkSpec:
    section_ordinal: int
    ordinal: int
    text: str


def normalize(text: str) -> str:
    lines = [line.rstrip() for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _clean_title(raw: str) -> str:
    title = raw.strip()
    match = _EMPHASIS.match(title)
    if match:
        title = match.group(2).strip()
    # Page extractors append anchor links to headings: "Setup[](https://…#setup)", "Setuphttps://…#setup".
    title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", title)
    title = re.sub(r"https?://\S+$", "", title).strip()
    return title.rstrip(":").strip()


def _headings(lines: List[str]) -> List[Tuple[int, int, str, int]]:
    """(line index, level, title, lines the heading takes) outside fenced code."""
    found: List[Tuple[int, int, str, int]] = []
    fence = ""
    i = 0
    while i < len(lines):
        line = lines[i]
        opener = _FENCE.match(line)
        if fence:
            if opener and opener.group(1)[0] == fence[0] and len(opener.group(1)) >= len(fence):
                fence = ""
            i += 1
            continue
        if opener:
            fence = opener.group(1)
            i += 1
            continue
        atx = _ATX.match(line)
        if atx:
            title = _clean_title(atx.group(2))
            if title:
                found.append((i, len(atx.group(1)), title, 1))
            i += 1
            continue
        if line.strip() and i + 1 < len(lines):
            underline = _SETEXT.match(lines[i + 1])
            if underline and len(underline.group(1)) >= 3:
                title = _clean_title(line)
                if title:
                    found.append((i, 1 if underline.group(1)[0] == "=" else 2, title, 2))
                    i += 2
                    continue
        i += 1
    return found


def split_sections(text: str, *, root_title: str = "") -> List[SectionSpec]:
    body = normalize(text)
    if not body:
        return []
    lines = body.split("\n")
    heads = _headings(lines)
    if not heads:
        title = root_title or DEFAULT_TITLE
        return [SectionSpec(0, title, title, 1, body)]
    raw: List[Tuple[str, str, int, str]] = []
    intro = "\n".join(lines[: heads[0][0]]).strip()
    if intro:
        title = root_title or INTRO_TITLE
        raw.append((title, title, 1, intro))
    stack: List[Tuple[int, str]] = []
    for n, (line_no, level, title, taken) in enumerate(heads):
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        section_body = "\n".join(lines[line_no + taken : end]).strip()
        if section_body:
            raw.append((title, " > ".join(t for _, t in stack), level, section_body))
    return [SectionSpec(i, title, path, level, section_body) for i, (title, path, level, section_body) in enumerate(raw)]


def _blocks(text: str) -> List[str]:
    """Paragraphs split on blank lines; a fenced code block stays one block."""
    blocks: List[str] = []
    current: List[str] = []
    fence = ""
    for line in text.split("\n"):
        opener = _FENCE.match(line)
        if fence:
            current.append(line)
            if opener and opener.group(1)[0] == fence[0] and len(opener.group(1)) >= len(fence):
                fence = ""
            continue
        if opener:
            fence = opener.group(1)
            current.append(line)
            continue
        if not line.strip():
            if current:
                blocks.append("\n".join(current))
                current = []
            continue
        current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _hard_split(unit: str, max_chars: int) -> List[str]:
    pieces: List[str] = []
    while len(unit) > max_chars:
        cut = unit.rfind(" ", 0, max_chars + 1)
        if cut <= 0:
            cut = max_chars
        pieces.append(unit[:cut].rstrip())
        unit = unit[cut:].lstrip()
    if unit:
        pieces.append(unit)
    return pieces


def _split_block(block: str, target: int, max_chars: int) -> List[Tuple[str, str]]:
    """An oversized block as (piece, separator to the next piece) pairs of at most max_chars."""
    sep = "\n" if "\n" in block else " "
    units = block.split("\n") if sep == "\n" else _SENTENCE_END.split(block)
    parts: List[str] = []
    for unit in units:
        parts.extend(_hard_split(unit, max_chars) if len(unit) > max_chars else [unit])
    pieces: List[str] = []
    current = ""
    for part in parts:
        if not current:
            current = part
        elif len(current) + len(sep) + len(part) <= target:
            current = current + sep + part
        else:
            pieces.append(current)
            current = part
    if current:
        pieces.append(current)
    return [(piece, sep) for piece in pieces[:-1]] + [(pieces[-1], "\n\n")]


def chunk_text(
    text: str,
    *,
    target: int = TARGET_CHARS,
    max_chars: int = MAX_CHARS,
    overlap: int = OVERLAP_CHARS,
) -> List[str]:
    body = normalize(text)
    if not body:
        return []
    pieces: List[Tuple[str, str]] = []
    for block in _blocks(body):
        pieces.extend(_split_block(block, target, max_chars) if len(block) > max_chars else [(block, "\n\n")])
    chunks: List[List[Any]] = []  # [text, separator after it]
    for piece, sep in pieces:
        if chunks and len(chunks[-1][0]) + len(chunks[-1][1]) + len(piece) <= target:
            chunks[-1][0] = chunks[-1][0] + chunks[-1][1] + piece
            chunks[-1][1] = sep
        else:
            chunks.append([piece, sep])
    if len(chunks) >= 2 and len(chunks[-1][0]) < MIN_CHARS:
        merged = chunks[-2][0] + chunks[-2][1] + chunks[-1][0]
        if len(merged) <= max_chars:
            chunks[-2] = [merged, chunks[-1][1]]
            chunks.pop()
    out = [chunks[0][0]]
    for (prev, prev_sep), (current, _) in zip(chunks, chunks[1:]):
        prefix = ""
        if overlap > 0:
            tail = prev[-overlap:]
            if len(prev) > overlap:
                space = re.search(r"\s", tail)
                tail = tail[space.end():] if space else ""
            prefix = tail.strip()
        joiner = "\n\n" if prev_sep == "\n\n" else " "
        out.append(prefix + joiner + current if prefix else current)
    return out


def chunk_document(text: str, *, root_title: str = "") -> Tuple[List[SectionSpec], List[ChunkSpec]]:
    sections = split_sections(text, root_title=root_title)
    chunks: List[ChunkSpec] = []
    for section in sections:
        for piece in chunk_text(section.text):
            chunks.append(ChunkSpec(section.ordinal, len(chunks), piece))
    return sections, chunks


def chunk_turn(text: str) -> List[str]:
    body = normalize(text)
    if not body:
        return []
    return [body] if len(body) <= MAX_CHARS else chunk_text(body)


def toc_entries(sections: Sequence[SectionSpec]) -> List[Dict[str, Any]]:
    return [{"ordinal": s.ordinal, "title": s.title, "path": s.path, "level": s.level} for s in sections]
