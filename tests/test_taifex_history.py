import csv
import io
import zipfile

import numpy as np
import pandas as pd
import pytest


HEADER = ['交易日期', '契約', '到期月份(週別)', '開盤價', '最高價', '最低價', '收盤價', '漲跌價', '漲跌%',
          '成交量', '結算價', '未沖銷契約數', '最後最佳買價', '最後最佳賣價', '歷史最高價', '歷史最低價',
          '是否因訊息面暫停交易', '交易時段', '價差對單式委託成交量']


def record(date='2024/01/16', code='TX', expiry='202401', session='一般', close=100., volume=1000., **extra):
    row = dict.fromkeys(HEADER, '999999')
    row.update({'交易日期':date, '契約':code, '到期月份(週別)':expiry, '交易時段':session,
                '開盤價':close, '最高價':close+1, '最低價':close-1, '收盤價':close, '成交量':volume})
    row.update(extra)
    return row


def csv_bytes(rows, *, encoding='utf-8-sig', trailing=False, optional_missing=False, header=HEADER):
    out = io.StringIO(newline='')
    writer = csv.writer(out)
    writer.writerow(header)
    for row in rows:
        cells = [row[k] for k in header]
        if optional_missing:
            cells = cells[:-1]
        elif trailing:
            cells.append('')
        writer.writerow(cells)
    out.write('\x1a')
    return out.getvalue().encode(encoding)


@pytest.mark.parametrize('encoding,archive,trailing,optional_missing', [
    ('cp950', False, True, False), ('utf-8-sig', False, False, True), ('cp950', True, True, False),
])
def test_history_parser_reads_official_encodings_zip_and_legacy_empty_fields_without_future_columns(encoding, archive, trailing, optional_missing):
    from twquant.taifex_history import parse_taifex_history
    rows = [record(date='2024/1/16'), record(expiry='202401/202402', close=-10), record(code='OTHER', close=-10)]
    content = csv_bytes(rows, encoding=encoding, trailing=trailing, optional_missing=optional_missing)
    if archive:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('Futures.csv', content)
        content = buffer.getvalue()
    out = parse_taifex_history(content)
    assert list(out.columns) == ['trade_date', 'contract', 'expiry', 'session', 'open', 'high', 'low', 'close', 'volume']
    assert len(out) == 1
    assert out.trade_date.iloc[0] == pd.Timestamp('2024-01-16')
    assert out.session.iloc[0] == 'regular'
    assert out.close.iloc[0] == 100.
    assert not any('settlement' in c or 'interest' in c or 'historic' in c for c in out)


@pytest.mark.parametrize('problem', ['extra_value', 'missing_essential', 'duplicate', 'bad_date',
                                     'bad_session', 'nonfinite_price', 'negative_volume', 'zip_many'])
def test_history_parser_rejects_ambiguous_or_invalid_records(problem):
    from twquant.taifex_history import parse_taifex_history
    row = record()
    header = HEADER
    if problem == 'bad_date': row['交易日期'] = '2024/13/01'
    if problem == 'bad_session': row['交易時段'] = 'unknown'
    if problem == 'nonfinite_price': row['收盤價'] = 'inf'
    if problem == 'negative_volume': row['成交量'] = -1
    if problem == 'missing_essential': header = [c for c in HEADER if c != '收盤價']
    content = csv_bytes([row, row] if problem == 'duplicate' else [row], header=header)
    if problem == 'extra_value': content = content.replace(b'\r\n\x1a', b',123\r\n\x1a')
    if problem == 'zip_many':
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as z:
            z.writestr('a.csv', content); z.writestr('b.csv', content)
        content = buffer.getvalue()
    with pytest.raises(ValueError): parse_taifex_history(content)


def night_history():
    from twquant.taifex_history import parse_taifex_history
    rows = [record(expiry='202401'), record(expiry='202402', close=200),
            record(date='2024/01/17', session='盤後', close=105, volume=400),
            record(date='2024/01/17', expiry='202402', session='盤後', close=204, volume=200),
            record(date='2024/01/17', close=110), record(date='2024/01/17', expiry='202402', close=200),
            record(date='2024/01/18', expiry='202402', session='盤後', close=202, volume=500)]
    return parse_taifex_history(csv_bytes(rows))


def test_night_events_pair_strictly_earlier_same_contract_quotes_and_do_not_jump_at_roll():
    from twquant.taifex_history import taifex_night_events
    events = taifex_night_events(night_history())
    returns = events.loc[events.feature.eq('taifex_tx_night_return')].sort_values('available_at')
    np.testing.assert_allclose(returns.value, [.05, .01])
    volumes = events.loc[events.feature.eq('taifex_tx_night_volume_ratio')].sort_values('available_at')
    np.testing.assert_allclose(volumes.value, [.4, .5])
    assert len(events) == 6
    assert events.ticker.eq('*').all()
    assert set(events.feature) == {'taifex_tx_night_return', 'taifex_tx_night_range', 'taifex_tx_night_volume_ratio'}
    assert events.available_at.dt.tz_convert('Asia/Taipei').dt.hour.eq(6).all()
    local_expiry = events.expires_at.dt.tz_convert('Asia/Taipei')
    assert (local_expiry.dt.hour.eq(8) & local_expiry.dt.minute.eq(30)).all()
    assert events.availability_basis.eq('reconstructed_taifex_trade_date_0600_no_original_receipt').all()
    assert events.original_pit_verified.eq(False).all()


def test_night_context_ignores_same_day_regular_outcomes_expires_and_is_scale_free():
    from twquant.taifex_history import taifex_night_events
    from twquant.features import point_in_time_join
    history = night_history()
    first = taifex_night_events(history)
    altered = history.copy()
    changed = altered.trade_date.ge(pd.Timestamp('2024-01-17')) & altered.session.eq('regular')
    altered.loc[changed, ['open', 'high', 'low', 'close', 'volume']] *= 100
    early = first.available_at.le(pd.Timestamp('2024-01-17T00:30Z'))
    a = first.loc[early].reset_index(drop=True)
    b = taifex_night_events(altered).loc[lambda f:f.available_at.le(pd.Timestamp('2024-01-17T00:30Z'))].reset_index(drop=True)
    pd.testing.assert_frame_equal(a, b)
    scaled = history.copy(); scaled[['open','high','low','close']] *= 100
    np.testing.assert_allclose(first.value, taifex_night_events(scaled).value)
    decisions = pd.DataFrame(dict(ticker=['2330','1216','2330'], decision_ts=pd.to_datetime([
        '2024-01-17T00:30Z','2024-01-17T00:30Z','2024-01-17T00:30:00.000001Z'],format='mixed')))
    joined = point_in_time_join(decisions, first)
    np.testing.assert_allclose(joined.f_ext_taifex_tx_night_return, [.05,.05,np.nan],equal_nan=True)


def test_missing_reference_never_uses_same_day_regular_close_or_a_different_contract():
    from twquant.taifex_history import parse_taifex_history, taifex_night_events
    rows = [record(date='2024/01/17'), record(date='2024/01/17',session='盤後',close=105),
            record(expiry='202402',close=200), record(date='2024/01/17',expiry='202402',session='盤後',close=205)]
    assert taifex_night_events(parse_taifex_history(csv_bytes(rows))).empty
