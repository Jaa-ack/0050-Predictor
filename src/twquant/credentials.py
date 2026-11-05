"""Server-only credential boundary; secret values have no printable representation."""
from pathlib import Path
import os
import re
import stat
import logging
from urllib.parse import quote, quote_plus

from pydantic import SecretStr

KEY_NAMES = ("FINMIND_TOKEN", "FRED_API_KEY", "FMP_API_KEY", "NEWS_API_KEY", "TWINKLE_API_KEY",
             "FUGLE_API_KEY", "FUBON_API_KEY", "FUBON_PERSONAL_ID", "FUBON_PASSWORD", "FUBON_CERT_PATH", "FUBON_CERT_PASSWORD")


class Credentials:
    def __init__(self, values):
        self._values = {k:SecretStr(v) for k,v in values.items() if k in KEY_NAMES and v}

    def __repr__(self):
        return f"Credentials(configured={sorted(self._values)})"

    def get(self, name):
        if name not in self._values:
            raise ValueError(f"Missing credential: {name}; set it in your private .env or environment")
        return self._values[name]

    def status(self):
        return {k:k in self._values for k in KEY_NAMES}

    def secret_values(self):
        """Use only at the transport/redaction boundary, never for status output."""
        return tuple(v.get_secret_value() for v in self._values.values())


def load_credentials(path=Path(".env")):
    path=Path(path)
    values={}
    if path.is_symlink():
        raise ValueError("Credential file must not be a symlink")
    if path.exists():
        flags=os.O_RDONLY | getattr(os,"O_NOFOLLOW",0)
        fd=os.open(path,flags)
        with os.fdopen(fd,encoding="utf-8") as handle:
            info=os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or (os.name=="posix" and
               (stat.S_IMODE(info.st_mode)&0o077 or info.st_uid!=os.getuid())):
                raise ValueError("Credential file requires owner-only permissions: chmod 0600 .env")
            for line in handle:
                line=line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name,value=line.removeprefix("export ").split("=",1)
                name=name.strip()
                if name in KEY_NAMES:
                    value=value.strip()
                    if len(value)>=2 and value[0]==value[-1] and value[0] in "\"'":
                        value=value[1:-1]
                    values[name]=value
    for name in KEY_NAMES:
        if os.environ.get(name):
            values[name]=os.environ[name]
    result=Credentials(values)
    install_secret_filters(result)
    return result


def initialize_credentials(path=Path(".env")):
    path=Path(path)
    try:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0),0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd,"w",encoding="utf-8") as handle:
        handle.write("# Private local credentials. Never commit, upload or paste these values.\n")
        handle.write("\n".join(name+"=" for name in KEY_NAMES)+"\n")
    return True


def redact(value, secrets=()):
    sensitive={"token","apikey","apisecret","authorization","password","secret","personalid",
               "certpath","certificate","certpassword","mcpsessionid","xapikey","cookie","setcookie"}
    if isinstance(value,dict):
        return {str(k):"[REDACTED]" if re.sub(r"[^a-z]","",str(k).lower()) in sensitive
                or str(k).upper() in KEY_NAMES else redact(v,secrets) for k,v in value.items()}
    if isinstance(value,(list,tuple)):
        return [redact(v,secrets) for v in value]
    if isinstance(value,SecretStr):
        return "[REDACTED]"
    if isinstance(value,str):
        for secret in sorted((s for s in secrets if s),key=len,reverse=True):
            for variant in {secret,quote(secret,safe=""),quote_plus(secret)}:
                value=value.replace(variant,"[REDACTED]")
        return value
    return value


class _SecretFilter(logging.Filter):
    def __init__(self,credentials):
        super().__init__()
        self.credentials=credentials

    def filter(self,record):
        record.msg=redact(record.getMessage(),self.credentials.secret_values())
        record.args=()
        # Provider exception reprs can contain authenticated URLs or SDK account objects.
        record.exc_info=None
        record.exc_text=None
        record.stack_info=None
        return True


def install_secret_filters(credentials):
    for name in ('urllib3','urllib3.connectionpool','requests','httpx','httpcore','fubon_neo'):
        logger=logging.getLogger(name)
        for old in list(logger.filters):
            if isinstance(old,_SecretFilter):
                logger.removeFilter(old)
        logger.addFilter(_SecretFilter(credentials))
        logger.setLevel(logging.WARNING)
