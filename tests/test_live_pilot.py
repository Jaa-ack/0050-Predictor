import importlib.util
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from twquant.universe import TRAINING_TICKERS, DEFAULT_EVALUATION_TICKERS


def module():
    assert importlib.util.find_spec('twquant.live_pilot'), 'Real-data pilot is missing'
    from twquant import live_pilot
    return live_pilot


def protocol():
    return dict(dates=['2026-11-16','2026-11-17'], members=list(TRAINING_TICKERS)+[str(4000+i) for i in range(40)],
        horizons=[60,300], prediction_time='10:00:00', max_input_age_seconds=60,
        max_emission_lateness_seconds=1, fallback_probability_up=.5,
        model_version='fixed-shared-bounce-v1', source='fubon',
        label_scope='provider_published_trade_first_change', training_tickers=list(TRAINING_TICKERS))


def stamp(seconds=0):
    return datetime(2026,11,16,10,tzinfo=ZoneInfo('Asia/Taipei'))+timedelta(seconds=seconds)


def message(channel, seconds, *, ticker='2330', price=100, kind='data', serial=1):
    data=dict(symbol=ticker,time=int(stamp(seconds).timestamp()*1e6),isContinuous=True)
    if channel=='trades':data.update(price=price,size=1,serial=serial)
    else:data.update(bids=[dict(price=100,size=20)],asks=[dict(price=101,size=30)])
    return dict(event=kind,channel=channel,data=data)


def seeded(tmp_path):
    p=module().PilotStore(tmp_path,protocol())
    p.ingest(message('books',-2),received_at=stamp(-1))
    p.ingest(message('trades',-2,price=100),received_at=stamp(-1))
    return p


def test_forecast_is_causal_shared_complete_and_immutable(tmp_path):
    p=seeded(tmp_path)
    p.ingest(message('books',-.1),received_at=stamp(1))
    rows=p.emit(stamp())
    assert len(rows)==len(set(protocol()['members'])|set(DEFAULT_EVALUATION_TICKERS))
    assert len({r['model_version'] for r in rows})==1
    stock=next(r for r in rows if r['ticker']=='2330')
    assert stock['direction']==1 and stock['input_ready']
    assert stock['max_input_received_at']==stamp(-1).isoformat()
    assert next(r for r in rows if r['ticker']=='4000')['fallback']
    assert p.emit(stamp(.5))==rows


def test_first_change_targets_separate_keep_timeouts_and_missing_days(tmp_path):
    p=seeded(tmp_path);p.emit(stamp())
    p.ingest(message('trades',20,price=100,serial=2),received_at=stamp(21))
    p.ingest(message('trades',120,price=101,serial=3),received_at=stamp(121))
    p.mark_coverage(stamp(-60),stamp(360))
    r=p.evaluate(as_of=stamp(400)+timedelta(days=1))
    assert r['horizons']['60']['expected_predictions']==100
    assert r['horizons']['60']['per_stock']['2330']['correct']==0
    assert r['horizons']['300']['per_stock']['2330']['correct']==1
    assert r['horizons']['60']['missing_predictions']==50
    assert r['horizons']['300']['missing_predictions']==50
    assert not r['empirical_target_met']


def test_stream_gap_makes_first_answer_unknown_instead_of_timeout_or_credit(tmp_path):
    p=seeded(tmp_path);p.emit(stamp())
    p.mark_coverage(stamp(-60),stamp(360));p.mark_gap(stamp(1),stamp(70),'disconnected')
    p.ingest(message('trades',120,price=101,serial=3),received_at=stamp(121))
    r=p.evaluate(as_of=stamp(400)+timedelta(days=1))
    assert r['horizons']['300']['per_stock']['2330']['correct']==0
    assert r['horizons']['300']['per_stock']['2330']['unknown']==2


