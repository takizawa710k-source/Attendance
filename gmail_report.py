"""Weekday 09:00 and 10:00 JST attendance email via Google Apps Script HTTPS."""
import json
import os
import hashlib
import urllib.error
import urllib.parse
import urllib.request
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))


def clean(value):
    return ' '.join(str(value).split())


def message_parts(date, rows):
    present = sum(bool(row['arrived_at']) for row in rows)
    lines = [f'【登校状況】{date} 09:00時点',
             f'在籍 {len(rows)}名 ／ 登校 {present}名 ／ 未登校 {len(rows)-present}名']
    groups = {}
    for row in rows:
        groups.setdefault(row['class_name'], []).append(row)
    for name, students in groups.items():
        arrived = sum(bool(row['arrived_at']) for row in students)
        lines.extend(['', f'■ {clean(name)}：登校 {arrived}/{len(students)}名・未登校 {len(students)-arrived}名'])
        pending = [row for row in students if not row['arrived_at']]
        lines.extend(f'・{clean(row["name"])}' for row in pending)
        if not pending:
            lines.append('未登校者なし')
    return ['\n'.join(lines)]


def followup_parts(date, rows):
    if not rows:
        return []
    arrived = sum(bool(row['arrived_at']) for row in rows)
    lines = [f'【登校状況】{date} 10:00時点',
             f'9時に未登校だった学生 {len(rows)}名 ／ 登校済み {arrived}名 ／ 未登校 {len(rows)-arrived}名']
    for row in rows:
        status = ('登校済み（' + row['arrived_at'][11:19] + '）'
                  if row['arrived_at'] else '10時時点でも未登校')
        lines.append(f'・{clean(row["class_name"])} / {clean(row["name"])}：{status}')
    return ['\n'.join(lines)]


class DeliveryError(Exception):
    def __init__(self, message, retry_after=300, permanent=False):
        super().__init__(message)
        self.retry_after = retry_after
        self.permanent = permanent


def endpoint_url(value):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != 'https' or parsed.netloc != 'script.google.com'
            or not parsed.path.startswith('/macros/s/') or not parsed.path.endswith('/exec')
            or parsed.query or parsed.fragment):
        raise ValueError('Invalid Apps Script web app URL')
    return value


