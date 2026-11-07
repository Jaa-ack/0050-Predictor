"""Offline official-history parsing and explicitly reconstructed preopen context.

Trade dates are volume-attribution dates. Neither an archive nor these assumed
06:00 receipts establishes original point-in-time delivery before 08:30.
"""
from __future__ import annotations

import csv
from datetime import datetime
import hashlib
import io
import re
import zipfile

import numpy as np
import pandas as pd


CONTRACTS = ('TX', 'TE', 'TF')
HISTORY_COLUMNS = ['trade_date', 'contract', 'expiry', 'session', 'open', 'high', 'low', 'close', 'volume']
EVENT_COLUMNS = ['ticker', 'feature', 'value', 'published_at', 'available_at', 'expires_at', 'source',
                 'availability_basis', 'original_pit_verified', 'trade_date', 'reference_date', 'contract', 'expiry']
FIELDS = {'交易日期':'trade_date', '契約':'contract', '到期月份(週別)':'expiry', '交易時段':'session',
          '開盤價':'open', '最高價':'high', '最低價':'low', '收盤價':'close', '成交量':'volume'}
AVAILABILITY_BASIS = 'reconstructed_taifex_trade_date_0600_no_original_receipt'


def _number(value, *, volume=False):
    if value in {'', '-', '--'}:
        return np.nan
    try:
        result = float(value)
    except ValueError:
        raise ValueError('Invalid TAIFEX numeric field') from None
    if not np.isfinite(result) or result < 0:
        raise ValueError('Invalid TAIFEX numeric field')
    return result if volume or result > 0 else np.nan


def parse_taifex_history(content: bytes) -> pd.DataFrame:
    """Normalize one CSV or single-member ZIP, retaining only monthly TX/TE/TF.

    Only OHLC/volume fields survive. Settlement, open interest and historical
    extrema never enter the normalized history. Empty optional legacy tails,
    one empty extra CSV cell and a terminal DOS EOF marker are recognized;
    ambiguous rows or duplicate keys fail rather than inventing observations.
    """
    fingerprint = hashlib.sha256(content).hexdigest()
    if len(content) > 500_000_000:
        raise ValueError('TAIFEX history exceeds size bound')
    if content.startswith(b'PK'):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                members = archive.infolist()
                if len(members) != 1 or members[0].flag_bits & 1 or members[0].file_size > 500_000_000:
                    raise ValueError('TAIFEX history requires one bounded unencrypted CSV')
                content = archive.read(members[0])
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError):
            raise ValueError('Invalid TAIFEX history archive') from None
    for encoding in ('utf-8-sig', 'cp950'):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError('Unsupported TAIFEX history encoding')
    reader = csv.reader(io.StringIO(text))
    header = next(reader, [])
    if not set(FIELDS).issubset(header) or len(header) != len(set(header)):
        raise ValueError('TAIFEX history schema mismatch')
    rows, eof = [], False
    for cells in reader:
        if not cells or all(not value.strip() for value in cells):
            continue
        if cells == ['\x1a']:
            eof = True
            continue
        if eof:
            raise ValueError('TAIFEX data follows EOF marker')
        if len(cells) == len(header)-1 and header[-1] == '價差對單式委託成交量':
            cells.append('')
        if len(cells) == len(header)+1 and cells[-1] == '':
            cells = cells[:-1]
        if len(cells) != len(header):
            raise ValueError('TAIFEX history row width mismatch')
        record = {key:value.strip() for key, value in zip(header, cells)}
        if record['契約'] not in CONTRACTS or not re.fullmatch(r'\d{6}', record['到期月份(週別)']):
            continue
        try:
            date = datetime.strptime(record['交易日期'], '%Y/%m/%d')
            datetime.strptime(record['到期月份(週別)'], '%Y%m')
        except ValueError:
            raise ValueError('Invalid TAIFEX daily date or expiry month') from None
        if record['交易時段'] not in {'一般', '盤後'}:
            raise ValueError('Unknown TAIFEX trading session')
        row = dict(trade_date=date, contract=record['契約'], expiry=record['到期月份(週別)'],
                   session='regular' if record['交易時段'] == '一般' else 'night')
        row.update({english:_number(record[chinese], volume=english == 'volume')
                    for chinese, english in FIELDS.items() if english in {'open','high','low','close','volume'}})
        if ((np.isfinite(row['high']) and any(np.isfinite(row[c]) and row[c] > row['high'] for c in ('open','low','close')))
            or (np.isfinite(row['low']) and any(np.isfinite(row[c]) and row[c] < row['low'] for c in ('open','close')))):
            raise ValueError('Invalid TAIFEX OHLC range')
        rows.append(row)
    result = pd.DataFrame(rows, columns=HISTORY_COLUMNS)
    result['trade_date'] = pd.to_datetime(result.trade_date)
    if result.duplicated(['trade_date','contract','expiry','session']).any():
        raise ValueError('Duplicate TAIFEX contract/session observation')
    result = result.sort_values(['trade_date','contract','expiry','session']).reset_index(drop=True)
    result.attrs.update(input_sha256=fingerprint, original_pit_verified=False)
    return result


