import json

import numpy as np
import pandas as pd
import pytest


def reference_case(tmp_path,policy='pooled_brier'):
    from test_direction import preopen_frame,search_frame
    from test_research import small_config
    from twquant.direction import DirectionCandidate,search_direction_models
    frame=preopen_frame(search_frame())
    frame['target_1']=np.where(frame.f_signal>0,.01,-.01)
    config=small_config(horizons=(1,),max_folds=1,evaluation_tickers=('0050','1216'))
    candidates=(DirectionCandidate('prior'),)
    output=tmp_path/'reference'
    result=search_direction_models(frame,config,candidates=candidates,holdout_dates=15,
        bootstrap_samples=10,evidence_kind='synthetic',output=output,selection_policy=policy,mode='preopen')
    return frame,config,candidates,output,result


@pytest.mark.parametrize('policy',['worst_stock_accuracy','pooled_brier','worst_stock_brier'])
@pytest.mark.parametrize('new_model',['prior','logistic'])
def test_pinned_reference_matches_fresh_extended_selection_probabilities_and_audits(tmp_path,policy,new_model):
    from twquant.direction import DirectionCandidate,search_direction_models
    from twquant.direction_reference import pin_direction_reference
    frame,config,old,output,original=reference_case(tmp_path,policy)
    manifest=pin_direction_reference(frame,config,output)
    assert manifest['selected_models_replayed_exactly'] is True
    assert manifest['reference_candidate_count']==1
    augmented=frame.assign(f_path_probe=frame.f_signal**2)
    candidates=old+(DirectionCandidate(new_model,'portable_path'),)
    options=dict(candidates=candidates,holdout_dates=15,bootstrap_samples=10,evidence_kind='synthetic',selection_policy=policy,mode='preopen')
    fresh=search_direction_models(augmented,config,**options)
    cached=search_direction_models(augmented,config,reference_run=output,**options)
    pd.testing.assert_frame_equal(fresh.predictions,cached.predictions)
    pd.testing.assert_frame_equal(fresh.transfer_predictions,cached.transfer_predictions)
    assert fresh.report['selections']==cached.report['selections']
    assert fresh.report['by_horizon']==cached.report['by_horizon']
    assert cached.report['protocol']['reference']['candidate_count']==1
    assert cached.report['real_world_target_verified'] is False
    if new_model=='prior':
        np.testing.assert_array_equal(cached.predictions.probability_up,original.predictions.probability_up)
        assert cached.predictions.candidate.eq(old[0].name).all()
    else:
        assert cached.predictions.candidate.eq(candidates[-1].name).all()


@pytest.mark.parametrize('problem',['training_value','external_value','future_label','config','policy','profile_leak','candidate_prefix','corrupt_model','unverified_pin','changed_report'])
def test_reference_rejects_changed_inputs_recipe_context_or_artifacts(tmp_path,problem):
    from dataclasses import replace
    from twquant.direction import DirectionCandidate,search_direction_models
    from twquant.direction_reference import pin_direction_reference
    frame,config,old,output,_=reference_case(tmp_path)
    pin_direction_reference(frame,config,output)
    augmented=frame.assign(f_path_probe=1.)
    policy='pooled_brier';candidates=old+(DirectionCandidate('logistic','portable_path'),)
    if problem=='training_value':augmented.loc[0,'f_signal']+=1
    elif problem=='external_value':augmented.loc[augmented.ticker.eq('1216'),'f_signal']+=1
    elif problem=='future_label':augmented.loc[augmented.index[-1],'target_1']=999.
    elif problem=='config':config=replace(config,random_state=43)
    elif problem=='policy':policy='worst_stock_brier'
    elif problem=='profile_leak':augmented['f_unscoped_new_input']=1.
    elif problem=='candidate_prefix':candidates=tuple(reversed(candidates))
    elif problem=='corrupt_model':
        model=next((output/'frozen_reference/models').glob('*.joblib'))
        model.write_bytes(model.read_bytes()+b'corrupt')
    elif problem=='unverified_pin':
        path=output/'frozen_reference/manifest.json'
        data=json.loads(path.read_text());data['selected_models_replayed_exactly']=False
        path.write_text(json.dumps(data))
    else:
        report=output/'report.json';report.write_text(report.read_text()+' ')
    with pytest.raises(ValueError,match='[Rr]eference'):
        search_direction_models(augmented,config,candidates=candidates,holdout_dates=15,bootstrap_samples=10,
            evidence_kind='synthetic',selection_policy=policy,mode='preopen',reference_run=output)


def test_pin_does_not_overwrite_an_existing_reference(tmp_path):
    from twquant.direction_reference import pin_direction_reference
    frame,config,_,output,_=reference_case(tmp_path)
    pin_direction_reference(frame,config,output)
    with pytest.raises(ValueError,match='already pinned'):
        pin_direction_reference(frame,config,output)


def test_multiple_cached_candidates_preserve_unavailable_scores_and_the_original_winner(tmp_path):
    from test_direction import preopen_frame,search_frame
    from test_research import small_config
    from twquant.direction import DirectionCandidate,search_direction_models
    from twquant.direction_reference import pin_direction_reference
    frame=preopen_frame(search_frame());frame['target_1']=np.where(frame.f_signal>0,.01,-.01)
    config=small_config(horizons=(1,),max_folds=1,evaluation_tickers=('0050','1216'))
    old=tuple(DirectionCandidate(name) for name in ['prior','logistic','factor_ridge'])
    options=dict(holdout_dates=15,bootstrap_samples=10,evidence_kind='synthetic',selection_policy='pooled_brier',mode='preopen')
    root=tmp_path/'reference'
    base=search_direction_models(frame,config,candidates=old,output=root,**options)
    assert base.report['selections'][0]['candidate']==old[1].name
    assert base.report['selections'][0]['candidate_scores'][-1]['status']=='unavailable'
    pin_direction_reference(frame,config,root)
    augmented=frame.assign(f_path_probe=frame.f_signal**2)
    candidates=old+(DirectionCandidate('hist','portable_path'),)
    cached=search_direction_models(augmented,config,candidates=candidates,reference_run=root,**options)
    fresh=search_direction_models(augmented,config,candidates=candidates,**options)
    assert cached.report['selections']==fresh.report['selections']
    pd.testing.assert_frame_equal(cached.predictions,fresh.predictions)


def test_reference_rejects_a_changed_similarity_implementation_dependency(tmp_path, monkeypatch):
    from test_direction import preopen_frame, search_frame
    from test_research import small_config
    import twquant.direction_reference as reference
    from twquant.direction import DirectionCandidate, search_direction_models
    frame=preopen_frame(search_frame())
    config=small_config(horizons=(1,),max_folds=1,evaluation_tickers=('0050','1216'))
    candidates=(DirectionCandidate('similarity_local'),)
    options=dict(candidates=candidates,holdout_dates=15,bootstrap_samples=10,
                 evidence_kind='synthetic',selection_policy='pooled_brier',mode='preopen')
    output=tmp_path/'reference'
    search_direction_models(frame,config,output=output,**options)
    reference.pin_direction_reference(frame,config,output)
    original=reference._sha
    def changed_dependency(path):
        return '0'*64 if path.name=='direction_similarity.py' else original(path)
    monkeypatch.setattr(reference,'_sha',changed_dependency)
    with pytest.raises(ValueError,match='recipe'):
        search_direction_models(frame,config,reference_run=output,**options)
