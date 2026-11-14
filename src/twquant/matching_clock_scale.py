"""Coupled clock changes for synthetic matching paths, never provider history.

Dividing every event rate by s and multiplying the input delay by s preserves
the event sequence and emission state. A separate output deadline is supported
only within the reference path's observed window. No market history is inferred.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .first_change import _labels, _wait
from .trade_first_change import TARGET


def scale_matching_clock(frame, *, slowdown, max_wait_seconds=None):
    """Return an independent synthetic panel with a slower clock.

    Only integer slowdowns of1..1000 with an integral source-clock deadline
    are supported. Unknown reference answers remain unknown. Future-event
    counters retain their reference provenance rather than claiming a replay.
    """
    if type(slowdown) is not int or not 1<=slowdown<=1000:
        raise ValueError('Slowdown requires a positive integer of at most1000')
    required={'source','history_window_unit','max_wait_seconds','event_rate',
        'latency_seconds','prediction_at','max_input_available_at','input_ready',
        'simulation_resolved','first_change_price','first_change_fill_rank',
        'diagnostic_anchor_trade_price'}
    if not isinstance(frame,pd.DataFrame) or not len(frame) or not frame.columns.is_unique or not required<=set(frame):
        raise ValueError('Declare a complete nonempty synthetic matching panel')
    if not frame.source.eq('synthetic_multilevel_matching').all():
        raise ValueError('Clock scaling accepts only synthetic multilevel matching')
    if not frame.history_window_unit.eq('events').all():
        raise ValueError('Clock scaling requires history windows measured in events')
    if frame.max_wait_seconds.nunique()!=1:
        raise ValueError('Declare one matching deadline')
    seconds=int(frame.max_wait_seconds.iloc[0]);_wait(seconds,target_kind=TARGET)
    output_seconds=seconds if max_wait_seconds is None else max_wait_seconds
    _wait(output_seconds,target_kind=TARGET)
    if output_seconds>seconds*slowdown:
        raise ValueError('Output deadline exceeds the covered reference future')
    if not frame.max_wait_seconds.eq(seconds).all() or output_seconds*1_000_000_000%slowdown:
        raise ValueError('Slowdown requires an integral source-clock deadline')
    _labels(frame,seconds,target_kind=TARGET)
    for col in ('input_ready','simulation_resolved','timeout'):
        if not pd.api.types.is_bool_dtype(frame[col]) or frame[col].isna().any():
            raise ValueError('Matching status flags must be complete booleans')
    if frame.duplicated(['ticker','decision_at']).any() or not frame.prediction_at.eq(frame.decision_at).all():
        raise ValueError('Preserve one fixed-clock matching row per stock and date')
    rate=pd.to_numeric(frame.event_rate,errors='coerce').to_numpy(dtype=float)
    delay=pd.to_numeric(frame.latency_seconds,errors='coerce').to_numpy(dtype=float)
    if (not np.isfinite(rate).all() or not np.isfinite(delay).all()
            or (rate<.01).any() or (rate>10000).any() or (delay<0).any() or (delay>5).any()
            or (rate/slowdown<.01).any() or (delay*slowdown>5).any()):
        raise ValueError('Slowed rate or latency exceeds the declared matching bounds')
    ready=frame.input_ready.to_numpy()
    cutoff=frame.decision_at-pd.to_timedelta(delay,unit='s')
    if not frame.loc[ready,'max_input_available_at'].eq(cutoff[ready]).all():
        raise ValueError('Matching cutoff must agree with its declared latency')
    actual=pd.to_numeric(frame.actual_return,errors='coerce').to_numpy(dtype=float)
    known=np.isfinite(actual)
    if np.isinf(actual).any() or not np.array_equal(known,frame.simulation_resolved.to_numpy()):
        raise ValueError('Matching resolution must agree with known answers')
    event=known&(actual!=0)
    if (frame.loc[~known,'first_change_at'].notna().any() or frame.loc[~known,'timeout'].any()):
        raise ValueError('Unresolved matching rows cannot have an event or timeout')
    duration=(frame.first_change_at-frame.decision_at).astype('timedelta64[ns]').array.asi8
    retained=event&(duration<=output_seconds*1_000_000_000//slowdown)
    late=event&~retained
    result=frame.copy(deep=True)
    result['reference_source']=frame.source
    result['reference_first_change_at']=frame.first_change_at
    result['reference_actual_return']=frame.actual_return
    result['reference_event_rate']=frame.event_rate
    result['reference_latency_seconds']=frame.latency_seconds
    result['reference_max_wait_seconds']=seconds
    for col in ('diagnostic_events_applied','diagnostic_book_incomplete'):
        if col in result:result=result.rename(columns={col:col.replace('diagnostic_','reference_',1)})
    result['source']='synthetic_multilevel_matching_clock_scaled'
    result['clock_slowdown']=slowdown
    result['clock_rounding_bound_ns']=slowdown+1
    result['event_rate']=rate/slowdown
    result['latency_seconds']=delay*slowdown
    result['max_wait_seconds']=output_seconds
    result['target_at']=frame.decision_at+pd.Timedelta(seconds=output_seconds)
    result['label_available_at']=result.target_at+(frame.label_available_at-frame.target_at)
    # The generator uses a scalar Timedelta for its single-latency panels.
    # Preserve its timestamp resolution instead of upcasting every cutoff to ns.
    scaled_cutoff=(frame.decision_at-pd.Timedelta(seconds=float(delay[0]*slowdown))
        if np.all(delay==delay[0]) else frame.decision_at-pd.to_timedelta(delay*slowdown,unit='s'))
    result['max_input_available_at']=scaled_cutoff.where(ready)
    elapsed=np.zeros(len(frame),dtype='int64');elapsed[retained]=duration[retained]*slowdown
    result['first_change_at']=(frame.decision_at+pd.to_timedelta(elapsed,unit='ns')).where(retained)
    result.loc[late,'actual_return']=0.
    result['timeout']=frame.timeout.to_numpy()|late
    result.loc[~retained,['first_change_price','first_change_fill_rank']]=np.nan
    _labels(result,output_seconds,target_kind=TARGET)
    return result


def _reference_module_compatible(name, reference, current):
    """Accept byte identity or only the target-specific 300s guard extension.

    All other matching, feature, fitting and prediction syntax must match.
    This is deliberately narrower than accepting a module-name allowlist.
    """
    import ast
    if reference==current:return True
    if name not in ('first_change.py','matching_simulation.py','matching_book.py'):return False
    before=ast.parse(reference);after=ast.parse(current)
    extension=ast.parse("if target_kind=='first_trade_change_before_deadline' and type(seconds) is int and seconds==300:return").body[0]
    for node in ast.walk(after):
        if isinstance(node,ast.FunctionDef) and node.name=='_wait':
            if (len(node.args.kwonlyargs)!=1 or node.args.kwonlyargs[0].arg!='target_kind'
                    or ast.dump(node.args.kw_defaults[0])!=ast.dump(ast.Constant(None))
                    or ast.dump(node.body[0])!=ast.dump(extension)):return False
            node.args.kwonlyargs=[];node.args.kw_defaults=[];node.body=node.body[1:]
        if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='_wait':
            for kw in node.keywords:
                if kw.arg!='target_kind':return False
                if not ((isinstance(kw.value,ast.Name) and kw.value.id=='target_kind')
                        or (isinstance(kw.value,ast.Constant) and kw.value.value==TARGET)):return False
            node.keywords=[]
    return ast.dump(before)==ast.dump(after)


def run_clock_transfer_study(reference, config, output):
    """Fit once per seed on fixed-ten reference rows, then query every world.

    Reference files are fingerprinted before fitting. This is an offline
    synthetic study, with no provider initialization or model selection on
    outside targets. Unknown answers and added timeouts retain full credit loss.
    """
    import hashlib
    import json
    from pathlib import Path
    import re
    import shutil
    from scipy.stats import beta
    from threadpoolctl import threadpool_limits
    from .matching_simulation import MatchingScenario, simulate_matching_panel
    from .trade_first_change import fit_trade_first_model, audit_trade_first_predictions
    from .universe import TRAINING_TICKERS, DEFAULT_EVALUATION_TICKERS, asset_class
    from .workflows import source_digest, write_json

    if not isinstance(config,dict) or not {'slowdowns','training_scenario'}<=set(config)<={'slowdowns','training_scenario','max_wait_seconds'}:
        raise ValueError('Declare slowdowns and one primary training scenario')
    slowdowns=config['slowdowns'];training=config['training_scenario']
    if (not isinstance(slowdowns,list) or not 1<=len(slowdowns)<=12
            or any(type(s) is not int or not 1<=s<=1000 for s in slowdowns)
            or len(set(slowdowns))!=len(slowdowns) or not isinstance(training,str)):
        raise ValueError('Declare distinct bounded slowdowns and a training scenario')
    reference=Path(reference).resolve();output=Path(output)
    if output.exists():raise ValueError('Clock transfer requires a fresh output directory')
    parent=json.loads((reference/'protocol.json').read_text())
    report=json.loads((reference/'report.json').read_text())
    if (parent.get('evidence_kind')!='synthetic_multilevel_matching'
            or parent.get('matching_rule')!='ordered_opposite_levels'
            or parent.get('target_kind')!=TARGET or parent.get('history_window_unit')!='events'):
        raise ValueError('Clock transfer requires a frozen synthetic matching study')
    if hashlib.sha256(json.dumps(parent['config'],sort_keys=True).encode()).hexdigest()!=parent['config_sha256']:
        raise ValueError('Reference configuration fingerprint differs')
    current_source=source_digest();compatible_extensions=[]
    if current_source!=parent['source_sha256']:
        snapshot=reference/'source_snapshot/src/twquant';digest=hashlib.sha256()
        files=sorted(snapshot.glob('*.py'))
        if not files:raise ValueError('Reference source snapshot is required for a different source version')
        for path in files:
            digest.update(path.name.encode());digest.update(path.read_bytes())
            # This auxiliary module never participates in the reference matching generator.
            if path.name!='matching_clock_scale.py':
                current=(Path(__file__).parent/path.name).read_bytes()
                if not _reference_module_compatible(path.name,path.read_bytes(),current):
                    raise ValueError('Reference matching/model implementation differs')
                if path.read_bytes()!=current:compatible_extensions.append(path.name)
        if digest.hexdigest()!=parent['source_sha256']:
            raise ValueError('Reference source fingerprint differs')
    base=parent['config'];seconds=base['max_wait_seconds'];_wait(seconds,target_kind=TARGET)
    output_seconds=config.get('max_wait_seconds',seconds);_wait(output_seconds,target_kind=TARGET)
    members=parent['membership']['tickers'];runs=report['experiments']
    if len(members)!=50 or len(set(members))!=50 or set(parent['training_tickers'])!=set(TRAINING_TICKERS):
        raise ValueError('Clock transfer requires the fixed ten and all50constituents')
    if not runs or training not in {r['scenario']['name'] for r in runs}:
        raise ValueError('Primary training scenario is absent')
    for run in runs:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}',run['run_id']):raise ValueError('Invalid reference run identity')
        path=(reference/run['predictions']).resolve()
        if not path.is_relative_to(reference) or hashlib.sha256(path.read_bytes()).hexdigest()!=run['prediction_sha256']:
            raise ValueError('Reference prediction fingerprint differs')
        scenario=MatchingScenario(**run['scenario'])
        for slowdown in slowdowns:
            rates=[scenario.event_rate,*[v for _,v in scenario.external_event_rates]]
            if output_seconds>seconds*slowdown:
                raise ValueError('Output deadline exceeds the covered reference future')
            if output_seconds*1_000_000_000%slowdown or min(rates)/slowdown<.01 or scenario.latency_seconds*slowdown>5:
                raise ValueError('Slowdown exceeds the declared matching clock bounds')
    canonical={}
    for seed in base['seeds']:
        choices=[r for r in runs if r['seed']==seed and r['scenario']['name']==training]
        if len(choices)!=1:raise ValueError('Declare exactly one primary training run per seed')
        canonical[seed]=choices[0]
    family=len(members)*len(runs)*len(slowdowns);alpha=.05/family
    protocol=dict(evidence_kind='synthetic_matching_clock_transfer',config=config,
        config_sha256=hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest(),
        source_sha256=current_source,reference_source_sha256=parent['source_sha256'],
        reference_protocol_sha256=hashlib.sha256((reference/'protocol.json').read_bytes()).hexdigest(),
        membership=parent['membership'],training_tickers=list(TRAINING_TICKERS),
        evaluation_tickers=parent['evaluation_tickers'],target_kind=TARGET,max_wait_seconds=output_seconds,
        training_max_wait_seconds=seconds,validation_only_300s_extension_modules=compatible_extensions,
        fixed_candidate='spread_bounce',selection='fixed_before_transfer_evaluation',
        model_shared_across_all_contexts_per_seed=True,uncertainty_family=family,
        assumptions=['All event rates divided by slowdown; input latency multiplied by slowdown; emission state and observed event history unchanged.',
            'Output deadline is separately declared and covered by the reference window; later events become timeouts, unresolved answers stay unknown.',
            'First-event timestamp is a rounded upper representation with slowdown+1nanosecond comparison tolerance.',
            'Future event counters retain reference provenance. No actual feed or market calibration is inferred.'],
        market_calibrated=False,empirical_target_met=False,target_feasibility_established=False)
    output.mkdir(parents=True,exist_ok=False);write_json(output/'protocol.json',protocol)
    shutil.copytree(Path(__file__).parent,output/'source_snapshot/src/twquant',ignore=shutil.ignore_patterns('__pycache__'))
    dates=pd.bdate_range('2020-01-02',periods=sum(base[k] for k in ('train_dates','calibration_dates','test_dates')))
    decisions=dates[-base['test_dates']:].tz_localize('Asia/Taipei')+pd.Timedelta(hours=10)
    models={};details={}
    for seed,run in canonical.items():
        train=simulate_matching_panel(TRAINING_TICKERS,dates[:base['train_dates']],
            MatchingScenario(**run['scenario']),seed=seed,future_seed=seed^0x5A5A,
            max_wait_seconds=seconds,max_events=base.get('max_events',2048))
        with threadpool_limits(limits=1):model=fit_trade_first_model(train,'spread_bounce',fit_at=parent['fit_at'])
        if model.training_details!=run['training_details']['spread_bounce']:
            raise ValueError('Primary-only reference fitting differs')
        models[seed]=model;details[str(seed)]=model.training_details
    results=[]
    for run in runs:
        original=pd.read_parquet(reference/run['predictions'])
        if len(original)!=len(parent['evaluation_tickers'])*base['test_dates']:
            raise ValueError('Reference query grid differs')
        model=models[run['seed']]
        for slowdown in slowdowns:
            p=scale_matching_clock(original,slowdown=slowdown,max_wait_seconds=output_seconds)
            p['probability_up']=model.predict_locations(p.f_trade_location.to_numpy(),input_ready=p.input_ready.to_numpy())[:,1]
            p['decision_threshold']=.5;p['model_version']=model.version
            p['max_input_available_at']=p.max_input_available_at.fillna(pd.Timestamp(parent['fit_at']))
            q=p[p.ticker.isin(members)]
            audit=audit_trade_first_predictions(q,members,expected_decisions=decisions,
                max_wait_seconds=output_seconds,evidence_kind='synthetic')
            if audit['prediction_coverage']!=1 or audit['missing_predictions'] or audit['invalid_predictions']:
                raise ValueError('Clock transfer must preserve the complete daily constituent grid')
            lower=[]
            for ticker in members:
                k=round(audit['per_stock'][ticker]['accuracy']*base['test_dates'])
                lower.append(float(beta.ppf(alpha,k,base['test_dates']-k+1)) if k else 0.)
            transfer={}
            for ticker in DEFAULT_EVALUATION_TICKERS:
                t=p[p.ticker.eq(ticker)];known=t.actual_return.notna()
                correct=known&t.actual_return.ne(0)&t.actual_return.gt(0).eq(t.probability_up.ge(.5))
                transfer[ticker]=dict(predictions=len(t),unresolved_answers=int((~known).sum()),
                    accuracy=float(correct.mean()) if known.any() else None,tradable=asset_class(ticker)!='index',
                    used_for_training_calibration_selection=False)
            run_id=f"{run['run_id']}_slow{slowdown}";destination=output/run_id;destination.mkdir()
            compact=p.drop(columns=[c for c in p if c.startswith(('f_book_','f_event_','probability_up_'))
                or c.startswith('f_trade_') and c!='f_trade_location' or c.startswith('observed_')])
            path=destination/'predictions.parquet';compact.to_parquet(path,index=False)
            item=dict(run_id=run_id,reference_run_id=run['run_id'],scenario=run['scenario'],seed=run['seed'],
                slowdown=slowdown,model_version=model.version,training_scenario=training,audit=audit,
                any_five_sample_gate=audit['worst_five_accuracy']>=.75,
                any_five_joint_lower=float(np.mean(sorted(lower)[:5])),minimum_stock_joint_lower=min(lower),
                predictions=str(path.relative_to(output)),prediction_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),transfer=transfer)
            write_json(destination/'report.json',item);results.append(item)
            write_json(output/'progress.json',dict(completed=len(results),last_run=run_id))
    if source_digest()!=current_source:raise RuntimeError('Source changed during clock transfer study')
    result=dict(evidence_kind='synthetic_matching_clock_transfer',experiments=results,models_trained=len(models),
        training_details=details,full_daily_coverage=True,market_calibrated=False,empirical_target_met=False,
        target_feasibility_established=False)
    write_json(output/'report.json',result)
    return result


def main(argv=None):
    import argparse
    import json
    from pathlib import Path
    parser=argparse.ArgumentParser(description='Offline frozen-model synthetic clock transfer')
    parser.add_argument('--study',required=True);parser.add_argument('--config',required=True);parser.add_argument('--out',required=True)
    args=parser.parse_args(argv)
    result=run_clock_transfer_study(args.study,json.loads(Path(args.config).read_text()),args.out)
    print(json.dumps(dict(experiments=len(result['experiments']),models_trained=result['models_trained'],
        full_daily_coverage=result['full_daily_coverage'],empirical_target_met=False)))
    return 0


if __name__=='__main__':raise SystemExit(main())
