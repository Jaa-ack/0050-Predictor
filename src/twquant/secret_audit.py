"""Offline checks for files eligible for open-source publication. Never print matched secrets."""
from pathlib import Path
import os
import re
import subprocess

from .credentials import Credentials

EXCLUDED={'.git','.venv','__pycache__','.pytest_cache','.playwright-cli','artifacts','output','data','build','dist','.secrets','credentials','log'}
PRIVATE_SUFFIXES={'.pfx','.p12','.pem','.key','.cer','.crt'}
RUNTIME_ROOTS={'artifacts','data','output','.venv','build','dist','credentials','.secrets','log'}
RUNTIME_SUFFIXES={'.joblib','.parquet','.sqlite','.sqlite3','.db','.pkl','.pickle'}


def audit_repository(root,credentials=None):
    root=Path(root).resolve()
    credentials=credentials or Credentials({})
    findings=[]
    tracked=set()
    if (root/'.git').exists():
        run=subprocess.run(['git','ls-files','--cached','-z'],cwd=root,capture_output=True,check=False)
        if run.returncode==0:
            tracked=set(run.stdout.decode().split('\0'))-{''}
    for relative in sorted(tracked):
        path=Path(relative)
        if path.parts[0] in RUNTIME_ROOTS or path.suffix.lower() in RUNTIME_SUFFIXES:
            findings.append(dict(path=relative,line=0,rule='tracked_runtime_artifact'))
    candidates=[]
    for current,dirs,files in os.walk(root,followlinks=False):
        dirs[:]=[d for d in dirs if d not in EXCLUDED and not d.endswith('.egg-info') and not Path(current,d).is_symlink()]
        for filename in files:
            p=Path(current,filename)
            relative=p.relative_to(root).as_posix()
            private=filename=='.env' or filename.startswith('.env.') and filename!='.env.example'
            if private:
                if relative in tracked:
                    findings.append(dict(path=relative,line=0,rule='tracked_secret_file'))
                continue
            candidates.append(p)
    # Include tracked files even if an ignore rule was added after a leak.
    candidates+= [root/p for p in tracked if (root/p).is_file() and root/p not in candidates]
    pattern=re.compile(r'''(?im)^\s*(?:FINMIND_TOKEN|[A-Z_]+API_KEY|FUBON_PASSWORD|FUBON_CERT_PASSWORD)\s*=\s*["']?([^\s"'\#]{12,})''')
    checked=0
    for path in dict.fromkeys(candidates):
        relative=path.relative_to(root).as_posix()
        if path.is_symlink():
            findings.append(dict(path=relative,line=0,rule='publication_symlink'))
            continue
        if path.suffix.lower() in PRIVATE_SUFFIXES:
            findings.append(dict(path=relative,line=0,rule='private_credential_file'))
            continue
        try:
            if path.stat().st_size>2_000_000:
                findings.append(dict(path=relative,line=0,rule='oversized_file_requires_manual_review'))
                continue
            text=path.read_text(encoding='utf-8')
        except (UnicodeError,OSError):
            continue
        checked+=1
        for number,line in enumerate(text.splitlines(),start=1):
            if any(secret and secret in line for secret in credentials.secret_values()):
                findings.append(dict(path=relative,line=number,rule='configured_secret_in_public_file'))
            match=pattern.search(line)
            if match and not match.group(1).lower().startswith(('example-','your_','placeholder','${','<')):
                findings.append(dict(path=relative,line=number,rule='possible_hardcoded_credential'))
            if line.lstrip().startswith(('-----BEGIN PRIVATE KEY-----','-----BEGIN RSA PRIVATE KEY-----')):
                findings.append(dict(path=relative,line=number,rule='private_key_material'))
    return dict(passed=not findings,files_checked=checked,findings=findings,
        history_scanned=False,limitation='Working-tree publication audit; run Gitleaks on full Git history before public release.')