def send_message(config, content):
    endpoint, token = config
    # Same snapshot has the same id across retries, including process restarts.
    report_id = hashlib.sha256(content.encode('utf-8')).hexdigest()
    payload = json.dumps({'token': token, 'report_id': report_id,
                          'subject': content.splitlines()[0], 'body': content},
                         ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(endpoint, data=payload,
        headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
        if not result.get('ok'):
            raise DeliveryError('Email relay rejected request',
                                permanent=result.get('error') in ('auth', 'config', 'invalid'))
    except urllib.error.HTTPError as error:
        raise DeliveryError(f'Email relay HTTP {error.code}',
                            permanent=400 <= error.code < 500 and error.code not in (408, 429)) from None
    except (urllib.error.URLError, OSError, ValueError):
        raise DeliveryError('Email relay connection/response error') from None


@contextmanager
def db_session(connect):
    connection = connect()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class Reporter:
    def __init__(self, connect, url, sender=send_message):
        self.connect, self.url, self.sender = lambda: db_session(connect), url, sender
        with self.connect() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS gmail_daily_reports(
                report_date TEXT PRIMARY KEY, state TEXT NOT NULL,
                messages TEXT NOT NULL, next_part INTEGER NOT NULL DEFAULT 0,
                lease_until REAL NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL)''')

            conn.execute("CREATE TABLE IF NOT EXISTS gmail_pending_students("
                         "report_date TEXT NOT NULL,student_id TEXT NOT NULL,"
                         "name TEXT NOT NULL,class_name TEXT NOT NULL,"
                         "PRIMARY KEY(report_date,student_id))")
            conn.execute("CREATE TABLE IF NOT EXISTS gmail_followup_reports("
                         "report_date TEXT PRIMARY KEY,state TEXT NOT NULL,"
                         "messages TEXT NOT NULL,next_part INTEGER NOT NULL DEFAULT 0,"
                         "lease_until REAL NOT NULL DEFAULT 0,retry_at REAL NOT NULL DEFAULT 0,"
                         "updated_at TEXT NOT NULL)")

    def tick(self, now=None):
        now = (now or datetime.now(JST)).astimezone(JST)
        self._tick(now, 9)
        self._tick(now, 10)

    def _tick(self, now, hour):
        table = 'gmail_daily_reports' if hour == 9 else 'gmail_followup_reports'
        now = (now or datetime.now(JST)).astimezone(JST)
        date, stamp = now.strftime('%Y-%m-%d'), now.timestamp()
        if now.weekday() >= 5 or now.hour < hour:
            return
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            job = conn.execute(f'SELECT * FROM {table} WHERE report_date=?', (date,)).fetchone()
            if job is None:
                # A brief restart recovery window; do not post stale reports on afternoon deploys.
                if now.hour != hour or now.minute >= 10:
                    return
                cutoff = date + f' {hour:02d}:00:00'
                if hour == 9:
                    rows = conn.execute("""SELECT s.student_id,s.name,s.class_name,d.arrived_at
                        FROM students s LEFT JOIN daily_attendance d
                        ON s.student_id=d.student_id AND d.attendance_date=? AND d.arrived_at<=?
                        WHERE s.active=1 ORDER BY s.class_name,s.student_id""", (date, cutoff)).fetchall()
                    conn.executemany("INSERT INTO gmail_pending_students VALUES(?,?,?,?)",
                        [(date, r['student_id'], r['name'], r['class_name'])
                         for r in rows if not r['arrived_at']])
                    messages = message_parts(date, rows) if any(r['arrived_at'] for r in rows) else []
                else:
                    rows = conn.execute("""SELECT s.name,s.class_name,d.arrived_at
                        FROM gmail_pending_students s LEFT JOIN daily_attendance d
                        ON s.student_id=d.student_id AND d.attendance_date=? AND d.arrived_at<=?
                        WHERE s.report_date=? ORDER BY s.class_name,s.student_id""",
                        (date, cutoff, date)).fetchall()
                    # The frozen 09:00 report has messages only when someone
                    # arrived by 09:00. Later arrivals must not enable followup.
                    morning = conn.execute(
                        'SELECT messages FROM gmail_daily_reports WHERE report_date=?',
                        (date,)).fetchone()
                    messages = (followup_parts(date, rows)
                                if morning and json.loads(morning['messages']) else [])
                state = 'pending' if messages else 'skipped'
                conn.execute(f'INSERT INTO {table}(report_date,state,messages,updated_at) VALUES(?,?,?,?)',
                             (date, state, json.dumps(messages, ensure_ascii=False), now.isoformat()))
                print(f'[Gmail] {date} {hour:02d}:00: {state}', flush=True)
                job = conn.execute(f'SELECT * FROM {table} WHERE report_date=?', (date,)).fetchone()
            if job['state'] != 'pending' or job['lease_until'] > stamp or job['retry_at'] > stamp:
                return
            conn.execute(f'UPDATE {table} SET lease_until=? WHERE report_date=?', (stamp+120, date))
            messages, index = json.loads(job['messages']), job['next_part']
        # Do network IO outside the DB transaction so student registration keeps working.
        try:
            self.sender(self.url, messages[index])
        except DeliveryError as error:
            with self.connect() as conn:
                conn.execute(f'''UPDATE {table} SET state=?,lease_until=0,retry_at=?,updated_at=?
                    WHERE report_date=?''', ('failed' if error.permanent else 'pending',
                    stamp+error.retry_after, now.isoformat(), date))
            print(f'[Gmail] {date} {hour:02d}:00: {error}', flush=True)
            return
        with self.connect() as conn:
            state = 'sent' if index+1 == len(messages) else 'pending'
            conn.execute(f'''UPDATE {table} SET state=?,next_part=?,lease_until=0,retry_at=0,updated_at=?
                WHERE report_date=?''', (state, index+1, now.isoformat(), date))
        if state == 'sent':
            print(f'[Gmail] {date} {hour:02d}:00: sent ({len(messages)} messages)', flush=True)


def start_reporter(connect):
    endpoint = os.environ.get('GMAIL_RELAY_URL', '').strip()
    token = os.environ.get('GMAIL_RELAY_TOKEN', '').strip()
    if not endpoint or not token:
        print('[Gmail] Disabled: set GMAIL_RELAY_URL and GMAIL_RELAY_TOKEN', flush=True)
        return
    try:
        endpoint_url(endpoint)
        if len(token) < 32:
            raise ValueError('Token too short')
    except ValueError:
        print('[Gmail] Disabled: invalid relay URL or token', flush=True)
        return
    reporter = Reporter(connect, (endpoint, token))

    def run():
        while True:
            try:
                reporter.tick()
            except Exception as error:
                print(f'[Gmail] Report error: {type(error).__name__}', flush=True)
            time.sleep(5)

    threading.Thread(target=run, name='gmail-daily-report', daemon=True).start()
    print('[Gmail] Weekdays 09:00 and 10:00 JST enabled', flush=True)
