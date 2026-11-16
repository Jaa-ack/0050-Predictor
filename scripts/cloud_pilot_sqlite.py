"""Research-venv SQLite tuning; preserve WAL/FULL and all frozen scoring code.

Avoid random checkpoint writes on the collector/forecast commit path of a small
standard persistent disk. Committed WAL remains durable and readers see it;
SQLite still checkpoints on final connection close. The existing disk reserve
also accounts for WAL growth. A small explicit cache reduces index rereads.
"""
from functools import wraps
import importlib
import sqlite3
import sys

_CONNECT = sqlite3.connect


def checkpoint_supported(version):return tuple(version)>=(3,51,3)


@wraps(_CONNECT)
def connect(*args, **kwargs):
    connection = _CONNECT(*args, **kwargs)
    try:
        connection.execute('PRAGMA wal_autocheckpoint=0')
        connection.execute('PRAGMA cache_size=-8192')
        connection.execute('PRAGMA journal_size_limit=134217728')
    except BaseException:
        connection.close()
        raise
    return connection


connect._twquant_cloud_tuned = True


def install():
    global sqlite3,_CONNECT
    if getattr(sqlite3.connect, '_twquant_cloud_tuned', False):
        return
    if sys.platform=='linux':
        try:backend=importlib.import_module('pysqlite3')
        except ImportError:backend=None
        if backend is not None:
            if not checkpoint_supported(backend.sqlite_version_info):raise RuntimeError('sqlite_backend_version_unverified')
            sqlite3=backend;sys.modules['sqlite3']=backend;sys.modules['sqlite3.dbapi2']=backend.dbapi2
    _CONNECT=sqlite3.connect
    sqlite3.connect = connect
    sqlite3.dbapi2.connect = connect
