"""Research-venv SQLite tuning; preserve WAL/FULL and all frozen scoring code.

Avoid random checkpoint writes on the collector/forecast commit path of a small
standard persistent disk. Committed WAL remains durable and readers see it;
SQLite still checkpoints on final connection close. The existing disk reserve
also accounts for WAL growth. A small explicit cache reduces index rereads.
"""
from functools import wraps
import sqlite3

_CONNECT = sqlite3.connect


@wraps(_CONNECT)
def connect(*args, **kwargs):
    connection = _CONNECT(*args, **kwargs)
    try:
        connection.execute('PRAGMA wal_autocheckpoint=0')
        connection.execute('PRAGMA cache_size=-8192')
    except BaseException:
        connection.close()
        raise
    return connection


connect._twquant_cloud_tuned = True


def install():
    if getattr(sqlite3.connect, '_twquant_cloud_tuned', False):
        return
    sqlite3.connect = connect
    sqlite3.dbapi2.connect = connect
