import threading
import time
from pathlib import Path

import pytest

from twquant.credentials import Credentials
from twquant.web_jobs import JobManager


def wait_finished(manager):
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        jobs=manager.list()
        if jobs and jobs[0]['status'] not in {'queued','running'}:
            return jobs[0]
        time.sleep(.01)
    raise AssertionError('workflow did not finish')


def test_jobs_use_allowlisted_argv_mask_keys_and_reject_concurrent_mutations(tmp_path):
    gate=threading.Event()
    calls=[]
    secret='example-web-job-private-value'
    def runner(argv,cwd):
        calls.append((argv,cwd))
        gate.wait(2)
        return 0,'result '+secret
    manager=JobManager({'demo':tmp_path/'demo','real':tmp_path/'real'},tmp_path,
                       Credentials({'FRED_API_KEY':secret}),runner)
    job=manager.submit('demo','demo')
    assert job['status'] in {'queued','running'}
    with pytest.raises(RuntimeError,match='running|active'):
        manager.submit('research','demo')
    gate.set()
    done=wait_finished(manager)
    assert done['status']=='succeeded'
    assert secret not in done['output_tail']
    assert '[REDACTED]' in done['output_tail']
    argv=calls[0][0]
    assert '-m' in argv and 'twquant' in argv and argv[-1]=='demo'
    assert secret not in str(calls)


def test_jobs_reject_shell_paths_and_demo_real_source_mixing(tmp_path):
    manager=JobManager({'demo':tmp_path/'demo','real':tmp_path/'real'},tmp_path,Credentials({}))
    for action,workspace in [('rm -rf /','real'),('demo','../../elsewhere'),('demo','real'),('sync','demo')]:
        with pytest.raises(ValueError):
            manager.submit(action,workspace)


def test_job_failure_reports_safe_error_and_releases_busy_state(tmp_path):
    def runner(argv,cwd):
        return 2,'missing real price data'
    manager=JobManager({'real':tmp_path/'real'},tmp_path,Credentials({}),runner)
    manager.submit('research','real')
    assert wait_finished(manager)['status']=='failed'
    manager.submit('research','real')
    assert wait_finished(manager)['status']=='failed'


def test_web_prediction_includes_only_configured_available_transfer_assets(tmp_path):
    import json
    from twquant.data import PriceStore
    from twquant.demo import make_demo_prices
    root=tmp_path/'real';p=make_demo_prices(5)
    PriceStore(root/'data').write_prices(p)
    (root/'current.json').write_text(json.dumps({'run_id':'test'}))
    folder=root/'runs'/'test';folder.mkdir(parents=True)
    (folder/'report.json').write_text(json.dumps({'provenance':{'config':{'evaluation_tickers':['0050','1216','IX0001']}}}))
    manager=JobManager({'real':root},tmp_path,Credentials({}))
    argv=manager._argv('predict','real')
    selected=argv[argv.index('--tickers')+1].split(',')
    assert '0050' in selected
    assert '1216' not in selected
    assert 'IX0001' not in selected
    assert '2330' in selected
