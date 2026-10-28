import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from contextlib import closing

from gmail_report import DeliveryError, JST, Reporter


class ReportsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'attendance.db'
        self.sent = []
        with closing(self.connect()) as db, db:
            db.executescript('''
                CREATE TABLE students(student_id TEXT PRIMARY KEY,name TEXT,class_name TEXT,active INTEGER);
                CREATE TABLE daily_attendance(attendance_date TEXT,student_id TEXT,arrived_at TEXT);
                INSERT INTO students VALUES('a','学生A','クラスA',1),('b','学生B','クラスA',1),
                    ('c','学生C','クラスB',1),('d','無効','クラスB',0);
            ''')
        self.reporter = Reporter(self.connect, None, lambda config, body: self.sent.append(body))

    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def tick(self, time, day='2026-10-28'):
        self.reporter.tick(datetime.fromisoformat(day + 'T' + time).replace(tzinfo=JST))

    def arrive(self, student, time):
        with closing(self.connect()) as db, db:
            db.execute('INSERT INTO daily_attendance VALUES(?,?,?)',
                       ('2026-10-28', student, '2026-10-28 ' + time))

    def test_followup_snapshot_cutoff_restart_and_duplicates(self):
        self.arrive('a', '09:00:00')
        self.tick('08:59:59')
        self.assertEqual(self.sent, [])
        self.tick('09:00:00')
        self.arrive('b', '09:30:00')
        self.arrive('c', '10:00:01')
        self.reporter = Reporter(self.connect, None, lambda config, body: self.sent.append(body))
        self.tick('10:01:00')
        self.assertEqual(len(self.sent), 2)
        self.assertIn('学生B：登校済み（09:30:00）', self.sent[1])
        self.assertIn('学生C：10時時点でも未登校', self.sent[1])
        self.assertNotIn('学生A', self.sent[1])
        self.assertNotIn('無効', self.sent[1])
        self.tick('10:02:00')
        self.assertEqual(len(self.sent), 2)

    def test_zero_arrivals_still_followed_up(self):
        self.tick('09:00:00')
        self.assertEqual(self.sent, [])
        self.tick('10:00:00')
        self.assertEqual(len(self.sent), 1)
        self.assertIn('未登校 3名', self.sent[0])

    def test_all_arrived_no_followup(self):
        for sid in ('a', 'b', 'c'):
            self.arrive(sid, '08:59:00')
        self.tick('09:00:00')
        self.tick('10:00:00')
        self.assertEqual(len(self.sent), 1)

    def test_weekend_and_missed_windows(self):
        self.tick('09:00:00', '2026-10-31')
        self.tick('10:00:00', '2026-10-31')
        self.tick('09:10:00')
        self.tick('10:00:00')
        self.assertEqual(self.sent, [])

    def test_retry_uses_frozen_ten_oclock_status(self):
        self.tick('09:00:00')
        def fail(config, body):
            raise DeliveryError('temporary')
        self.reporter.sender = fail
        self.tick('10:00:00')
        self.arrive('b', '10:01:00')
        self.reporter.sender = lambda config, body: self.sent.append(body)
        self.tick('10:04:59')
        self.assertEqual(self.sent, [])
        self.tick('10:05:00')
        self.assertIn('学生B：10時時点でも未登校', self.sent[0])


if __name__ == '__main__':
    unittest.main()
