from pathlib import Path
import json

import numpy as np
import pandas as pd
import pytest


def small_prices():
    return pd.DataFrame({
        "ticker": ["0050", "0050"],
        "date": ["2025-01-02", "2025-01-03"],
        "open": [50.0, 51.0], "high": [52.0, 53.0],
        "low": [49.0, 50.0], "close": [51.0, 52.0],
        "adj_close": [25.5, 26.0], "volume": [1000, 1200],
        "available_at": ["2025-01-02T12:00:00Z", "2025-01-03T12:00:00Z"],
        "source": ["verified_csv"] * 2,
        "adjustment_source": ["vendor_total_return"] * 2,
    })


def test_price_validation_preserves_ticker_and_utc():
    from twquant.data import validate_prices
    result = validate_prices(small_prices())
    assert result.ticker.tolist() == ["0050", "0050"]
    assert str(result.available_at.dt.tz) == "UTC"
    assert result.date.dt.tz is None


@pytest.mark.parametrize("mutation,message", [
    (lambda x: pd.concat([x, x.iloc[[0]]]), "duplicate"),
    (lambda x: x.assign(high=1.0), "OHLC"),
    (lambda x: x.assign(adj_close=np.nan), "adj_close"),
    (lambda x: x.assign(volume=-1), "volume"),
    (lambda x: x.assign(close=0.0), "close"),
    (lambda x: x.assign(available_at="2025-01-04 20:00:00"), "timezone"),
])
def test_invalid_prices_fail_loudly(mutation, message):
    from twquant.data import validate_prices
    with pytest.raises(ValueError, match=message):
        validate_prices(mutation(small_prices()))


def test_price_snapshots_are_immutable_and_roundtrip(tmp_path):
    from twquant.data import PriceStore
    store = PriceStore(tmp_path)
    first_id = store.write_prices(small_prices(), {"token": "secret", "nested": {"api_key": "secret"}})
    second = small_prices().assign(adj_close=[30.0, 31.0])
    second_id = store.write_prices(second)
    assert first_id != second_id
    assert store.latest_snapshot_id() == second_id
    assert store.read_prices(first_id).adj_close.tolist() == [25.5, 26.0]
    assert store.read_prices().adj_close.tolist() == [30.0, 31.0]
    assert len(store.list_snapshots()) == 2
    for manifest in tmp_path.glob("snapshots/*/manifest.json"):
        assert "secret" not in manifest.read_text()


