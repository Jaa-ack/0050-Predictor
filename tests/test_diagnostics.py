import pandas as pd
import pytest


def test_block_loss_audit_resamples_dates_together_and_preserves_pairing():
    from twquant.diagnostics import paired_block_audit
    rows=pd.DataFrame({'date':pd.date_range('2024-01-01',periods=40),'horizon':1,
                       'actual_return':.01,'probability_up':.8,'training_probability':.5})
    a=paired_block_audit(rows,repetitions=100)
    b=paired_block_audit(pd.concat([rows]*10,ignore_index=True),repetitions=100)
    m=a['by_horizon']['1']
    assert m['dates']==40
    assert m['loss_difference']==pytest.approx(-.21)
    assert m['ci95'][0]==pytest.approx(-.21)
    assert m['ci95']==b['by_horizon']['1']['ci95']
    assert a['unit']=='market_date'
    assert a['exploratory'] is True
