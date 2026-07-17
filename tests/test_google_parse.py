"""Pure-Python tests for google scraper parsing logic."""
from __future__ import annotations

import re

from ai_scraper.scrape.scrapers.google import (
    _ADL_ANSWER_START_RE,
    _clean_links,
    _extract_div_block,
    parse_google_fragment,
)


# ─────────────────────────────────────────────────────────────────────────────
# _extract_div_block
# ─────────────────────────────────────────────────────────────────────────────

def test_extract_div_block_simple():
    raw = '<div class="n6owBd">answer text</div>'
    out = _extract_div_block(raw, _ADL_ANSWER_START_RE)
    assert out == '<div class="n6owBd">answer text</div>'


def test_extract_div_block_nested():
    raw = (
        '<div class="n6owBd">'
        '<div>inner1</div>'
        '<div>inner2</div>'
        'outer'
        '</div>'
        '<div>unrelated</div>'
    )
    out = _extract_div_block(raw, _ADL_ANSWER_START_RE)
    assert out == (
        '<div class="n6owBd">'
        '<div>inner1</div>'
        '<div>inner2</div>'
        'outer'
        '</div>'
    )


def test_extract_div_block_no_anchor_returns_empty():
    raw = "<div>no match</div>"
    out = _extract_div_block(raw, _ADL_ANSWER_START_RE)
    assert out == ""


def test_extract_div_block_matches_alternate_classes():
    for cls in ("LangJde", "wDYxhc", "IVvmDb", "pWvJNd"):
        raw = f'<div class="foo {cls} bar">x</div>'
        out = _extract_div_block(raw, _ADL_ANSWER_START_RE)
        assert out.startswith(f'<div class="foo {cls} bar">')


# ─────────────────────────────────────────────────────────────────────────────
# parse_google_fragment
# ─────────────────────────────────────────────────────────────────────────────

def test_parse_fragment_extracts_content_when_long_enough():
    """The new parser requires candidates to have at least one citation link
    to be considered — this filters out style-block false-positives."""
    body = (
        '<div class="n6owBd">'
        + ("word " * 30)   # >80 chars after processing
        + '<a href="https://example.com/cite">source</a>'
        + '</div>'
    ).encode("utf-8")
    content, links = parse_google_fragment(body)
    assert "word" in content
    assert len(content) >= 80


def test_parse_fragment_skips_candidate_with_no_links():
    """Style blocks or empty divs matching the anchor class shouldn't produce
    content. The link-count filter guards against this."""
    body = (
        '<div class="n6owBd">'
        + ("filler text " * 30)  # long enough by size
        + '</div>'                 # but no <a href>
    ).encode("utf-8")
    content, _ = parse_google_fragment(body)
    assert content == ""


def test_parse_fragment_skips_content_when_too_short():
    body = '<div class="n6owBd">tiny</div>'.encode("utf-8")
    content, _ = parse_google_fragment(body)
    assert content == ""


def test_parse_fragment_extracts_citation_hrefs():
    body = (
        '<div class="n6owBd">' + ("filler " * 20) + '</div>'
        '<a href="https://example.com/one">a</a>'
        '<a href="https://another.com/x?y=1">b</a>'
    ).encode("utf-8")
    _, links = parse_google_fragment(body)
    assert "https://example.com/one" in links
    assert "https://another.com/x?y=1" in links


def test_parse_fragment_filters_skip_domains():
    body = (
        '<a href="https://www.google.com/search?q=x">g</a>'
        '<a href="https://youtube.com/watch">y</a>'
        '<a href="https://gstatic.com/x">s</a>'
        '<a href="https://legit.com/x">ok</a>'
    ).encode("utf-8")
    _, links = parse_google_fragment(body)
    assert links == ["https://legit.com/x"]


def test_parse_fragment_dedupes_links():
    body = (
        '<a href="https://a.com/1">x</a>'
        '<a href="https://a.com/1">y</a>'
        '<a href="https://a.com/2">z</a>'
    ).encode("utf-8")
    _, links = parse_google_fragment(body)
    assert links == ["https://a.com/1", "https://a.com/2"]


# ─────────────────────────────────────────────────────────────────────────────
# _clean_links
# ─────────────────────────────────────────────────────────────────────────────

def test_clean_links_strips_utm_params():
    out = _clean_links([
        "https://a.com/x?utm_source=chatgpt&utm_medium=cpc&keep=1",
        "https://b.com/?gclid=abc&fbclid=def&real=y",
    ])
    assert out == [
        "https://a.com/x?keep=1",
        "https://b.com/?real=y",
    ]


def test_clean_links_drops_non_http():
    out = _clean_links([
        "javascript:void(0)",
        "mailto:x@y.com",
        "https://ok.com",
    ])
    assert out == ["https://ok.com"]


def test_clean_links_dedupes_after_cleaning():
    out = _clean_links([
        "https://a.com/?utm_source=x",
        "https://a.com/",   # becomes same after cleaning
    ])
    assert out == ["https://a.com/"]