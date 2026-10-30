"""De-duplication: canonical URL identity + fuzzy title matching.

The same story is syndicated across feeds with different URLs and slightly different headlines
("HDFC Bank's Q2 profit rises 12%" / "HDFC Bank Q2 profit up 12 per cent"). Titles are reduced to a
set of normalised tokens; two titles are duplicates when their token sets overlap strongly.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from functools import cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = re.compile(r"^(utm_|fbclid$|gclid$|ref$|from$|source$|cmp$|_ga$)")
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "of",
        "to",
        "in",
        "on",
        "for",
        "and",
        "or",
        "by",
        "with",
        "at",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "its",
        "it",
        "this",
        "that",
        "from",
        "after",
        "over",
        "amid",
        "into",
        "says",
        "said",
        "say",
        "per",
        "cent",
        "pc",
        "percent",
        "vs",
        "than",
        "up",
        "down",
    ]
)
SYNONYMS = {
    "rises": "rise",
    "rose": "rise",
    "gains": "rise",
    "jumps": "rise",
    "climbs": "rise",
    "surges": "rise",
    "falls": "fall",
    "fell": "fall",
    "drops": "fall",
    "slips": "fall",
    "declines": "fall",
    "plunges": "fall",
    "q1": "q1",
    "q2": "q2",
    "q3": "q3",
    "q4": "q4",
    "profit": "profit",
    "profits": "profit",
    "bank's": "bank",
    "banks": "bank",
    "rbi's": "rbi",
    "rate": "rate",
    "rates": "rate",
}
TOKEN = re.compile(r"[a-z0-9]+(?:'s)?")


def canonical_url(url: str) -> str:
    """Lower-case scheme/host, drop fragments, tracking params and trailing slashes."""
    parts = urlsplit(url.strip())
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query) if not TRACKING_PARAMS.match(k.lower())))
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower() or "https", parts.netloc.lower().removeprefix("www."), path, query, ""))


def title_tokens(title: str) -> list[str]:
    toks = []
    for t in TOKEN.findall(title.lower().replace("’", "'")):
        t = SYNONYMS.get(t, t.removesuffix("'s"))
        if t not in STOPWORDS:
            toks.append(t)
    return sorted(set(toks))


def title_key(title: str) -> str:
    """Exact fingerprint of the normalised title (catches case/punctuation/word-order variants)."""
    return hashlib.sha1(" ".join(title_tokens(title)).encode()).hexdigest()[:16]


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


GENERIC_NAME_WORDS = frozenset(
    {"bank", "of", "india", "the", "small", "finance", "first", "ltd", "limited", "national"}
)


@cache
def entity_words() -> frozenset[str]:
    """Distinctive words of known company names and tickers (hdfc, icici, sbin, kotak, ...)."""
    from mp_core.registry import load_indices

    words: set[str] = set()
    for spec in load_indices().values():
        for c in spec.constituents:
            words.add(c.symbol.lower())
            words |= {w for w in title_tokens(c.name) if w not in GENERIC_NAME_WORDS}
    return frozenset(words)


def _numbers(tokens: Iterable[str]) -> set[str]:
    return {t for t in tokens if any(ch.isdigit() for ch in t)}


def find_duplicate(tokens: list[str], recent: Iterable[tuple[str, list[str]]], threshold: float = 0.75) -> str | None:
    """Id of the first recent item that is the same story, else None.

    Same story = identical normalised tokens, or (both >= 4 tokens and Jaccard >= threshold). Titles
    whose numbers differ ("credit growth 6%" vs "14%") or that name different companies ("HDFC Bank
    profit rises" vs "ICICI Bank profit rises") are never the same story."""
    nums = _numbers(tokens)
    names = set(tokens) & entity_words()
    for item_id, other in recent:
        if tokens == other:
            return item_id
        other_nums = _numbers(other)
        if nums and other_nums and nums != other_nums:
            continue
        other_names = set(other) & entity_words()
        if names and other_names and names != other_names:
            continue
        if min(len(tokens), len(other)) >= 4 and jaccard(tokens, other) >= threshold:
            return item_id
    return None
