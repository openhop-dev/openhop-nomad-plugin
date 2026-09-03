from meshcore_nomad_bridge.text import clean_for_radio, split_for_meshcore


def test_clean_for_radio_removes_markdown_noise() -> None:
    raw = "# Heading\n\n|a|b|\n|--|--|\n```code```\nDone"
    cleaned = clean_for_radio(raw)
    assert "#" not in cleaned
    assert "```" not in cleaned
    assert "|a|b|" not in cleaned
    assert "Done" in cleaned


def test_utf8_split_respects_byte_limit() -> None:
    message = "ASCII text £ euro and emoji 😀 and café accents " * 10
    chunks = split_for_meshcore(message, max_bytes=70, max_chunks=6)
    assert chunks
    for chunk in chunks:
        assert len(chunk.encode("utf-8")) <= 70


def test_multipart_prefixes_present_when_split() -> None:
    message = "Sentence one. Sentence two. Sentence three. " * 8
    chunks = split_for_meshcore(message, max_bytes=60, max_chunks=4)
    assert len(chunks) >= 2
    total = len(chunks)
    for idx, chunk in enumerate(chunks, start=1):
        assert chunk.startswith(f"[{idx}/{total}] ")
        assert len(chunk.encode("utf-8")) <= 60


def test_long_word_fallback_never_exceeds_bytes() -> None:
    message = "x" * 400
    chunks = split_for_meshcore(message, max_bytes=50, max_chunks=4)
    assert chunks
    for chunk in chunks:
        assert len(chunk.encode("utf-8")) <= 50


def test_truncates_when_exceeding_max_chunks() -> None:
    message = "Very long text. " * 200
    chunks = split_for_meshcore(message, max_bytes=80, max_chunks=4)
    assert len(chunks) == 4
    assert "shortened" in chunks[-1].lower()
