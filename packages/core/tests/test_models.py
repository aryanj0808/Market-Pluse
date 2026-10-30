from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from mp_core.models import (
    Candle,
    InstrumentKey,
    InstrumentKind,
    Interval,
    OptionChain,
    OptionChainRow,
    OptionQuote,
    OptionType,
    Quote,
    Source,
    stamp,
)

BN = InstrumentKey.parse("NSE:BANKNIFTY")
NOW = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)


def opt(strike: float, ot: str = "CE", expiry: date = date(2026, 10, 27)) -> InstrumentKey:
    return InstrumentKey(exchange="NSE", underlying="BANKNIFTY", expiry=expiry, strike=strike, option_type=ot)


def oq(strike: float, ot: str = "CE", **kw) -> OptionQuote:
    base = dict(key=opt(strike, ot), ltp=100, bid=99, ask=101, volume=10, oi=1000, oi_change=50)
    return OptionQuote(**(base | kw))


class TestProvenance:
    def test_naive_datetime_rejected(self):
        with pytest.raises(ValueError, match="naive datetime"):
            stamp(1.0, source=Source.SYNTHETIC, as_of=datetime(2026, 10, 1, 9, 15), now=NOW, ttl_seconds=5)

    def test_as_of_normalised_to_utc(self):
        from mp_core.market_calendar import IST

        s = stamp(1.0, source=Source.KITE, as_of=datetime(2026, 10, 1, 11, 30, tzinfo=IST), now=NOW, ttl_seconds=5)
        assert s.as_of == NOW and s.as_of.tzinfo == UTC

    def test_stale_is_computed_from_ttl(self):
        fresh = stamp(1.0, source=Source.KITE, as_of=NOW - timedelta(seconds=4), now=NOW, ttl_seconds=5)
        old = stamp(1.0, source=Source.KITE, as_of=NOW - timedelta(seconds=6), now=NOW, ttl_seconds=5)
        assert not fresh.stale and old.stale
        assert fresh.restamp(NOW + timedelta(seconds=10)).stale

    def test_synthetic_flag(self):
        assert stamp(1, source=Source.SYNTHETIC, as_of=NOW, now=NOW, ttl_seconds=1).is_synthetic


class TestInstrumentKey:
    @pytest.mark.parametrize("text", ["NSE:BANKNIFTY", "NSE:NIFTY:2026-10-27", "NSE:FINNIFTY:2026-10-27:26500:PE"])
    def test_parse_id_roundtrip(self, text):
        assert InstrumentKey.parse(text).id == text

    def test_kinds(self):
        assert BN.kind is InstrumentKind.INDEX
        assert InstrumentKey.parse("NSE:BANKNIFTY:2026-10-27").kind is InstrumentKind.FUTURE
        assert opt(52000).kind is InstrumentKind.OPTION
        assert InstrumentKey.parse("NSE:HDFCBANK", is_index=False).kind is InstrumentKind.EQUITY

    def test_hashable_and_equal(self):
        assert {opt(52000), opt(52000.0)} == {opt(52000)}

    @pytest.mark.parametrize(
        "kw, msg",
        [
            (dict(strike=52000, option_type="CE"), "options need an expiry"),
            (dict(expiry=date(2026, 10, 27), strike=52000), "together"),
            (dict(expiry=date(2026, 10, 27), strike=-1, option_type="CE"), "positive"),
        ],
    )
    def test_invalid_shapes(self, kw, msg):
        with pytest.raises(ValidationError, match=msg):
            InstrumentKey(exchange="NSE", underlying="BANKNIFTY", **kw)

    def test_lowercase_and_unknown_exchange_rejected(self):
        with pytest.raises(ValidationError):
            InstrumentKey(exchange="NSE", underlying="banknifty")
        with pytest.raises(ValidationError):
            InstrumentKey(exchange="LSE", underlying="BANKNIFTY")

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError, match="Extra inputs"):
            InstrumentKey(exchange="NSE", underlying="BANKNIFTY", kite_token=123)


class TestMarketModels:
    def test_candle_ohlc_consistency(self):
        Candle(key=BN, interval=Interval.M1, ts=NOW, open=10, high=12, low=9, close=11)
        with pytest.raises(ValidationError, match="inconsistent OHLC"):
            Candle(key=BN, interval=Interval.M1, ts=NOW, open=10, high=10.5, low=9, close=11)
        with pytest.raises(ValidationError):
            Candle(key=BN, interval=Interval.M1, ts=NOW, open=0, high=1, low=0, close=1)

    def test_quote_ltp_inside_range(self):
        q = Quote(key=BN, ts=NOW, ltp=101, open=100, high=102, low=99, prev_close=100)
        assert q.change == 1 and q.change_pct == pytest.approx(1.0)
        with pytest.raises(ValidationError, match="outside day range"):
            Quote(key=BN, ts=NOW, ltp=103, open=100, high=102, low=99, prev_close=100)

    def test_option_quote_checks(self):
        with pytest.raises(ValidationError, match="crossed market"):
            oq(52000, bid=102, ask=101)
        with pytest.raises(ValidationError, match="needs an option key"):
            OptionQuote(key=BN, ltp=1, bid=1, ask=1, volume=0, oi=0, oi_change=0)

    def test_chain_row_must_match_strike(self):
        with pytest.raises(ValidationError, match="does not belong to strike"):
            OptionChainRow(strike=52100, call=oq(52000))
        with pytest.raises(ValidationError, match="does not belong to strike"):
            OptionChainRow(strike=52000, call=oq(52000, "PE"))

    def test_chain_checks(self):
        rows = [OptionChainRow(strike=k, call=oq(k), put=oq(k, "PE")) for k in (52000, 52100)]
        OptionChain(underlying=BN, expiry=date(2026, 10, 27), ts=NOW, spot=52050, rows=rows)
        with pytest.raises(ValidationError, match="strictly increasing"):
            OptionChain(underlying=BN, expiry=date(2026, 10, 27), ts=NOW, spot=52050, rows=rows[::-1])
        with pytest.raises(ValidationError, match="does not belong to chain"):
            OptionChain(underlying=BN, expiry=date(2026, 11, 24), ts=NOW, spot=52050, rows=rows)
        with pytest.raises(ValidationError):
            OptionChain(underlying=BN, expiry=date(2026, 10, 27), ts=NOW, spot=52050, rows=[])
        assert OptionType.CE == "CE"
