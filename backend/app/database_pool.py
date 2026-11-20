# ============================================================
# 文件名称：database_pool.py
# 生成日期：2026-10-22
# 修改日期：2026-11-20
# 版本信息：v0.261115
# 作    者：JUN
# 备    注：本程序由AI辅助完成。
#           文件功能：实现 database_pool 模块相关功能。
#           文件用途：供TAAS的 taas_api/backend/app/database_pool.py 模块使用。
#           注意事项：运行前检查依赖与配置；涉及数据或服务变更时先备份并验证。
# ============================================================
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
            if settings.get('database_type') == 'mysql':
                from .mysql_backend import Pool
                _pool=Pool(settings,20,dictionaries=True); return _pool
            options = {k:v for k,v in settings.items() if k != 'database_type'}
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
