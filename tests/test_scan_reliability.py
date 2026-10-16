import sqlite3
import unittest
import uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from h1bfinder import db, ats


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(__file__).parent / ('scan-test-' + uuid.uuid4().hex + '.db')
        self.conn = db.connect(self.path)

    def tearDown(self):
        self.conn.close()
        for suffix in ['', '-wal', '-shm']:
            Path(str(self.path) + suffix).unlink(missing_ok=True)

    def test_workers_do_not_query_the_shared_connection(self):
        execute = db.Connection.execute
        main = self.conn

        def reject_shared_sql(conn, sql, *args, **kwargs):
            if conn is main:
                raise AssertionError('Worker queried the shared connection')
            return execute(conn, sql, *args, **kwargs)

        def worker(_):
            try:
                for _ in range(250):
                    self.assertEqual(db.path_of(main), str(self.path.resolve()))
                    own = db.thread_conn(main)
                    self.assertIsNot(own, main)
                    self.assertEqual(own.execute('SELECT 1').fetchone()[0], 1)
            finally:
                own = getattr(db._tl, 'conn', None)
                if own is not None:
                    own.close()
                    del db._tl.conn

        with patch.object(db.Connection, 'execute', reject_shared_sql):
            with ThreadPoolExecutor(12) as pool:
                list(pool.map(worker, range(12)))

    def test_standard_sqlite_connection_still_supported(self):
        with sqlite3.connect(self.path) as standard:
            self.assertEqual(db.path_of(standard), str(self.path.resolve()))
        standard.close()


class ScanTests(unittest.TestCase):
    def test_lookup_and_storage_errors_do_not_abort_remaining_employers(self):
        scan = ats.Scanner(None)
        employers = [('Lookup fails', 'LOOKUP'), ('Write fails', 'WRITE'), ('Works', 'GOOD')]
        scan.state.update(running=True, total=3)
        stored = []

        def lookup(name, norm, refresh):
            if norm == 'LOOKUP':
                raise TypeError("'NoneType' object is not subscriptable")
            return norm, ('lever', norm.lower(), [{}]), 'scanned'

        def write(norm, result):
            if norm == 'WRITE':
                raise sqlite3.OperationalError('simulated write error')
            stored.append(norm)

        with patch.object(scan, '_one', side_effect=lookup), patch.object(scan, '_store', side_effect=write), patch('traceback.print_exc'):
            scan._run(employers, False, 3)

        self.assertEqual(scan.state['done'], 3)
        self.assertEqual(scan.state['failed'], 2)
        self.assertFalse(scan.state['running'])
        self.assertEqual(scan.state['boards'], 1)
        self.assertEqual(scan.state['jobs'], 1)
        self.assertEqual(stored, ['GOOD'])
        self.assertTrue('LOOKUP' in scan.state['error'] or 'WRITE' in scan.state['error'])

    def test_cached_results_and_misses_count_as_processed(self):
        scan = ats.Scanner(None)
        employers = [('Cached job', 'FRESH'), ('Cached miss', 'MISS')]
        with patch.object(scan, '_one', side_effect=lambda n, norm, r: (norm, None, 'fresh' if norm == 'FRESH' else 'cached-miss')), patch.object(scan, '_store') as store:
            scan._run(employers, False, 2)
        self.assertEqual(scan.state['done'], 2)
        self.assertEqual(scan.state['failed'], 0)
        store.assert_not_called()


if __name__ == '__main__':
    unittest.main()

