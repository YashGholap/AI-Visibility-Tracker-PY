"""Competitor ranking — byte-compatible port of Go's ondemand.go helpers.

- normalize_domain(): strip scheme, www., trailing slash; lowercase.
- compute_competitor_rankings(): for each competitor, find the 1-based
  position of its first substring-match in links. Absent = "not found".
  Empty competitors → None (writer stores SQL NULL).
"""
from __future__ import annotations


def normalize_domain(s: str) -> str:
    """Ports Go's normalizeDomain."""
    s = s.strip().lower()
    for prefix in ("https://", "http://"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    if s.startswith("www."):
        s = s[4:]
    return s.rstrip("/")


def compute_competitor_rankings(
    links: list[str],
    competitors: list[str],
) -> dict[str, int] | None:
    """Return {raw_competitor: 1-based_rank} for competitors found in links.

    Absent key = not found. Returns None (not empty dict) when competitors
    is empty so the DB column stays NULL — matches Go behaviour exactly.
    """
    if not competitors:
        return None

    rankings: dict[str, int] = {}
    for comp in competitors:
        norm = normalize_domain(comp)
        if not norm:
            continue
        for i, link in enumerate(links):
            if norm in link.lower():
                rankings[comp] = i + 1
                break

    return rankings
