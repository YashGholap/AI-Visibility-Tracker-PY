"""Tests for split/ranking.py — pure functions, no DB."""
from __future__ import annotations

from ai_scraper.split.ranking import extract_domain, find_url_rank


# ----------------------------------------------------------------------------
# extract_domain
# ----------------------------------------------------------------------------
def test_extract_domain_strips_scheme():
    assert extract_domain("https://example.com") == "example.com"
    assert extract_domain("http://example.com") == "example.com"


def test_extract_domain_strips_path():
    assert extract_domain("https://example.com/some/path") == "example.com"
    assert extract_domain("https://example.com/?q=1") == "example.com"


def test_extract_domain_lowercases_by_default():
    assert extract_domain("https://Example.COM") == "example.com"


def test_extract_domain_preserves_case_when_flag_set():
    assert extract_domain("https://Example.COM", case_sensitive=True) == "Example.COM"


def test_extract_domain_strips_www_when_flag_set():
    assert extract_domain("https://www.example.com", remove_www=True) == "example.com"


def test_extract_domain_preserves_www_by_default():
    assert extract_domain("https://www.example.com") == "www.example.com"


def test_extract_domain_bare_domain_passthrough():
    assert extract_domain("example.com") == "example.com"


# ----------------------------------------------------------------------------
# find_url_rank
# ----------------------------------------------------------------------------
def test_find_url_rank_finds_first_match():
    links = [
        "https://a.com/page",
        "https://target.com/page",
        "https://b.com/page",
    ]
    url, rank = find_url_rank(links, ["target.com"])
    assert url == "https://target.com/page"
    assert rank == 2


def test_find_url_rank_returns_none_when_no_match():
    links = ["https://a.com", "https://b.com"]
    url, rank = find_url_rank(links, ["nope.com"])
    assert url is None
    assert rank is None


def test_find_url_rank_returns_first_of_multiple_matches():
    links = ["https://a.com", "https://target.com/one", "https://target.com/two"]
    url, rank = find_url_rank(links, ["target.com"])
    assert url == "https://target.com/one"
    assert rank == 2


def test_find_url_rank_substring_match_bidirectional():
    """The Go behaviour is bidirectional substring match: td in ld OR ld in td.
    Preserved deliberately for byte-compatibility."""
    # td ("myapp.com") appears inside ld ("myapp.com") — trivially matches.
    assert find_url_rank(["https://myapp.com"], ["myapp.com"]) == ("https://myapp.com", 1)
    # td ("app.com") appears as substring of ld ("myapp.com") — SHOULD match
    # under the current Go semantics (false-positive risk, preserved).
    assert find_url_rank(["https://myapp.com"], ["app.com"]) == ("https://myapp.com", 1)


def test_find_url_rank_case_insensitive_by_default():
    links = ["https://Example.COM/x"]
    url, rank = find_url_rank(links, ["example.com"])
    assert url == "https://Example.COM/x"
    assert rank == 1


def test_find_url_rank_multiple_targets_first_win():
    links = ["https://target2.com", "https://target1.com"]
    url, rank = find_url_rank(links, ["target1.com", "target2.com"])
    # Iteration is over links first, targets second — so target2 in link[0] wins.
    assert url == "https://target2.com"
    assert rank == 1


def test_find_url_rank_empty_inputs():
    assert find_url_rank([], ["x.com"]) == (None, None)
    assert find_url_rank(["https://a.com"], []) == (None, None)
    assert find_url_rank([], []) == (None, None)