def test_snapshot_tampering_is_detected(tmp_path):
    from twquant.data import PriceStore
    store = PriceStore(tmp_path)
    snapshot_id = store.write_prices(small_prices())
    path = tmp_path / "snapshots" / snapshot_id / "prices.parquet"
    with path.open("ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(ValueError, match="checksum"):
        store.read_prices(snapshot_id)


def test_snapshot_ids_cannot_escape_store(tmp_path):
    from twquant.data import PriceStore
    with pytest.raises(ValueError, match="snapshot"):
        PriceStore(tmp_path).read_prices("../../outside")


def test_csv_keeps_leading_zero_and_requires_adjustments(tmp_path):
    from twquant.ingestion import ingest_csv
    path = tmp_path / "input.csv"
    small_prices().to_csv(path, index=False)
    assert ingest_csv(path).ticker.tolist() == ["0050", "0050"]
    small_prices().drop(columns="adj_close").to_csv(path, index=False)
    with pytest.raises(ValueError, match="adj_close"):
        ingest_csv(path)


class FakeResponse:
    status_code = 200
    def __init__(self, data):
        self.payload = {"status": 200, "msg": "success", "data": data}
    def json(self):
        return self.payload


def finmind_raw():
    return [{"date": "2025-01-02", "stock_id": "0050", "open": 50, "max": 52,
             "min": 49, "close": 51, "Trading_Volume": 1000, "Trading_money": 51000}]


def test_finmind_merges_real_raw_and_adjusted_without_leaking_token(monkeypatch, tmp_path):
    from twquant.ingestion import ingest_finmind
    calls = []
    def fake_get(url, **kwargs):
        calls.append(kwargs)
        data = finmind_raw()
        if kwargs["params"]["dataset"] == "TaiwanStockPriceAdj":
            data[0]["close"] = 25.5
        return FakeResponse(data)
    monkeypatch.setattr("requests.get", fake_get)
    prices = ingest_finmind(["0050"], "2025-01-02", "2025-01-03", token="SUPERSECRET", raw_dir=tmp_path)
    assert prices.close.tolist() == [51]
    assert prices.adj_close.tolist() == [25.5]
    assert prices.availability_basis.tolist() == ["reconstructed_eod"]
    assert prices.available_at.iloc[0] == pd.Timestamp("2025-01-02T12:00:00Z")
    assert len(calls) == 2
    assert all("token" not in call["params"] for call in calls)
    assert all("SUPERSECRET" not in p.read_text() for p in tmp_path.glob("*.json"))


def test_finmind_rejects_missing_adjustment_dates(monkeypatch):
    from twquant.ingestion import ingest_finmind
    def fake_get(url, **kwargs):
        data = finmind_raw()
        if kwargs["params"]["dataset"] == "TaiwanStockPriceAdj":
            data[0]["date"] = "2025-01-03"
        return FakeResponse(data)
    monkeypatch.setattr("requests.get", fake_get)
    with pytest.raises(ValueError, match="adjusted|alignment"):
        ingest_finmind(["0050"], "2025-01-02", "2025-01-03")


def test_finmind_rejects_auth_and_rate_limit(monkeypatch):
    from twquant.ingestion import ingest_finmind, IngestionError
    response = FakeResponse([])
    response.payload = {"status": 402, "msg": "Requests reach the upper limit.", "data": []}
    monkeypatch.setattr("requests.get", lambda *a, **k: response)
    with pytest.raises(IngestionError, match="402"):
        ingest_finmind(["0050"], "2025-01-02", "2025-01-03")


def test_demo_is_deterministic_and_explicitly_synthetic():
    from twquant.demo import make_demo_prices
    from twquant.data import validate_prices
    first = make_demo_prices(sessions=30, seed=42)
    pd.testing.assert_frame_equal(first, make_demo_prices(sessions=30, seed=42))
    assert len(first) == 330
    assert set(first.source) == {"synthetic_demo"}
    assert first.date.max() == pd.Timestamp("2025-12-31")
    assert "0050" in set(first.ticker)
    validate_prices(first)


@pytest.mark.parametrize("local_time", ["00:00:00", "08:30:00", "13:29:59"])
def test_full_daily_ohlc_cannot_be_available_before_physical_close(local_time):
    from twquant.data import validate_prices
    prices = small_prices()
    prices.loc[0, "available_at"] = f"2025-01-02T{local_time}+08:00"
    with pytest.raises(ValueError, match="13:30|closing"):
        validate_prices(prices)


def test_full_daily_ohlc_can_be_available_at_physical_close():
    from twquant.data import validate_prices
    prices = small_prices()
    prices.loc[0, "available_at"] = "2025-01-02T13:30:00+08:00"
    result = validate_prices(prices)
    assert result.available_at.iloc[0] == pd.Timestamp("2025-01-02T05:30:00Z")


def test_legacy_finmind_archive_redacts_url_encoded_echo(monkeypatch,tmp_path):
    from urllib.parse import quote
    from twquant.ingestion import ingest_finmind
    token='example-private/token+value'
    def fake_get(url,**kwargs):
        data=finmind_raw()
        response=FakeResponse(data)
        response.payload['echo']=quote(token,safe='')
        return response
    monkeypatch.setattr('requests.get',fake_get)
    ingest_finmind(['0050'],'2025-01-02','2025-01-03',token=token,raw_dir=tmp_path)
    assert all(quote(token,safe='') not in p.read_text() for p in tmp_path.glob('*.json'))


def test_index_may_preserve_unknown_volume_but_equity_volume_remains_required():
    import numpy as np
    from twquant.demo import make_demo_prices
    from twquant.data import validate_prices
    p=make_demo_prices(5).query('ticker == "2330"').copy()
    p['ticker']='IX0001';p['asset_class']='index';p['volume']=np.nan
    assert validate_prices(p).volume.isna().all()
    p['ticker']='2330';p['asset_class']='equity'
    with pytest.raises(ValueError,match='volume'):
        validate_prices(p)
