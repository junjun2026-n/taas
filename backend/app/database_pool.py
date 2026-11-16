"""Bounded PostgreSQL connection reuse for the API and NiceGUI console."""
from contextlib import contextmanager
from threading import BoundedSemaphore, Lock

from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

_pool = None
_lock = Lock()
_slots = BoundedSemaphore(20)


def get_pool(settings):
    global _pool
    with _lock:
        if _pool is None:
            options = dict(settings)
            options['options'] = (options.get('options', '') + ' -c search_path=taas').strip()
            options.setdefault('connect_timeout', 8)
            _pool = ThreadedConnectionPool(2, 20, cursor_factory=RealDictCursor, **options)
        return _pool


@contextmanager
def connection(settings):
    if not _slots.acquire(timeout=10):
        raise TimeoutError('Database connection pool is busy')
    pool = conn = None
    try:
        pool = get_pool(settings)
        conn = pool.getconn()
        try:
            yield conn
            conn.commit()
        except BaseException:
            if not conn.closed:
                conn.rollback()
            raise
    finally:
        try:
            if conn is not None:
                pool.putconn(conn, close=bool(conn.closed))
        finally:
            _slots.release()


def close_pool():
    global _pool
    with _lock:
        if _pool is not None:
            _pool.closeall()
            _pool = None
