"""RSS 2.0 / Atom feed parsing and fetching, plus the offline fixture source.

Feed XML is untrusted input: it is parsed with defusedxml (no entity expansion / external entities)
and every item is validated into a `RawNewsItem`. Malformed items are skipped and counted; a
response that is not a feed at all (e.g. a proxy's "Blocked site" HTML page) is an error.
"""

from __future__ import annotations

import html
import logging
import re
import tomllib
from collections.abc import Sequence
from datetime import datetime
from email.utils import parsedate_to_datetime
from importlib import resources
from pathlib import Path
from typing import Protocol

import httpx
from defusedxml import ElementTree as SafeET
from pydantic import ValidationError

from mp_core import market_calendar as cal
from mp_core.models import Source, StrictModel
from mp_core.news.models import RawNewsItem

log = logging.getLogger(__name__)

ATOM = "{http://www.w3.org/2005/Atom}"
TAG = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")


class FeedError(RuntimeError):
    pass


class FeedSpec(StrictModel):
    name: str
    url: str
    verified: bool = False


def load_feeds() -> list[FeedSpec]:
    raw = tomllib.loads(resources.files("mp_core.data").joinpath("news_feeds.toml").read_text("utf-8"))
    return [FeedSpec(**f) for f in raw["feed"]]


def _text(x: str | None) -> str:
    """Strip markup and entities from feed text."""
    return WS.sub(" ", html.unescape(TAG.sub(" ", x or ""))).strip()


def _all_text(el) -> str | None:
    """Text of an element including inline children - feeds often put raw <b>/<a> tags in
    descriptions, which XML parses as child elements (findtext would stop at the first one)."""
    return None if el is None else "".join(el.itertext())


def _parse_date(s: str | None) -> datetime | None:
    if not s or not s.strip():
        return None
    s = s.strip()
    try:
        dt = parsedate_to_datetime(s)  # RFC 822 (RSS)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))  # ISO 8601 (Atom)
        except ValueError:
            return None
    # Indian publishers occasionally omit the zone; their timestamps are IST.
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=cal.IST)


def parse_feed(
    content: bytes | str, feed: str, fetched_at: datetime, source: Source = Source.NEWS_FEED
) -> tuple[list[RawNewsItem], int]:
    """Parse RSS or Atom. Returns (items, number of malformed items skipped)."""
    head = (content[:300].decode("utf-8", "replace") if isinstance(content, bytes) else content[:300]).lower()
    if "<html" in head or "<!doctype html" in head:
        raise FeedError(f"{feed}: got an HTML page, not a feed (blocked or moved?)")
    try:
        root = SafeET.fromstring(content)
    except SafeET.ParseError as ex:
        raise FeedError(f"{feed}: not valid XML ({ex})") from None
    except Exception as ex:  # defusedxml raises its own errors for entity attacks
        raise FeedError(f"{feed}: rejected unsafe XML ({type(ex).__name__})") from None

    entries: list[dict] = []
    if root.tag == "rss" or root.find("channel") is not None:
        for it in root.iter("item"):
            entries.append(
                {
                    "title": it.findtext("title"),
                    "url": it.findtext("link") or it.findtext("guid"),
                    "summary": _all_text(it.find("description")),
                    "date": it.findtext("pubDate"),
                }
            )
    elif root.tag == f"{ATOM}feed":
        for e in root.iter(f"{ATOM}entry"):
            link = e.find(f"{ATOM}link[@rel='alternate']")
            if link is None:  # (Elements without children are falsy - never use `or` here)
                link = e.find(f"{ATOM}link")
            entries.append(
                {
                    "title": e.findtext(f"{ATOM}title"),
                    "url": link.get("href") if link is not None else None,
                    "summary": _all_text(e.find(f"{ATOM}summary")) or _all_text(e.find(f"{ATOM}content")),
                    "date": e.findtext(f"{ATOM}published") or e.findtext(f"{ATOM}updated"),
                }
            )
    else:
        raise FeedError(f"{feed}: unknown feed format (root <{root.tag}>)")

    items, bad = [], 0
    for e in entries:
        published = _parse_date(e["date"])
        try:
            items.append(
                RawNewsItem(
                    feed=feed,
                    url=(e["url"] or "").strip(),
                    title=_text(e["title"]),
                    summary=_text(e["summary"])[:2000],
                    published_at=published or fetched_at,
                    published_estimated=published is None,
                    fetched_at=fetched_at,
                    source=source,
                )
            )
        except ValidationError:
            bad += 1
    return items, bad


class NewsSource(Protocol):
    name: str
    url: str
    verified: bool

    async def fetch(self, now: datetime) -> list[RawNewsItem]: ...


class RssSource:
    """One live feed, with conditional GET so unchanged feeds cost a 304."""

    def __init__(self, spec: FeedSpec, client: httpx.AsyncClient) -> None:
        self.name, self.url, self.verified = spec.name, spec.url, spec.verified
        self.client = client
        self._etag: str | None = None
        self._last_modified: str | None = None

    async def fetch(self, now: datetime) -> list[RawNewsItem]:
        headers = {}
        if self._etag:
            headers["If-None-Match"] = self._etag
        if self._last_modified:
            headers["If-Modified-Since"] = self._last_modified
        try:
            resp = await self.client.get(self.url, headers=headers)
        except httpx.HTTPError as ex:
            raise FeedError(f"{self.name}: {type(ex).__name__}: {ex}") from None
        if resp.status_code == 304:
            return []
        if resp.status_code != 200:
            raise FeedError(f"{self.name}: HTTP {resp.status_code}")
        self._etag = resp.headers.get("etag")
        self._last_modified = resp.headers.get("last-modified")
        items, bad = parse_feed(resp.content, self.name, now)
        if bad:
            log.warning("%s: skipped %d malformed items", self.name, bad)
        return items


class FixtureSource:
    """Replays a JSONL file of RawNewsItem. Only items published at or before `now` are returned,
    so a replay never sees tomorrow's news."""

    def __init__(self, path: str | Path, name: str = "fixture") -> None:
        self.name, self.url, self.verified = name, str(path), True
        self.items = [
            RawNewsItem.model_validate_json(line) for line in Path(path).read_text("utf-8").splitlines() if line.strip()
        ]

    async def fetch(self, now: datetime) -> list[RawNewsItem]:
        return [i for i in self.items if i.published_at <= now]


def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(15.0, connect=5.0),
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (market-pulse news reader)"},
        limits=httpx.Limits(max_connections=8),
    )


def rss_sources(client: httpx.AsyncClient, feeds: Sequence[FeedSpec] | None = None) -> list[RssSource]:
    return [RssSource(f, client) for f in (feeds or load_feeds())]
