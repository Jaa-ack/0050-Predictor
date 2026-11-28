"""Compare opening-trial and multisource directions on stock-days with overnight issuer events."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import re

import pandas as pd

from twquant import opening_gap as gap
from twquant.direction_audit import load_membership_snapshot
from twquant.public_sources import _date, normalize_announcements
from twquant.source_store import SourceStore
from twquant.universe import TRAINING_TICKERS

GROUPS = ('disclosure', 'financial', 'conference', 'headline')
DISCLOSURES = 'https://openapi.twse.com.tw/v1/opendata/t187ap04_L'
EVENTS = 'https://www.twse.com.tw/rwd/zh/IIH/market/events'


def title_issuer(title, aliases):
    hits = set()
    for ticker, names in aliases.items():
        for name in names:
            if not name:
                raise ValueError('Missing issuer name')
            pattern = re.escape(name)
            if name.isascii():
                pattern = r'(?<![A-Za-z0-9])' + pattern + r'(?![A-Za-z0-9])'
            if re.search(pattern, title, flags=re.IGNORECASE):
                hits.add(ticker)
                break
    return hits.pop() if len(hits) == 1 else None


def available(records, start, due, *, strict):
    result = defaultdict(set)
    for ticker, category, published, received in records:
        if start < published < due and published <= received and (not strict or received < due):
            result[category].add(ticker)
    return dict(result)


def inputs(stores, files, aliases):
    records, coverage, fingerprints = [], [], []
    def append(ticker, category, pub, recv):
        if ticker not in aliases:
            return
        published, received = pd.Timestamp(pub), pd.Timestamp(recv)
        if published.tzinfo is None or received.tzinfo is None:
            raise ValueError('Timezone-aware publication and receipt required')
        records.append((ticker, category, published, received))

    def disclosure(rows, received):
        for row in rows:
            ticker, = row['ticker_ids']
            append(ticker, 'disclosure', row['published_at'], received)

    for root in stores:
        store = SourceStore(root)
        for source in store.manifests():
            if (source['provider'], source['dataset']) not in {('twse','announcements'), ('newsapi','everything'),
                                                             ('cna','finance'), ('cna','technology')}:
                continue
            doc = store.read(source['snapshot_id'])
            fingerprints.append(dict(provider=doc.provider, dataset=doc.dataset, snapshot=source['snapshot_id'],
                                     sha256=source['sha256'], received_at=doc.retrieved_at))
            if doc.provider == 'twse':
                disclosure(doc.records, doc.retrieved_at)
            else:
                if doc.provider == 'newsapi':
                    coverage.append(dict(snapshot=source['snapshot_id'], retrieved=doc.metadata['received'],
                                         total=doc.metadata['total_results'], complete=doc.metadata['complete']))
                for row in doc.records:
                    ticker = title_issuer(row['title'] or '', aliases)
                    if pd.Timestamp(row['published_at']) <= pd.Timestamp(doc.retrieved_at):
                        append(ticker, 'headline', row['published_at'], row['available_at'])
    for path in files:
        meta_path = path.with_suffix('.meta.json')
        if not meta_path.exists():
            meta_path = path.with_name(path.name+'.meta.json')
        meta = json.loads(meta_path.read_text())
        if gap._sha(path) != meta['sha256']:
            raise ValueError('Official snapshot fingerprint mismatch')
        payload = json.loads(path.read_bytes())
        received = meta['received_at']
        fingerprints.append(dict(url=meta['url'], sha256=meta['sha256'], received_at=received))
        if meta['url'] == DISCLOSURES:
            disclosure(normalize_announcements(payload, received).records, received)
        elif meta['url'] == EVENTS:
            for key, category in [('news','disclosure'), ('fina','financial'), ('conference','conference')]:
                for row in payload[key]:
                    published = pd.Timestamp(_date(row['date'])+' '+row['time'], tz=gap.LOCAL)
                    append(row['companyId'], category, published, received)
        else:
            raise ValueError('Unrecognized official snapshot URL')
    return records, coverage, fingerprints


def policies(frame, model):
    base = frame[model].astype(float)
    trial = frame.direct_trial.astype(float)
    usable = frame.trial_fallback.eq(False) & trial.notna()
    arms = {'multisource':base, 'direct':trial}
    masks = {k:frame[k] for k in GROUPS}
    masks.update(any_event=frame[list(GROUPS)].any(axis=1),
                 official=frame[['disclosure','financial','conference']].any(axis=1))
    for key, mask in masks.items():
        arms[key] = base.mask(mask & usable, trial)
    return arms, masks


def same_count(frame, model, mask):
    # Place the same number of switches among eligible stocks on the same day and in the same training role.
    known = frame.actual_return.notna()
    actual = frame.actual_return.gt(0)
    baseline = frame[model]
    base_hit = known & baseline.between(0, 1) & baseline.ge(.5).eq(actual)
    trial_hit = known & frame.direct_trial.eq(actual)
    rows = frame.assign(switched=mask, core=frame.ticker.isin(TRAINING_TICKERS),
                        rescued=trial_hit & ~base_hit, wrong=base_hit & ~trial_hit)
    eligible = rows.loc[rows.trial_fallback.eq(False) & rows.direct_trial.notna()]
    expected_rescued = expected_wrong = 0.
    for _, part in eligible.groupby(['date','core']):
        count = int(part.switched.sum())
        expected_rescued += count * part.rescued.mean()
        expected_wrong += count * part.wrong.mean()
    chosen = eligible.loc[eligible.switched]
    return dict(eligible_switched=len(chosen), observed_net=int(chosen.rescued.sum()-chosen.wrong.sum()),
                expected_rescued=float(expected_rescued), expected_added_errors=float(expected_wrong))


def compare(frame, model, records, days, symbols, next_day):
    if (len(days) < 2 or sorted(set(days)) != days or next_day <= days[-1]
            or set(frame.date) - {str(d.date()) for d in days[1:]}):
        raise ValueError('Unexpected calibration or scoring dates')
    index = pd.MultiIndex.from_product([[str(d.date()) for d in days[1:]], symbols], names=['date','ticker'])
    data = frame.set_index(['date','ticker'])
    if not data.index.is_unique:
        raise ValueError('Duplicate evaluation rows')
    data = data.reindex(index).reset_index()
    reports = {}
    for mode in ('actual_receipts','publication_replay'):
        rows = data.copy()
        for key in GROUPS:
            rows[key] = False
        by_day = {}
        for prior, day in zip(days, [*days[1:],next_day], strict=True):
            opened = prior.tz_localize(gap.LOCAL) + pd.Timedelta(hours=13, minutes=30)
            due = day.tz_localize(gap.LOCAL) + gap.DECISION
            current = available(records, opened, due, strict=(mode == 'actual_receipts'))
            by_day[str(day.date())] = {key:sorted(current.get(key, set())) for key in GROUPS}
            for key, names in current.items():
                rows.loc[rows.date.eq(str(day.date())) & rows.ticker.isin(names), key] = True
        scopes = {}
        for name, universe in [('fixed_ten',TRAINING_TICKERS), ('constituents',symbols)]:
            subset = rows.loc[rows.ticker.isin(universe)]
            arms, masks = policies(subset, model)
            scopes[name] = {}
            for arm, p in arms.items():
                f = subset.assign(probability_up=p)
                scopes[name][arm] = dict(gap._metrics(f, universe), paired_to_direct=gap._paired_hits(f, arms['direct']),
                                          paired_to_multisource=gap._paired_hits(f, arms['multisource']))
                if arm in masks:
                    scopes[name][arm]['same_count_control'] = same_count(subset, model, masks[arm])
        reports[mode] = dict(observed_issuers=by_day, scopes=scopes)
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('predictions','membership','aliases'):
        parser.add_argument('--'+key, required=True, type=Path)
    parser.add_argument('--stores', nargs='*', type=Path, default=[])
    parser.add_argument('--official', nargs='*', type=Path, default=[])
    parser.add_argument('--previous', type=pd.Timestamp, required=True)
    parser.add_argument('--next-day', type=pd.Timestamp, required=True)
    args = parser.parse_args()
    report = json.loads(args.predictions.with_name('report.json').read_text())
    if (report['evidence_kind'] != 'historical_replay' or tuple(report['training_tickers']) != tuple(TRAINING_TICKERS)
            or gap._sha(args.predictions) != report['artifacts']['predictions.parquet']):
        parser.error('Expected the pinned fixed-ten capture-model replay')
    symbols = load_membership_snapshot(args.membership)['tickers']
    aliases = json.loads(args.aliases.read_text())
    if set(symbols) != set(aliases) or len(symbols) != 50:
        parser.error('Expected issuer aliases for the same fifty constituents')
    raw = pd.read_parquet(args.predictions)
    rows = raw.loc[raw.clock_mode.eq('actual_receipts')]
    records, coverage, fingerprints = inputs(args.stores,args.official,aliases)
    dates = [args.previous, *map(pd.Timestamp, report['sessions'])]
    print(json.dumps(dict(evidence_kind='historical_receipt_replay', timely_issued_sessions=0, news_queries=coverage,
                          sources=fingerprints, aliases_sha256=gap._sha(args.aliases),
                          predictions_sha256=report['artifacts']['predictions.parquet'],
                          multisource=report['selected_backup'], result=compare(
                              rows, report['selected_backup'], records, dates, symbols, args.next_day)), allow_nan=False))


if __name__ == '__main__':
    main()
