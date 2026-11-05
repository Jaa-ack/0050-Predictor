"""Bounded local web workflow execution; no shell or provider credentials in argv."""
from datetime import datetime,timezone
from pathlib import Path
import subprocess
import sys
import threading
import uuid

from .credentials import redact

ACTIONS={'demo','sync','research','holdout','predict','settle'}


def _now():
    return datetime.now(timezone.utc).isoformat()


class JobManager:
    def __init__(self,workspaces,project_dir,credentials,runner=None):
        self.workspaces={k:Path(v).resolve() for k,v in workspaces.items()}
        self.project_dir=Path(project_dir).resolve()
        self.credentials=credentials
        self.runner=runner or self._run_command
        self._lock=threading.Lock()
        self._jobs=[]
        self._active=None

    @staticmethod
    def _run_command(argv,cwd):
        # Fixed argv, never shell=True; provider credentials remain server-side.
        result=subprocess.run(argv,cwd=cwd,capture_output=True,text=True,timeout=3600,check=False)
        return result.returncode,result.stdout+'\n'+result.stderr

    def _argv(self,action,workspace):
        root=self.workspaces[workspace]
        base=[sys.executable,'-m','twquant','--root',str(root)]
        if action=='sync':
            return base+['sources','sync','--plan',str(self.project_dir/'configs'/'source_plan.json')]
        if action=='research':
            args=base+['research','--config',str(self.project_dir/'configs'/'research.json')]
            events=root/'sources'/'events.csv'
            return args+(['--events',str(events)] if events.exists() else [])
        if action in {'predict','settle'}:
            import pandas as pd
            cutoff=pd.Timestamp.now(tz='Asia/Taipei').isoformat()
            if workspace=='demo' or root.name=='demo':
                from .data import PriceStore
                cutoff=PriceStore(root/'data').read_prices().available_at.max().isoformat()
            args=base+[action,'--as-of',cutoff]
            if action=='predict':
                from .data import PriceStore
                from .workflows import current_report
                from .universe import TRAINING_TICKERS
                available=set(PriceStore(root/'data').read_prices().ticker)
                report=current_report(root)
                configured=report.get('provenance',{}).get('config',{}).get('evaluation_tickers',[])
                targets=[t for t in dict.fromkeys([*TRAINING_TICKERS,*configured]) if t in available]
                args+=['--paper','--tickers',','.join(targets)]
                events=root/'sources'/'events.csv'
                if events.exists():
                    args+=['--events',str(events)]
            return args
        return base+[action]

    def submit(self,action,workspace):
        if action not in ACTIONS or workspace not in self.workspaces:
            raise ValueError('Unsupported action or workspace')
        demo=workspace=='demo' or self.workspaces[workspace].name=='demo'
        if action=='demo' and not demo or action=='sync' and demo:
            raise ValueError('Demo generation and private-source acquisition use separate workspaces')
        with self._lock:
            if self._active:
                raise RuntimeError('A workflow is already running; wait for the active job')
            record=dict(id=uuid.uuid4().hex,action=action,workspace=workspace,status='queued',
                        started_at=None,finished_at=None,message='等待執行',output_tail='')
            self._jobs.insert(0,record)
            self._jobs=self._jobs[:50]
            self._active=record['id']
            answer=dict(record)
        threading.Thread(target=self._execute,args=(record,),daemon=True).start()
        return answer

    def _execute(self,record):
        with self._lock:
            record.update(status='running',started_at=_now(),message='工作執行中')
        try:
            argv=self._argv(record['action'],record['workspace'])
            code,output=self.runner(argv,self.project_dir)
            if code==0 and record['action']=='sync':
                from .source_store import SourceStore
                store=SourceStore(self.workspaces[record['workspace']]/'sources')
                if any(store._has_mapping(m['provider'],m['dataset'],m['kind']) for m in store.manifests()):
                    more_code,more_output=self.runner([sys.executable,'-m','twquant','--root',
                        str(self.workspaces[record['workspace']]),'sources','export-events'],self.project_dir)
                    code=more_code
                    output+='\n'+more_output
            message='已完成，研究資料已更新' if code==0 else '未完成，請查看執行訊息與資料設定'
        except Exception as exc:
            code=2
            output=str(exc)
            message='未完成，請查看執行訊息與資料設定'
        safe=redact(str(output),self.credentials.secret_values())[-12000:]
        with self._lock:
            record.update(status='succeeded' if code==0 else 'failed',finished_at=_now(),message=message,output_tail=safe)
            self._active=None

    def list(self):
        with self._lock:
            return [dict(job) for job in self._jobs]