def taifex_night_events(history: pd.DataFrame) -> pd.DataFrame:
    """Pair nearest observed night contracts with strictly earlier same-contract closes.

    05:00 publication / 06:00 receipt / inclusive 08:30 expiry are reconstructed
    assumptions on the attribution date, not observed publication timestamps.
    No current-day regular session or cash-index target is used. Missing front
    references stay unknown rather than switching to a more convenient expiry.
    """
    if not set(HISTORY_COLUMNS).issubset(history.columns):
        raise ValueError('Use normalized TAIFEX history')
    if history.duplicated(['trade_date','contract','expiry','session']).any():
        raise ValueError('Duplicate TAIFEX contract/session observation')
    known = history.close.gt(0) & history.volume.gt(0)
    regular = history.loc[history.session.eq('regular') & known,
                          ['trade_date','contract','expiry','close','volume']].rename(
        columns={'trade_date':'reference_date','close':'reference_close','volume':'reference_volume'})
    night = history.loc[history.session.eq('night') & known].copy()
    night = night.loc[night.expiry.ge(night.trade_date.dt.strftime('%Y%m'))]
    night = night.sort_values(['trade_date','contract','expiry']).drop_duplicates(['trade_date','contract'])
    if night.empty or regular.empty:
        return pd.DataFrame(columns=EVENT_COLUMNS)
    paired = pd.merge_asof(night.sort_values('trade_date'), regular.sort_values('reference_date'),
        left_on='trade_date', right_on='reference_date', by=['contract','expiry'],
        direction='backward', allow_exact_matches=False)
    records = []
    for row in paired.itertuples(index=False):
        if pd.isna(row.reference_close):
            continue
        day = row.trade_date.tz_localize('Asia/Taipei')
        values = dict(night_return=row.close / row.reference_close - 1,
                      night_range=(row.high - row.low) / row.reference_close,
                      night_volume_ratio=row.volume / row.reference_volume)
        for feature, value in values.items():
            if not np.isfinite(value):
                continue
            records.append(dict(ticker='*', feature=f'taifex_{row.contract.lower()}_{feature}', value=float(value),
                published_at=(day + pd.Timedelta(hours=5)).tz_convert('UTC'),
                available_at=(day + pd.Timedelta(hours=6)).tz_convert('UTC'),
                expires_at=(day + pd.Timedelta(hours=8, minutes=30)).tz_convert('UTC'),
                source='taifex_historical_report', availability_basis=AVAILABILITY_BASIS, original_pit_verified=False,
                trade_date=row.trade_date, reference_date=row.reference_date, contract=row.contract, expiry=row.expiry))
    return pd.DataFrame(records, columns=EVENT_COLUMNS).sort_values(['available_at','feature']).reset_index(drop=True)
