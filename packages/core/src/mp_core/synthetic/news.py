"""Synthetic news for a synthetic market dataset - development data, never real news.

Every item: source="synthetic", feed "synthetic_wire_*", URL on the reserved `.invalid` TLD. Each
item ships with a ground-truth label (what an ideal classifier should say), used to (a) run the
pipeline offline via FixtureLabelClassifier and (b) measure Haiku's agreement once a key is set.

Gap days in the market dataset get a pre-open macro story in the direction of the gap, so the
fusion engine has something to find; ~20% of stories are syndicated by a second wire with a
reworded title (exercises fuzzy de-duplication).
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np

from mp_core import market_calendar as cal
from mp_core.dataset import DayLabel, read_manifest
from mp_core.models import Source
from mp_core.news.models import Horizon, Impact, NewsClassification, RawNewsItem
from mp_core.registry import get_index

# (title template, summary template, sentiment, impact, horizon, entity kind)
COMPANY = [
    (
        "{name} Q2 net profit rises {x}% on strong loan growth",
        "{name} reported higher quarterly earnings, helped by loan growth and stable margins.",
        0.6,
        "high",
        "days",
    ),
    (
        "{name} Q2 profit falls {x}% as provisions jump",
        "Provisions for bad loans rose sharply at {name}, weighing on quarterly profit.",
        -0.6,
        "high",
        "days",
    ),
    (
        "{name} raises Rs {x},000 crore via bond issue",
        "{name} completed a bond issue to fund loan growth.",
        0.15,
        "low",
        "weeks",
    ),
    (
        "{name} deposit growth slows to {x}% in the quarter",
        "{name}'s provisional update showed slower deposit growth.",
        -0.3,
        "med",
        "days",
    ),
    (
        "Brokerage upgrades {name} to buy, sees {x}% upside",
        "A domestic brokerage upgraded {name}, citing improving asset quality.",
        0.4,
        "med",
        "days",
    ),
]
SECTOR = [
    (
        "Bank credit growth picks up to {x}% in latest fortnight",
        "Data showed faster credit growth across banks.",
        0.35,
        "med",
        "days",
        ["SECTOR_BANKS"],
    ),
    (
        "Banks' gross NPAs rise for the first time in {x} quarters",
        "Asset quality worsened across lenders.",
        -0.5,
        "high",
        "weeks",
        ["SECTOR_BANKS"],
    ),
    (
        "RBI keeps policy rate unchanged, signals comfortable liquidity",
        "The central bank held rates and eased liquidity concerns.",
        0.3,
        "high",
        "days",
        ["RBI_POLICY", "SECTOR_BANKS"],
    ),
    (
        "RBI tightens norms on unsecured lending by banks",
        "New risk weights raise capital needs for some lenders.",
        -0.45,
        "high",
        "weeks",
        ["RBI_POLICY", "SECTOR_BANKS"],
    ),
    (
        "FIIs turn net buyers of Indian financials for {x}th straight session",
        "Foreign investors added to financial stocks.",
        0.3,
        "med",
        "intraday",
        ["FLOWS"],
    ),
    (
        "Rupee weakens past key level as crude climbs",
        "The rupee fell as oil prices rose.",
        -0.3,
        "med",
        "intraday",
        ["MACRO_INDIA", "GLOBAL"],
    ),
]
IRRELEVANT = [
    ("Film collections cross Rs {x} crore in opening week", "Box office update."),
    ("Monsoon session of parliament to begin next month", "Parliamentary calendar announced."),
    ("Cricket board announces squad for home series", "Sports update."),
]
GAP = {
    -1: (
        "Global markets slide overnight as US yields spike; Asian peers fall {x}%",
        "Overseas markets fell sharply overnight, pointing to a weak open for Indian equities.",
        -0.7,
    ),
    1: (
        "Global markets rally overnight on soft US inflation; Asian peers gain {x}%",
        "Overseas markets rose sharply overnight, pointing to a strong open for Indian equities.",
        0.7,
    ),
}
REWORD = {
    "rises": "climbs",
    "falls": "drops",
    "Q2 net profit": "Q2 profit",
    "jump": "surge",
    "unchanged": "steady",
    "picks up": "rises",
}


def _label(
    sentiment: float, impact: str, horizon: str, entities: list[str], reason: str, relevant: bool = True
) -> NewsClassification:
    return NewsClassification(
        relevant=relevant,
        entities=entities if relevant else [],
        sentiment=sentiment if relevant else 0.0,
        impact=Impact(impact),
        horizon=Horizon(horizon),
        reason=reason,
    )


def generate_news(dataset_dir: Path, seed: int = 7, per_day: tuple[int, int] = (4, 8)) -> tuple[Path, Path]:
    """Writes news.jsonl and news_labels.jsonl next to the market dataset. Returns both paths."""
    manifest = read_manifest(dataset_dir)
    spec = get_index(manifest.symbol)
    labels_by_day = {}
    events = dataset_dir / "events.json"
    if events.exists():
        labels_by_day = {d.day: d for d in (DayLabel.model_validate(x) for x in json.loads(events.read_text()))}
    rng = np.random.default_rng(seed)
    constituents = spec.constituents
    weights = np.array([c.weight for c in constituents], dtype=float)
    weights = weights / weights.sum() if len(weights) else weights

    items: list[tuple[RawNewsItem, NewsClassification]] = []
    n = 0

    def add(day: date, hh: int, mm: int, title: str, summary: str, label: NewsClassification, wire: str = "a") -> None:
        nonlocal n
        n += 1
        ts = datetime.combine(day, time(hh, mm), cal.IST)
        item = RawNewsItem(
            feed=f"synthetic_wire_{wire}",
            url=f"https://news.synthetic.invalid/{day:%Y%m%d}/{n}",
            title=title,
            summary=summary,
            published_at=ts,
            fetched_at=ts,
            source=Source.SYNTHETIC,
        )
        items.append((item, label))
        if wire == "a" and rng.random() < 0.2:  # syndicated copy with a reworded title
            alt = title
            for a, b in REWORD.items():
                alt = alt.replace(a, b)
            if alt == title:
                alt = title.replace(" on ", " amid ", 1) if " on " in title else title + " - report"
            lag = int(rng.integers(2, 25))
            add_ts = ts + timedelta(minutes=lag)
            add(day, add_ts.hour, add_ts.minute, alt, summary, label, wire="b")

    for day in manifest.days:
        lb = labels_by_day.get(day)
        if lb is not None and lb.is_gap_day:
            sign = 1 if lb.gap_pct > 0 else -1
            t, s, sent = GAP[sign]
            add(
                day,
                8,
                20,
                t.format(x=round(abs(lb.gap_pct) * 1.5, 1)),
                s,
                _label(sent, "high", "intraday", ["GLOBAL", "MACRO_INDIA"], s),
            )
        for _ in range(int(rng.integers(per_day[0], per_day[1] + 1))):
            hh, mm = int(rng.integers(7, 21)), int(rng.integers(0, 60))
            kind = rng.random()
            if kind < 0.5 and constituents:
                c = constituents[int(rng.choice(len(constituents), p=weights))]
                t, s, sent, imp, hor = COMPANY[int(rng.integers(len(COMPANY)))]
                x = int(rng.integers(3, 30))
                add(
                    day,
                    hh,
                    mm,
                    t.format(name=c.name, x=x),
                    s.format(name=c.name),
                    _label(sent, imp, hor, [c.symbol], s.format(name=c.name)),
                )
            elif kind < 0.85:
                t, s, sent, imp, hor, ents = SECTOR[int(rng.integers(len(SECTOR)))]
                add(day, hh, mm, t.format(x=int(rng.integers(3, 15))), s, _label(sent, imp, hor, ents, s))
            else:
                t, s = IRRELEVANT[int(rng.integers(len(IRRELEVANT)))]
                add(
                    day,
                    hh,
                    mm,
                    t.format(x=int(rng.integers(50, 400))),
                    s,
                    _label(0.0, "low", "days", [], "Not related to markets.", relevant=False),
                )

    items.sort(key=lambda p: p[0].published_at)
    news_path, labels_path = dataset_dir / "news.jsonl", dataset_dir / "news_labels.jsonl"
    news_path.write_text("".join(i.model_dump_json() + "\n" for i, _ in items), encoding="utf-8")
    labels_path.write_text(
        "".join(json.dumps({"id": i.id, "label": lb.model_dump(mode="json")}) + "\n" for i, lb in items),
        encoding="utf-8",
    )
    return news_path, labels_path


def load_labels(path: Path) -> dict[str, NewsClassification]:
    out = {}
    for line in Path(path).read_text("utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            out[d["id"]] = NewsClassification.model_validate(d["label"])
    return out