def test_late_emit_and_snapshot_cannot_manufacture_prospective_success(tmp_path):
    p=seeded(tmp_path);rows=p.emit(stamp(2))
    assert not any(r['on_time'] for r in rows)
    p.ingest(message('trades',20,price=101,kind='snapshot',serial=2),received_at=stamp(21))
    p.mark_coverage(stamp(-60),stamp(360))
    r=p.evaluate(as_of=stamp(400))
    assert r['horizons']['300']['per_stock']['2330']['correct']==0


def test_collector_does_not_store_authentication_or_arbitrary_fields(tmp_path):
    p=seeded(tmp_path)
    assert not p.ingest(dict(event='authenticated',data={'token':'do-not-store'}),received_at=stamp())
    m=message('trades',1,serial=2);m['data']['token']='do-not-store'
    p.ingest(m,received_at=stamp(2));p.close()
    assert b'do-not-store' not in (tmp_path/'pilot.sqlite3').read_bytes()


def test_restart_preserves_predictions_and_deduplicates_trade_events(tmp_path):
    p=seeded(tmp_path);rows=p.emit(stamp());m=message('trades',20,price=101,serial=2)
    assert p.ingest(m,received_at=stamp(21));assert not p.ingest(m,received_at=stamp(22));p.close()
    p=module().PilotStore(tmp_path,protocol());assert p.emit(stamp(.5))==rows
    with pytest.raises(ValueError,match='protocol'):
        module().PilotStore(tmp_path,dict(protocol(),fallback_probability_up=.6))


def test_final_report_runs_only_after_last_day_and_counts_all_fifty(tmp_path):
    p=seeded(tmp_path)
    with pytest.raises(ValueError,match='finished|complete'):
        p.write_report(as_of=stamp(400),final=True)
    result=p.write_report(as_of=stamp(400)+timedelta(days=1),final=True)
    assert result['horizons']['300']['expected_predictions']==100
    assert (tmp_path/'evaluation.md').exists()
    assert not result['statistical_target_established']


def test_durable_release_seal_is_required_and_cannot_be_backdated(tmp_path):
    p=module().PilotStore(tmp_path,dict(protocol(),require_release_seal=True))
    p.ingest(message('books',-2),received_at=stamp(-1))
    p.ingest(message('trades',-2),received_at=stamp(-1));p.emit(stamp())
    p.ingest(message('trades',20,price=101,serial=2),received_at=stamp(21))
    p.mark_coverage(stamp(-60),stamp(360))
    assert p.evaluate(as_of=stamp(400))['horizons']['300']['per_stock']['2330']['correct']==0
    p.seal('2026-11-16',stamp(.1))
    assert p.evaluate(as_of=stamp(400))['horizons']['300']['per_stock']['2330']['correct']==1
    with pytest.raises(ValueError):p.seal('2026-11-16',stamp())


def test_report_contains_daily_baselines_and_observed_latency(tmp_path):
    p=seeded(tmp_path);p.emit(stamp());p.mark_coverage(stamp(-60),stamp(360))
    p.ingest(message('trades',20,price=101,serial=2),received_at=stamp(21))
    r=p.evaluate(as_of=stamp(400))
    assert r['horizons']['300']['per_stock']['2330']['always_up_correct']==1
    assert r['horizons']['300']['per_day']['2026-11-16']['expected']==50
    assert r['data_quality']['received_events']==3
    assert r['data_quality']['median_event_to_receipt_seconds']==1


def test_index_transfer_never_gets_a_fabricated_transaction_answer(tmp_path):
    p=seeded(tmp_path);p.emit(stamp())
    r=p.evaluate(as_of=stamp(400))
    index=r['horizons']['300']['transfers']['IX0001']
    assert index['accuracy'] is None and not index['tradable']
    assert not index['used_for_training']


def test_pilot_rejects_a_different_runtime_implementation(tmp_path):
    with pytest.raises(ValueError,match='source|implementation'):
        module().PilotStore(tmp_path,dict(protocol(),source_sha256='0'*64))
