"""Verified local reference models and calibration scores for appended candidates.

Only trusted, locally generated research artifacts may be pinned or loaded.
Hashes bind the complete original frame, recipe, environment and evidence.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import inspect
import json
from pathlib import Path
import platform
from importlib.metadata import version

import joblib
import numpy as np
import pandas as pd
from threadpoolctl import threadpool_limits

from .comparison import reserve_development
from .features import get_feature_columns,select_feature_columns
from .research import _numeric,_positive_probability,_validate,temporal_splits
from .universe import TRAINING_TICKERS
from .workflows import source_digest,write_json


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _plain(value):
    return json.loads(json.dumps(value))


def _frame_sha(frame):
    schema=dict(columns=list(frame),dtypes=[str(frame[c].dtype) for c in frame])
    return hashlib.sha256(json.dumps(schema,sort_keys=True).encode()
        +pd.util.hash_pandas_object(frame,index=False).to_numpy().tobytes()).hexdigest()


def _recipe_signature():
    from .direction import _calibration_metrics,_selection_key
    files=['direction_models.py','direction_similarity.py','direction_fusion.py','research.py','comparison.py','universe.py']
    return dict(recipe_files={name:_sha(Path(__file__).parent/name) for name in files},
        recipe_functions={f.__name__:hashlib.sha256(inspect.getsource(f).encode()).hexdigest() for f in [_calibration_metrics,_selection_key]},
        environment=dict(python=platform.python_version(),packages={name:version(name) for name in ['numpy','pandas','scipy','scikit-learn','joblib','threadpoolctl']}))


def _check_recipe(recipe,root):
    signature=_recipe_signature()
    if any(recipe.get(k)!=v for k,v in signature.items()):
        raise ValueError('Reference recipe or numerical environment changed')
    for name,digest in recipe['evidence_files'].items():
        path=_local_file(root,name)
        if _sha(path)!=digest:raise ValueError('Reference evidence checksum mismatch')


def _local_file(root,name):
    path=(Path(root)/name).resolve()
    if not path.is_relative_to(Path(root).resolve()) or not path.is_file():
        raise ValueError('Reference artifact requires a trusted local file')
    return path


def _make_recipe(frame,root,report):
    profiles=sorted({c['feature_profile'] for c in report['protocol']['candidates']})
    evidence=['report.json','protocol.json','predictions.parquet','transfer_predictions.parquet',
              'constituent_predictions.parquet','membership_snapshot.json','data_provenance.json']
    contracts={k:v for k,v in frame.attrs.items() if k in {
        'flow_receipt_contract','margin_receipt_contract','opening_proxy_contract','sector_response_contract'}}
    return dict(schema_version=1,origin_source_sha256=report['protocol']['source_sha256'],
        **_recipe_signature(),base_columns=list(frame),base_dtypes=[str(frame[c].dtype) for c in frame],
        base_frame_sha256=_frame_sha(frame),base_attrs=_plain(contracts),
        feature_profiles={p:select_feature_columns(frame,p) for p in profiles},
        evidence_files={name:_sha(root/name) for name in evidence if (root/name).exists()})


def pin_direction_reference(frame,config,reference_run):
    """Reconstruct each selected model and verify every stored probability.

    This never selects on outer outcomes. It verifies the original development
    matrix, mature splits, calibration statistics, and all test/transfer outputs
    before creating private model artifacts. Existing pins are immutable.
    """
    from .direction import DirectionCandidate,_calibration_metrics
    from .direction_models import fit_direction_model
    root=Path(reference_run);destination=root/'frozen_reference'
    if (destination/'manifest.json').exists():raise ValueError('Reference already pinned')
    if destination.exists() and any(destination.iterdir()):raise ValueError('Reference partial pin requires inspection')
    report=json.loads((root/'report.json').read_text());protocol=report['protocol']
    if _plain(asdict(config))!=protocol['config']:raise ValueError('Reference configuration mismatch')
    development,start=reserve_development(frame,config.horizons,protocol['holdout_dates'])
    fixed=_validate(development,get_feature_columns(development),config)
    digest=hashlib.sha256(pd.util.hash_pandas_object(fixed,index=False).to_numpy().tobytes()).hexdigest()
    if digest!=protocol['development_sha256']:raise ValueError('Reference development inputs changed')
    recipe_path=root/'reference_recipe.json'
    if not recipe_path.exists():
        if source_digest()!=protocol['source_sha256']:
            raise ValueError('Reference original recipe must be recorded before code changes')
        write_json(recipe_path,_make_recipe(frame,root,report))
    recipe=json.loads(recipe_path.read_text());_check_recipe(recipe,root)
    if (recipe['origin_source_sha256']!=protocol['source_sha256']
            or recipe['base_frame_sha256']!=_frame_sha(frame)):
        raise ValueError('Reference complete input frame changed')
    external=_validate(development,get_feature_columns(development),config,training=False) if config.evaluation_tickers else pd.DataFrame()
    calendar=pd.DatetimeIndex(development.loc[development.ticker.isin((*TRAINING_TICKERS,'0050')),'date'].unique()).sort_values()
    split_frame=fixed.drop(columns=[f'benchmark_target_{h}' for h in config.horizons],errors='ignore').copy()
    for h in config.horizons:split_frame.loc[split_frame[f'label_end_{h}'].ge(start),f'label_end_{h}']=pd.NaT
    splits={h:temporal_splits(split_frame,h,config,calendar_dates=calendar) for h in config.horizons}
    stored=[pd.read_parquet(root/'predictions.parquet')]
    transferred=pd.read_parquet(root/'transfer_predictions.parquet')
    if not transferred.empty:stored.append(transferred)
    original=pd.concat(stored,ignore_index=True)
    candidates={DirectionCandidate(**c).name:DirectionCandidate(**c) for c in protocol['candidates']}
    records=[]
    (destination/'models').mkdir(parents=True,exist_ok=True)
    (destination/'rows').mkdir(exist_ok=True)
    for selection in report['selections']:
        h,fold=selection['horizon'],selection['fold'];split=splits[h][fold-1]
        train,calibration,test=(fixed.loc[split[k]].copy() for k in ['train','calibration','test'])
        candidate=candidates[selection['candidate']];columns=selection['feature_columns']
        if select_feature_columns(train,candidate.feature_profile)!=columns:
            raise ValueError('Reference feature profile changed')
        with threadpool_limits(limits=1):
            model,used=fit_direction_model(candidate,train,columns,h,config.random_state)
            probability=_positive_probability(model,_numeric(calibration,columns))
            metrics=_calibration_metrics(calibration,probability,selection['threshold'],h)
            expected=next(s for s in selection['candidate_scores'] if s['candidate']==candidate.name and s.get('threshold')==selection['threshold'])
            if any(metrics[k]!=expected[k] for k in metrics):raise ValueError('Reference calibration replay mismatch')
            if getattr(model,'training_details',{})!=selection['model_details']:raise ValueError('Reference training statistics mismatch')
            parts=[test]
            if not external.empty:parts.append(external.loc[external.date.isin(split['test_calendar'])])
            probe=pd.concat(parts,ignore_index=True)
            predicted=_positive_probability(model,_numeric(probe,columns))
        probe=probe[['ticker','date','decision_ts',f'target_{h}']].copy()
        probe['probability_up']=predicted
        actual=original.loc[original.horizon.eq(h)&original.fold.eq(fold)].set_index(['ticker','date'])
        probe=probe.set_index(['ticker','date'])
        if len(probe)!=len(actual) or not probe.index.is_unique or set(probe.index)!=set(actual.index):
            raise ValueError('Reference replay stock/date grid changed')
        probe=probe.reindex(actual.index)
        if (not np.array_equal(probe.probability_up.to_numpy(),actual.probability_up.to_numpy())
                or not np.array_equal(probe.decision_ts.to_numpy(),actual.decision_ts.to_numpy())
                or not np.array_equal(np.expm1(probe[f'target_{h}']).to_numpy(),actual.actual_return.to_numpy(),equal_nan=True)):
            raise ValueError('Reference prediction replay mismatch')
        if actual.model_version.nunique()!=1:raise ValueError('Reference model version is not shared')
        model_name=f'models/h{h}-f{fold}.joblib';rows_name=f'rows/h{h}-f{fold}.parquet'
        joblib.dump(model,destination/model_name)
        keys=used[['ticker','date']].copy();keys.insert(0,'row_index',used.index.to_numpy())
        keys.to_parquet(destination/rows_name,index=False)
        records.append(dict(horizon=h,fold=fold,candidate=candidate.name,
            model_file=model_name,model_sha256=_sha(destination/model_name),rows_file=rows_name,rows_sha256=_sha(destination/rows_name),
            train_indices=train.index.tolist(),calibration_indices=calibration.index.tolist(),
            origin_model_version=str(actual.model_version.iloc[0]),replayed_probabilities=len(actual)))
    manifest=dict(schema_version=1,reference_candidate_count=len(candidates),selected_models_replayed_exactly=True,
        recipe_sha256=_sha(recipe_path),origin_source_sha256=protocol['source_sha256'],pin_source_sha256=source_digest(),models=records)
    write_json(destination/'manifest.json',manifest)
    return manifest


class DirectionReference:
    """Validate a complete reference once, then expose its verified fold winner."""

    def __init__(self,path,frame,protocol):
        self.root=Path(path);self.pinned=self.root/'frozen_reference'
        manifest_path=_local_file(self.pinned,'manifest.json')
        self.manifest=json.loads(manifest_path.read_text())
        recipe_path=_local_file(self.root,'reference_recipe.json')
        if _sha(recipe_path)!=self.manifest['recipe_sha256']:raise ValueError('Reference recipe checksum mismatch')
        self.recipe=json.loads(recipe_path.read_text());_check_recipe(self.recipe,self.root)
        self.report=json.loads((self.root/'report.json').read_text());old=self.report['protocol']
        if (self.manifest.get('schema_version')!=1
                or self.manifest.get('selected_models_replayed_exactly') is not True
                or self.manifest.get('reference_candidate_count')!=len(old['candidates'])
                or self.manifest.get('origin_source_sha256')!=old['source_sha256']
                or self.recipe.get('origin_source_sha256')!=old['source_sha256']
                or len(self.manifest['models'])!=len(self.report['selections'])
                or {(r['horizon'],r['fold']) for r in self.manifest['models']}
                    !={(s['horizon'],s['fold']) for s in self.report['selections']}):
            raise ValueError('Reference pin is incomplete or unverified')
        for field in ['config','selection_policy','mode','holdout_dates','holdout_start','evidence_kind','constituent_audit']:
            if _plain(protocol.get(field))!=old.get(field):raise ValueError('Reference protocol context changed')
        count=self.manifest['reference_candidate_count'];self.count=count
        if protocol['candidates'][:count]!=old['candidates']:raise ValueError('Reference candidate prefix changed')
        columns=self.recipe['base_columns']
        if not set(columns).issubset(frame) or _frame_sha(frame[columns])!=self.recipe['base_frame_sha256']:
            raise ValueError('Reference complete input frame changed')
        for name,value in self.recipe['base_attrs'].items():
            if _plain(frame.attrs.get(name))!=value:raise ValueError('Reference receipt context changed')
        for profile,expected in self.recipe['feature_profiles'].items():
            if select_feature_columns(frame,profile)!=expected:raise ValueError('Reference feature profile changed')
        for record in self.manifest['models']:
            for kind in ['model','rows']:
                if _sha(_local_file(self.pinned,record[kind+'_file']))!=record[kind+'_sha256']:
                    raise ValueError('Reference pinned artifact checksum mismatch')
        self.info=dict(candidate_count=count,origin_source_sha256=old['source_sha256'],
            manifest_sha256=_sha(manifest_path),selected_models_replayed_exactly=True)

    def fold(self,horizon,fold,train,calibration):
        from .direction import DirectionCandidate,_calibration_metrics,_selection_key
        selection=next(s for s in self.report['selections'] if s['horizon']==horizon and s['fold']==fold)
        record=next(r for r in self.manifest['models'] if r['horizon']==horizon and r['fold']==fold)
        if train.index.tolist()!=record['train_indices'] or calibration.index.tolist()!=record['calibration_indices']:
            raise ValueError('Reference mature split changed')
        model=joblib.load(_local_file(self.pinned,record['model_file']))
        keys=pd.read_parquet(_local_file(self.pinned,record['rows_file']))
        if not keys.row_index.isin(train.index).all():raise ValueError('Reference training subset changed')
        subset=train.loc[keys.row_index.to_numpy()].copy()
        if not np.array_equal(subset[['ticker','date']].to_numpy(),keys[['ticker','date']].to_numpy()):
            raise ValueError('Reference training row identities changed')
        candidate=next(DirectionCandidate(**c) for c in self.report['protocol']['candidates'] if DirectionCandidate(**c).name==selection['candidate'])
        columns=selection['feature_columns'];threshold=selection['threshold']
        probability=_positive_probability(model,_numeric(calibration,columns))
        metrics=_calibration_metrics(calibration,probability,threshold,horizon)
        expected=next(s for s in selection['candidate_scores'] if s['candidate']==candidate.name and s.get('threshold')==threshold)
        if any(metrics[k]!=expected[k] for k in metrics):raise ValueError('Reference cached calibration mismatch')
        policy=self.report['protocol']['selection_policy'];key=_selection_key(metrics,threshold,policy)
        scores=_plain(selection['candidate_scores'])
        if max(tuple(s['selection_score']) for s in scores if 'selection_score' in s)!=key:
            raise ValueError('Reference selected candidate is inconsistent')
        return (candidate,model,columns,threshold,subset),key,scores
