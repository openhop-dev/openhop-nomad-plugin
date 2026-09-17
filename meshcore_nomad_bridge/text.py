"""Radio-friendly text cleanup and UTF-8 byte-aware chunking."""

from __future__ import annotations

import re

_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_HEADING_RE = re.compile(r"^#{1,6}\s*", re.MULTILINE)
_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_MULTISPACE_RE = re.compile(r"[ \t]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_SENTENCE_BREAK_RE = re.compile(r"(?<=[.!?])\s+")


def clean_for_radio(text: str) -> str:
    """Convert LLM output into compact plain text suitable for low-bandwidth links."""
    cleaned = text.strip()
    cleaned = _CODE_FENCE_RE.sub(" ", cleaned)
    cleaned = _TABLE_LINE_RE.sub(" ", cleaned)
    cleaned = _HEADING_RE.sub("", cleaned)
    # Strip paired inline formatting, not literal arithmetic or identifier underscores.
    for marker in ("**", "__", "*", "_", "`"):
        escaped = re.escape(marker)
        cleaned = re.sub(
            rf"(?<!\w){escaped}(\S(?:[^\n]*?\S)?){escaped}(?!\w)",
            r"\1",
            cleaned,
        )
    cleaned = cleaned.replace("\r\n", "\n")
    cleaned = _MULTISPACE_RE.sub(" ", cleaned)
    cleaned = _MULTI_NEWLINE_RE.sub("\n\n", cleaned)
    cleaned = "\n".join(line.strip() for line in cleaned.split("\n"))
    cleaned = cleaned.strip()
    return cleaned


def split_for_meshcore(text: str, *, max_bytes: int, max_chunks: int) -> list[str]:
    """Split text into UTF-8-safe chunks and apply multipart prefixes when needed."""
    source = text.strip()
    if not source:
        return []

    for total in range(1, max_chunks + 1):
        capacities = [_payload_capacity(i, total, max_bytes) for i in range(1, total + 1)]
        pieces, complete = _split_with_capacities(source, capacities)
        if complete:
            if total == 1:
                return pieces
            return [f"[{idx}/{total}] {piece}" for idx, piece in enumerate(pieces, start=1)]

    capacities = [_payload_capacity(i, max_chunks, max_bytes) for i in range(1, max_chunks + 1)]
    pieces, _ = _split_with_capacities(source, capacities)
    if len(pieces) < max_chunks:
        pieces.extend([""] * (max_chunks - len(pieces)))

    marker = "." * min(3, capacities[-1])
    tail_limit = capacities[-1] - len(marker)
    tail = _truncate_to_bytes(pieces[-1], tail_limit).rstrip() if tail_limit else ""
    # Avoid ending on a partial word when there is a reasonable nearby boundary.
    if len(pieces[-1].encode("utf-8")) > tail_limit and " " in tail:
        boundary = tail.rfind(" ")
        if len(tail[:boundary].encode("utf-8")) >= tail_limit * 0.8:
            tail = tail[:boundary]
    pieces[-1] = tail.rstrip(" .") + marker
    if max_chunks == 1:
        return pieces
    return [f"[{idx}/{max_chunks}] {piece}" for idx, piece in enumerate(pieces, start=1)]


def _payload_capacity(index: int, total: int, max_bytes: int) -> int:
    prefix = f"[{index}/{total}] " if total > 1 else ""
    capacity = max_bytes - len(prefix.encode("utf-8"))
    if capacity <= 0:
        raise ValueError("MAX_CHUNK_BYTES is too small for multipart prefix")
    return capacity


def _split_with_capacities(text: str, capacities: list[int]) -> tuple[list[str], bool]:
    remaining = text
    result: list[str] = []

    for capacity in capacities:
        if not remaining:
            break
        piece, remaining = _take_piece(remaining, capacity)
        result.append(piece)

    complete = not remaining
    return result, complete


def _take_piece(text: str, limit: int) -> tuple[str, str]:
    if len(text.encode("utf-8")) <= limit:
        return text, ""

    hard_cut = _utf8_hard_cut_index(text, limit)
    prefix = text[:hard_cut]

    split_at = _best_split_point(prefix)
    if split_at <= 0:
        split_at = hard_cut

    piece = text[:split_at].strip()
    if not piece:
        split_at = hard_cut
        piece = text[:split_at]

    remainder = text[split_at:].lstrip()
    return piece, remainder


def _utf8_hard_cut_index(text: str, limit: int) -> int:
    total = 0
    for idx, char in enumerate(text, start=1):
        b = len(char.encode("utf-8"))
        if total + b > limit:
            return max(1, idx - 1)
        total += b
    return len(text)


def _best_split_point(prefix: str) -> int:
    # Natural boundaries are useful only when they do not waste most of a packet.
    minimum = len(prefix.encode("utf-8")) * 0.8
    paragraph = prefix.rfind("\n\n")
    if paragraph > 0 and len(prefix[:paragraph].encode("utf-8")) >= minimum:
        return paragraph

    sentence_break = 0
    for match in _SENTENCE_BREAK_RE.finditer(prefix):
        before = prefix[: match.start()]
        if re.search(r"(?:^|\s)\d+\.$", before):
            continue
        sentence_break = match.end()
    if sentence_break > 0 and len(prefix[:sentence_break].encode("utf-8")) >= minimum:
        return sentence_break

    space = prefix.rfind(" ")
    if space > 0:
        return space

    return 0


def _truncate_to_bytes(text: str, limit: int) -> str:
    if len(text.encode("utf-8")) <= limit:
        return text
    cut = _utf8_hard_cut_index(text, limit)
    return text[:cut]
