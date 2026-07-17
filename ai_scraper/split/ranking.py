"""Domain matching and rank finding — pure functions, no I/O.

Ports internal/processor/url.go from the Go splitter byte-for-byte.
Same substring-match semantics, same 1-based rank, same behaviour when
nothing matches. See the note in the earlier design doc: the substring
match has known false-positive risks (e.g. "app.com" matches "myapp.com")
that we preserve deliberately to keep the numbers on your dashboards
unchanged during cutover.
"""
from __future__ import annotations

def extract_domain(raw_url: str, remove_www: bool = False, case_sensitive: bool = False) -> str:
    """Strip scheme, path, and optionally 'www.' from a URL to get the domain.

    Ports Go's extractDomain. Behaviour:
      - Strips 'https://' or 'http://' prefix.
      - Strips everything from the first '/' onward.
      - If remove_www, strips leading 'www.'.
      - If not case_sensitive, lowercases the result.
    """
    d = raw_url
    for prefix in ("https://", "http://"):
        if d.startswith(prefix):
            d = d[len(prefix):]
            break

    slash = d.find("/")
    if slash != -1:
        d = d[:slash]

    if remove_www and d.startswith("www."):
        d = d[len("www."):]

    if not case_sensitive:
        d = d.lower()

    return d

def find_url_rank(
    links: list[str],
    targets: list[str],
    remove_www: bool = False,
    case_sensitive: bool = False
) -> tuple[str | None, int | None]:
    """Return the first link (URL, 1-based rank) whose extracted domain
    substring-matches any target domain.

    Ports Go's findURLRank. Substring match in both directions:
      `td in ld` OR `ld in td`
    This preserves Go's behaviour exactly (including its false positives).

    Returns (None, None) when nothing matches.
    """
    for i, link in enumerate(links):
        ld = extract_domain(link, remove_www=remove_www, case_sensitive=case_sensitive)
        for td in targets:
            if td in ld or ld in td:
                return link, i + 1
    return None, None