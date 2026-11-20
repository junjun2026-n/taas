# ============================================================
# 文件名称：test_database_pool.py
# 生成日期：2026-10-22
# 修改日期：2026-11-20
# 版本信息：v0.261115
# 作    者：JUN
# 备    注：本程序由AI辅助完成。
#           文件功能：验证 database_pool 相关功能。
#           文件用途：用于开发验证及功能回归。
#           注意事项：使用隔离测试环境，避免操作正式业务数据。
# ============================================================
import unittest
from unittest.mock import Mock, patch

from app import database_pool as database


class DatabasePoolTests(unittest.TestCase):
    def tearDown(self):
        database.close_pool()

    def test_queries_reuse_pool_commit_and_return_connection(self):
        conn = Mock(closed=False)
        pool = Mock()
        pool.getconn.return_value = conn
        with patch.object(database, 'ThreadedConnectionPool', return_value=pool) as create:
            for _ in range(3):
                with database.connection({'dbname': 'test'}) as actual:
                    self.assertIs(actual, conn)
            create.assert_called_once()
            self.assertIn('search_path=taas', create.call_args.kwargs['options'])
        self.assertEqual(conn.commit.call_count, 3)
        self.assertEqual(pool.putconn.call_count, 3)
        pool.putconn.assert_called_with(conn, close=False)

    def test_failed_transaction_rolls_back_and_releases_capacity(self):
        conn = Mock(closed=False)
        pool = Mock()
        pool.getconn.return_value = conn
        with patch.object(database, 'ThreadedConnectionPool', return_value=pool):
            with self.assertRaises(ValueError):
                with database.connection({}):
                    raise ValueError('invalid operation')
            conn.rollback.assert_called_once()
            conn.commit.assert_not_called()
            with database.connection({}):
                pass
        self.assertEqual(pool.putconn.call_count, 2)

    def test_connection_failure_does_not_leak_slots(self):
        pool = Mock()
        pool.getconn.side_effect = RuntimeError('cannot connect')
        with patch.object(database, 'get_pool', return_value=pool), \
             patch.object(database, '_slots') as slots:
            slots.acquire.return_value = True
            with self.assertRaises(RuntimeError):
                with database.connection({}):
                    pass
            slots.release.assert_called_once()
