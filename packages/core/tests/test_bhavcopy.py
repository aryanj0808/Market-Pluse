import io
import zipfile
from datetime import date

import httpx
import pytest

from mp_core.loaders.bhavcopy import (
    bhavcopy_url,
    download_bhavcopy,
    parse_bhavcopy,
    read_bhavcopy_zip,
)
from mp_core.models import InstrumentKind

# Fixtures follow NSE's published layouts (hand-written; not real market data).
UDIFF = """TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4
2026-10-23,2026-10-23,FO,NSE,IDF,1,,BANKNIFTY,,2026-10-27,2026-10-27,,,BANKNIFTY26OCTFUT,56100.00,56420.00,55980.00,56310.50,56305.00,56050.00,56280.10,56310.50,1520340,-45210,812340,0,0,F1,30,,,,,
2026-10-23,2026-10-23,FO,NSE,IDO,2,,BANKNIFTY,,2026-10-27,2026-10-27,56500.00,CE,BANKNIFTY26OCT56500CE,210.00,305.40,150.10,188.25,190.00,240.00,56280.10,188.25,2250090,310230,15020340,0,0,F1,30,,,,,
2026-10-23,2026-10-23,FO,NSE,IDO,3,,BANKNIFTY,,2026-10-27,2026-10-27,52000.00,PE,BANKNIFTY26OCT52000PE,0.00,0.00,0.00,0.00,0.00,0.05,56280.10,0.05,1200,0,0,0,0,F1,30,,,,,
2026-10-23,2026-10-23,FO,NSE,IDO,4,,NIFTY,,2026-10-27,2026-10-27,25500.00,PE,NIFTY26OCT25500PE,80.00,95.00,60.00,70.10,70.00,85.00,25610.30,70.10,8800000,120000,99000000,0,0,F1,65,,,,,
2026-10-23,2026-10-23,FO,NSE,STF,5,INE040A01034,HDFCBANK,,2026-10-27,2026-10-27,,,HDFCBANK26OCTFUT,1000.00,1010.00,995.00,1005.00,1005.00,998.00,1003.20,1005.00,50000000,10000,2000000,0,0,F1,550,,,,,
"""

LEGACY = """INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,SETTLE_PR,CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP,
FUTIDX,BANKNIFTY,25-Jan-2024,0,XX,48300,48450,47900,48010.5,48010.5,150234,0,2345600,-12000,02-JAN-2024,
OPTIDX,BANKNIFTY,03-Jan-2024,48000,CE,250,400,120,180.35,180.35,9876543,0,4523000,620000,02-JAN-2024,
OPTSTK,SBIN,25-Jan-2024,640,PE,10,12,9,11,11,1000,0,50000,1000,02-JAN-2024,
"""


def test_udiff_parse_and_filter():
    recs = parse_bhavcopy(UDIFF, symbols=["NSE:BANKNIFTY"])
    assert [r.key.id for r in recs] == [
        "NSE:BANKNIFTY:2026-10-27",
        "NSE:BANKNIFTY:2026-10-27:56500:CE",
        "NSE:BANKNIFTY:2026-10-27:52000:PE",
    ]
    fut, ce, untraded = recs
    assert fut.key.kind is InstrumentKind.FUTURE and fut.oi_change == -45210
    assert ce.underlying_price == 56280.10 and ce.lot_size == 30 and ce.volume_unit == "units"
    assert untraded.open == 0 and untraded.settle == 0.05


def test_udiff_stock_futures_are_not_indices():
    hdfc = next(r for r in parse_bhavcopy(UDIFF) if r.key.underlying == "HDFCBANK")
    assert not hdfc.key.is_index and hdfc.lot_size == 550


def test_legacy_parse():
    recs = parse_bhavcopy(LEGACY)
    assert len(recs) == 3
    fut, ce, stk = recs
    assert fut.trade_date == date(2024, 1, 2) and fut.key.expiry == date(2024, 1, 25)
    assert ce.key.id == "NSE:BANKNIFTY:2024-01-03:48000:CE" and ce.volume_unit == "contracts"
    assert stk.key.is_index is False and stk.lot_size is None


def test_unknown_header_fails_loudly():
    with pytest.raises(ValueError, match="unrecognised bhavcopy header"):
        parse_bhavcopy("Date,Symbol,Close\n2026-10-23,BANKNIFTY,1\n")


def test_bad_row_fails_loudly():
    bad = UDIFF.replace("56420.00,55980.00", "55000.00,55980.00")  # high < low
    with pytest.raises(ValueError, match="high"):
        parse_bhavcopy(bad)


def test_urls():
    assert bhavcopy_url(date(2026, 10, 23)).endswith("/content/fo/BhavCopy_NSE_FO_0_0_0_20261023_F_0000.csv.zip")
    assert bhavcopy_url(date(2024, 1, 2)).endswith("/historical/DERIVATIVES/2024/JAN/fo02JAN2024bhav.csv.zip")


def _zip(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("fo.csv", text)
    return buf.getvalue()


def test_download_and_read_zip(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=_zip(UDIFF))))
    path = download_bhavcopy(date(2026, 10, 23), tmp_path, client)
    assert len(read_bhavcopy_zip(path, ["BANKNIFTY"])) == 3


def test_download_rejects_html_block_page(tmp_path):
    page = b"<!doctype html><title>Blocked site</title>"
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=page)))
    with pytest.raises(ValueError, match="not a zip"):
        download_bhavcopy(date(2026, 10, 23), tmp_path, client)
