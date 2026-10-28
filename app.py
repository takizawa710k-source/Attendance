#!/usr/bin/env python3
from __future__ import annotations
import base64, csv, hashlib, hmac, html, io, json, mimetypes, os, re, secrets, socket, sqlite3, threading, time, unicodedata, urllib.parse, webbrowser, zipfile
from datetime import datetime, timezone, timedelta
from email.parser import BytesParser
from email.policy import default as email_policy
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree as ET
import qrcode
import qrcode.image.svg
from gmail_report import start_reporter

APP_DIR=Path(__file__).resolve().parent
IS_RAILWAY=bool(os.environ.get('RAILWAY_ENVIRONMENT') or os.environ.get('RAILWAY_PROJECT_ID'))
DATA_DIR=Path(os.environ.get('DATA_DIR') or ('/app/data' if IS_RAILWAY else str(APP_DIR/'data')))
PHOTOS_DIR=DATA_DIR/'photos'
SEED_PHOTOS_DIR=APP_DIR/'photos'
DB_PATH=DATA_DIR/'attendance.db'
SECRET_PATH=DATA_DIR/'secret.key'
PORT=int(os.environ.get('PORT') or 8765)
TEACHER_PASSWORD=os.environ.get('TEACHER_PASSWORD','').strip()
JST=timezone(timedelta(hours=9), name='JST')
MAX_UPLOAD=60*1024*1024
SECRET=b''

def now_text(): return datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')
def today_text(): return datetime.now(JST).strftime('%Y-%m-%d')
def get_lan_ip():
    try:
        s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.connect(('8.8.8.8',80)); ip=s.getsockname()[0]; s.close()
        if ip and not ip.startswith('127.'): return ip
    except OSError: pass
    try:
        ip=socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith('127.'): return ip
    except OSError: pass
    return '127.0.0.1'
def base_url(): return f'http://{get_lan_ip()}:{PORT}'

def class_slug(name:str)->str:
    n=unicodedata.normalize('NFKC',name).strip()
    safe=re.sub(r'[^A-Za-z0-9]+','-',n).strip('-')[:32] or 'class'
    if any(ord(c)>127 for c in n) or not re.fullmatch(r'[A-Za-z0-9 _-]+',n):
        safe += '-'+hashlib.sha1(n.encode('utf-8')).hexdigest()[:6]
    return safe

def db():
    c=sqlite3.connect(DB_PATH,timeout=10); c.row_factory=sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON'); c.execute('PRAGMA journal_mode=WAL')
    return c

def init_db():
    DATA_DIR.mkdir(parents=True,exist_ok=True); PHOTOS_DIR.mkdir(parents=True,exist_ok=True)
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS students(
          student_id TEXT PRIMARY KEY,name TEXT NOT NULL,class_name TEXT NOT NULL,class_slug TEXT NOT NULL,
          photo TEXT NOT NULL DEFAULT '',active INTEGER NOT NULL DEFAULT 1,updated_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_students_class ON students(class_slug,active);
        -- Legacy v11 tables are kept so an existing Volume can be upgraded safely.
        CREATE TABLE IF NOT EXISTS attendance_sessions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,class_slug TEXT NOT NULL,started_at TEXT NOT NULL,closed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS attendance(
          session_id INTEGER NOT NULL,student_id TEXT NOT NULL,registered_at TEXT NOT NULL,
          PRIMARY KEY(session_id,student_id),FOREIGN KEY(session_id) REFERENCES attendance_sessions(id),
          FOREIGN KEY(student_id) REFERENCES students(student_id)
        );
        CREATE TABLE IF NOT EXISTS period_defaults(
          class_slug TEXT NOT NULL,period_no INTEGER NOT NULL,start_time TEXT NOT NULL DEFAULT '',end_time TEXT NOT NULL DEFAULT '',
          PRIMARY KEY(class_slug,period_no)
        );
        CREATE TABLE IF NOT EXISTS period_sessions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,class_slug TEXT NOT NULL,session_date TEXT NOT NULL,period_no INTEGER NOT NULL,
          label TEXT NOT NULL,start_time TEXT NOT NULL DEFAULT '',end_time TEXT NOT NULL DEFAULT '',opened_at TEXT NOT NULL DEFAULT '',closed_at TEXT,
          UNIQUE(class_slug,session_date,period_no)
        );
        CREATE INDEX IF NOT EXISTS idx_period_sessions_open ON period_sessions(class_slug,session_date,closed_at);
        CREATE TABLE IF NOT EXISTS period_attendance(
          session_id INTEGER NOT NULL,student_id TEXT NOT NULL,status TEXT NOT NULL DEFAULT '',scan_time TEXT NOT NULL DEFAULT '',
          status_time TEXT NOT NULL DEFAULT '',reason TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL DEFAULT '',source TEXT NOT NULL DEFAULT '',
          PRIMARY KEY(session_id,student_id),FOREIGN KEY(session_id) REFERENCES period_sessions(id),FOREIGN KEY(student_id) REFERENCES students(student_id)
        );
        CREATE TABLE IF NOT EXISTS app_settings(
          key TEXT PRIMARY KEY,value TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS daily_attendance(
          attendance_date TEXT NOT NULL,student_id TEXT NOT NULL,arrived_at TEXT NOT NULL,
          updated_at TEXT NOT NULL DEFAULT '',source TEXT NOT NULL DEFAULT 'qr',
          PRIMARY KEY(attendance_date,student_id),FOREIGN KEY(student_id) REFERENCES students(student_id)
        );
        CREATE INDEX IF NOT EXISTS idx_daily_attendance_date ON daily_attendance(attendance_date,arrived_at);
        ''')
        cols={r[1] for r in c.execute('PRAGMA table_info(students)')}
        if 'class_slug' not in cols: c.execute("ALTER TABLE students ADD COLUMN class_slug TEXT NOT NULL DEFAULT ''")
        if 'active' not in cols: c.execute('ALTER TABLE students ADD COLUMN active INTEGER NOT NULL DEFAULT 1')
        if 'updated_at' not in cols: c.execute("ALTER TABLE students ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
        for r in c.execute("SELECT student_id,class_name FROM students WHERE class_slug='' OR class_slug IS NULL").fetchall():
            c.execute('UPDATE students SET class_slug=? WHERE student_id=?',(class_slug(r['class_name']),r['student_id']))
        if c.execute('SELECT count(*) FROM students').fetchone()[0]==0 and (APP_DIR/'students.csv').exists():
            with (APP_DIR/'students.csv').open(encoding='utf-8-sig',newline='') as f:
                for r in csv.DictReader(f):
                    sid=r.get('student_id','').strip(); name=r.get('name','').strip(); cn=r.get('class','').strip(); photo=r.get('photo','').strip()
                    if sid and name and cn:
                        c.execute('INSERT OR REPLACE INTO students(student_id,name,class_name,class_slug,photo,active,updated_at) VALUES(?,?,?,?,?,1,?)',
                                  (sid,name,cn,class_slug(cn),photo,now_text()))
        for r in c.execute('SELECT DISTINCT class_slug FROM students WHERE active=1').fetchall():
            for n in range(1,5):
                c.execute('INSERT OR IGNORE INTO period_defaults(class_slug,period_no,start_time,end_time) VALUES(?,?,?,?)',(r['class_slug'],n,'',''))
    if SEED_PHOTOS_DIR.exists():
        for fp in SEED_PHOTOS_DIR.iterdir():
            if fp.is_file() and fp.suffix.lower() in {'.jpg','.jpeg','.png','.webp'}:
                dest=PHOTOS_DIR/fp.name
                if not dest.exists():
                    try: dest.write_bytes(fp.read_bytes())
                    except OSError: pass

def load_secret():
    DATA_DIR.mkdir(parents=True,exist_ok=True)
    if not SECRET_PATH.exists(): SECRET_PATH.write_bytes(secrets.token_bytes(32))
    return SECRET_PATH.read_bytes()

def sign(v):
    p=base64.urlsafe_b64encode(v.encode()).decode().rstrip('=')
    return p+'.'+hmac.new(SECRET,p.encode(),hashlib.sha256).hexdigest()
def verify(t):
    if not t or '.' not in t:return None
    p,s=t.rsplit('.',1); e=hmac.new(SECRET,p.encode(),hashlib.sha256).hexdigest()
    if not hmac.compare_digest(s,e):return None
    try:return base64.urlsafe_b64decode(p+'='*(-len(p)%4)).decode()
    except Exception:return None

PERIOD_COUNT=4
STATUS_OPTIONS=('出席','欠席','遅刻','早退','公欠','出席停止')

RINGUAL_LAST_KEY='ringual_last_registered_date'

def get_setting(key,default=''):
    with db() as c:
        r=c.execute('SELECT value FROM app_settings WHERE key=?',(key,)).fetchone()
    return r['value'] if r else default

def set_setting(key,value):
    with db() as c:
        c.execute('''INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?)
                     ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at''',
                  (key,str(value),now_text()))

def parse_date_text(v):
    try:return datetime.strptime((v or '').strip(),'%Y-%m-%d').date()
    except Exception:return None

def date_range_text(start_text,end_text):
    start=parse_date_text(start_text);end=parse_date_text(end_text)
    if not start or not end or end<start:return []
    out=[];d=start
    while d<=end:
        out.append(d.isoformat());d+=timedelta(days=1)
    return out

def class_info(slug):
    with db() as c:
        return c.execute('SELECT class_name,class_slug,count(*) total FROM students WHERE active=1 AND class_slug=? GROUP BY class_name,class_slug',(slug,)).fetchone()

def ensure_period_defaults(slug):
    with db() as c:
        for n in range(1,PERIOD_COUNT+1):
            c.execute('INSERT OR IGNORE INTO period_defaults(class_slug,period_no,start_time,end_time) VALUES(?,?,?,?)',(slug,n,'',''))

def get_period_defaults(slug):
    ensure_period_defaults(slug)
    with db() as c:
        rows=c.execute('SELECT period_no,start_time,end_time FROM period_defaults WHERE class_slug=? ORDER BY period_no',(slug,)).fetchall()
    return {r['period_no']:{'start_time':r['start_time'],'end_time':r['end_time']} for r in rows}

def period_session(slug,period_no,date=None):
    date=date or today_text()
    with db() as c:
        return c.execute('SELECT * FROM period_sessions WHERE class_slug=? AND session_date=? AND period_no=?',(slug,date,int(period_no))).fetchone()

def current_open_session(slug):
    with db() as c:
        return c.execute('SELECT * FROM period_sessions WHERE class_slug=? AND session_date=? AND closed_at IS NULL ORDER BY period_no DESC,id DESC LIMIT 1',(slug,today_text())).fetchone()

def _close_session_in_conn(c,ses):
    if not ses or ses['closed_at'] is not None:return
    ts=now_text()
    students=c.execute('SELECT student_id FROM students WHERE active=1 AND class_slug=?',(ses['class_slug'],)).fetchall()
    for st in students:
        c.execute('''INSERT INTO period_attendance(session_id,student_id,status,scan_time,status_time,reason,updated_at,source)
                     VALUES(?,?,?,'','','',?,?) ON CONFLICT(session_id,student_id) DO NOTHING''',
                  (ses['id'],st['student_id'],'欠席',ts,'auto'))
    c.execute('UPDATE period_sessions SET closed_at=? WHERE id=?',(ts,ses['id']))

def open_period(slug,period_no,start_time='',end_time=''):
    period_no=int(period_no); start_time=(start_time or '').strip(); end_time=(end_time or '').strip(); ts=now_text(); date=today_text()
    with db() as c:
        # Only one QR reception can be open per class. Close another period first.
        for old in c.execute('SELECT * FROM period_sessions WHERE class_slug=? AND session_date=? AND closed_at IS NULL AND period_no<>?',(slug,date,period_no)).fetchall():
            _close_session_in_conn(c,old)
        c.execute('INSERT OR REPLACE INTO period_defaults(class_slug,period_no,start_time,end_time) VALUES(?,?,?,?)',(slug,period_no,start_time,end_time))
        ses=c.execute('SELECT * FROM period_sessions WHERE class_slug=? AND session_date=? AND period_no=?',(slug,date,period_no)).fetchone()
        if ses:
            c.execute('UPDATE period_sessions SET label=?,start_time=?,end_time=?,opened_at=?,closed_at=NULL WHERE id=?',(f'{period_no}限',start_time,end_time,ts,ses['id']))
            sid=ses['id']
        else:
            cur=c.execute('INSERT INTO period_sessions(class_slug,session_date,period_no,label,start_time,end_time,opened_at,closed_at) VALUES(?,?,?,?,?,?,?,NULL)',
                          (slug,date,period_no,f'{period_no}限',start_time,end_time,ts))
            sid=cur.lastrowid
    return sid

def close_period(slug,period_no):
    with db() as c:
        ses=c.execute('SELECT * FROM period_sessions WHERE class_slug=? AND session_date=? AND period_no=?',(slug,today_text(),int(period_no))).fetchone()
        if ses:_close_session_in_conn(c,ses)
    return bool(ses)

def period_data(slug,period_no,public_base=''):
    cl=class_info(slug)
    if not cl:return None
    period_no=int(period_no); defaults=get_period_defaults(slug).get(period_no,{'start_time':'','end_time':''}); ses=period_session(slug,period_no)
    with db() as c:
        ss=c.execute('SELECT student_id,name,photo FROM students WHERE active=1 AND class_slug=? ORDER BY student_id',(slug,)).fetchall()
        records={}
        if ses:
            records={r['student_id']:r for r in c.execute('SELECT * FROM period_attendance WHERE session_id=?',(ses['id'],)).fetchall()}
    students=[]; counts={k:0 for k in STATUS_OPTIONS}; pending=0
    for st in ss:
        rec=records.get(st['student_id']); status=rec['status'] if rec else ''
        if status in counts:counts[status]+=1
        else:pending+=1
        # QRで登録した出席は、押した時刻を管理画面の時刻欄にも表示する。
        # v12以前の既存データで status_time が空でも scan_time から補完する。
        status_time = rec['status_time'] if rec else ''
        if rec and not status_time and rec['status']=='出席' and rec['scan_time']:
            status_time = rec['scan_time'][11:16]
        students.append({'student_id':st['student_id'],'name':st['name'],'photo':st['photo'],'photo_v':photo_version(st['photo']),
                         'status':status,'scan_time':rec['scan_time'] if rec else '','status_time':status_time,
                         'reason':rec['reason'] if rec else '','source':rec['source'] if rec else ''})
    return {'class_name':cl['class_name'],'class_slug':slug,'period_no':period_no,'label':f'{period_no}限','date':today_text(),
            'open':bool(ses and ses['closed_at'] is None),'started':bool(ses),'opened_at':ses['opened_at'] if ses else '',
            'closed_at':ses['closed_at'] if ses else '','start_time':ses['start_time'] if ses else defaults['start_time'],
            'end_time':ses['end_time'] if ses else defaults['end_time'],'total':len(students),'pending':pending,'counts':counts,'students':students,
            'student_url':f'{public_base}/class/{urllib.parse.quote(slug)}' if public_base else ''}

def classes_status():
    with db() as c:
        classes=c.execute('SELECT class_name,class_slug,count(*) total FROM students WHERE active=1 GROUP BY class_name,class_slug ORDER BY class_name').fetchall()
    out=[]
    for cl in classes:
        periods=[]
        for n in range(1,PERIOD_COUNT+1):
            d=period_data(cl['class_slug'],n)
            done=sum(d['counts'].values()) if d else 0
            periods.append({'period_no':n,'open':bool(d and d['open']),'started':bool(d and d['started']),'done':done,
                            'present':0 if not d else d['counts']['出席'],'late':0 if not d else d['counts']['遅刻'],'absent':0 if not d else d['counts']['欠席']})
        out.append({'class_name':cl['class_name'],'class_slug':cl['class_slug'],'total':cl['total'],'periods':periods})
    return out

def update_record(slug,period_no,student_id,status,status_time='',reason=''):
    status=(status or '').strip(); status_time=(status_time or '').strip(); reason=(reason or '').strip()
    if status not in STATUS_OPTIONS:raise ValueError('出欠状態を選択してください。')
    ses=period_session(slug,period_no)
    if not ses:raise ValueError('この時限はまだ開始されていません。')
    with db() as c:
        st=c.execute('SELECT student_id FROM students WHERE student_id=? AND active=1 AND class_slug=?',(student_id,slug)).fetchone()
        if not st:raise ValueError('学生が見つかりません。')
        old=c.execute('SELECT * FROM period_attendance WHERE session_id=? AND student_id=?',(ses['id'],student_id)).fetchone()
        scan=old['scan_time'] if old else ''
        c.execute('''INSERT INTO period_attendance(session_id,student_id,status,scan_time,status_time,reason,updated_at,source)
                     VALUES(?,?,?,?,?,?,?,?)
                     ON CONFLICT(session_id,student_id) DO UPDATE SET status=excluded.status,status_time=excluded.status_time,
                     reason=excluded.reason,updated_at=excluded.updated_at,source='teacher' ''',
                  (ses['id'],student_id,status,scan,status_time,reason,now_text(),'teacher'))

def record_qr(slug,student_id):
    ses=current_open_session(slug)
    if not ses:return None,False,''
    ts=now_text(); qr_time=ts[11:16]; already=False
    with db() as c:
        old=c.execute('SELECT * FROM period_attendance WHERE session_id=? AND student_id=?',(ses['id'],student_id)).fetchone()
        if old and old['status']=='出席':
            already=True; reg=old['scan_time'] or old['updated_at']
            # v12以前にQR登録済みで時刻欄が空の場合は、既存の読取時刻から補完する。
            if not old['status_time'] and reg:
                c.execute('UPDATE period_attendance SET status_time=?,updated_at=? WHERE session_id=? AND student_id=?',
                          (reg[11:16],now_text(),ses['id'],student_id))
        else:
            c.execute('''INSERT INTO period_attendance(session_id,student_id,status,scan_time,status_time,reason,updated_at,source)
                         VALUES(?,?,?,?,?,'',?,?)
                         ON CONFLICT(session_id,student_id) DO UPDATE SET status='出席',scan_time=excluded.scan_time,status_time=excluded.status_time,reason='',updated_at=excluded.updated_at,source='qr' ''',
                      (ses['id'],student_id,'出席',ts,qr_time,ts,'qr'))
            reg=ts
    return ses,already,reg

# ---- v16: one daily arrival scan ----
def daily_qr_token(date_text=None):
    date_text=date_text or today_text()
    digest=hmac.new(SECRET,('daily-arrival:'+date_text).encode(),hashlib.sha256).digest()[:5]
    return base64.urlsafe_b64encode(digest).decode().rstrip('=')

def valid_daily_qr_token(token):
    return bool(token and hmac.compare_digest(str(token),daily_qr_token()))

def record_daily_arrival(student_id,source='qr'):
    ts=now_text();date=today_text()
    with db() as c:
        st=c.execute('SELECT student_id FROM students WHERE student_id=? AND active=1',(student_id,)).fetchone()
        if not st:raise ValueError('在籍学生が見つかりません。')
        cur=c.execute('INSERT OR IGNORE INTO daily_attendance(attendance_date,student_id,arrived_at,updated_at,source) VALUES(?,?,?,?,?)',
                      (date,student_id,ts,ts,source))
        if cur.rowcount==1:return False,ts
        old=c.execute('SELECT arrived_at FROM daily_attendance WHERE attendance_date=? AND student_id=?',(date,student_id)).fetchone()
    return True,old['arrived_at']

def remove_daily_arrival(student_id):
    with db() as c:
        c.execute('DELETE FROM daily_attendance WHERE attendance_date=? AND student_id=?',(today_text(),student_id))

def daily_status_data(date_text=None):
    date_text=date_text or today_text()
    with db() as c:
        rows=c.execute('''SELECT s.student_id,s.name,s.class_name,s.class_slug,s.photo,d.arrived_at,d.source
                          FROM students s LEFT JOIN daily_attendance d
                            ON d.student_id=s.student_id AND d.attendance_date=?
                          WHERE s.active=1
                          ORDER BY s.class_name,CASE WHEN d.arrived_at IS NULL THEN 0 ELSE 1 END,s.student_id''',(date_text,)).fetchall()
    groups=[];by_class={};present=0
    for r in rows:
        if r['class_slug'] not in by_class:
            g={'class_name':r['class_name'],'class_slug':r['class_slug'],'total':0,'present':0,'absent':0,'students':[]}
            by_class[r['class_slug']]=g;groups.append(g)
        g=by_class[r['class_slug']];arrived=r['arrived_at'] or '';is_present=bool(arrived)
        g['total']+=1;g['present']+=int(is_present);present+=int(is_present)
        g['students'].append({'student_id':r['student_id'],'name':r['name'],'photo':r['photo'],
                              'photo_v':photo_version(r['photo']),'arrived_at':arrived,'present':is_present,
                              'source':r['source'] or ''})
    for g in groups:g['absent']=g['total']-g['present']
    return {'date':date_text,'total':len(rows),'present':present,'absent':len(rows)-present,'groups':groups,'updated_at':now_text()}

CSS='''
*{box-sizing:border-box}body{margin:0;background:#f4f6f8;color:#17202a;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Yu Gothic',sans-serif}
a{color:#1769aa}.wrap{max-width:1180px;margin:auto;padding:22px}.card{background:white;border:1px solid #dfe4ea;border-radius:16px;padding:20px;margin-bottom:16px}
.top{display:flex;gap:16px;justify-content:space-between;align-items:flex-start;flex-wrap:wrap}.big{font-size:32px;font-weight:800}.muted{color:#677487}.small{font-size:13px;color:#677487}
button,.btn{border:0;border-radius:10px;padding:12px 17px;font-size:16px;font-weight:700;cursor:pointer;text-decoration:none;display:inline-block}.primary{background:#1769aa;color:white}.danger{background:#b42318;color:white}.secondary{background:#e7edf4;color:#17202a}
.actions{display:flex;gap:8px;flex-wrap:wrap}.pill{display:inline-block;padding:7px 12px;border-radius:99px;font-weight:800}.open{background:#dcfce7;color:#166534}.closed{background:#fee2e2;color:#991b1b}
.stats{display:flex;gap:28px;flex-wrap:wrap}.count{font-size:28px;font-weight:800}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}.classcard{border:1px solid #dde4eb;border-radius:14px;padding:16px;background:white}.student{display:flex;gap:12px;align-items:center;border:1px solid #dde4eb;border-radius:13px;padding:10px}.present{background:#f2fff5;border-color:#91cca0}
.photo{width:66px;height:66px;border-radius:11px;object-fit:cover;background:#eef2f6}.name{font-weight:800}.sid{font-size:13px;color:#677487}.time{font-size:13px;color:#166534;margin-top:4px}.center{max-width:560px;margin:45px auto}.notice{padding:13px;border-radius:10px;background:#fff7d6;border:1px solid #ead58a}.ok{background:#eaf8ef;border-color:#9dd3ae}.error{background:#fff0f0;border-color:#e8a8a8}
input{width:100%;padding:13px;border:1px solid #cbd5e1;border-radius:10px;font-size:16px}.row{margin:13px 0}.studentbtn{width:100%;font-size:22px;padding:18px}.board{min-height:100vh;display:grid;place-items:center;text-align:center;background:white;padding:22px}.qr{width:min(74vw,620px);height:auto;background:white;padding:10px}
table{width:100%;border-collapse:collapse}th,td{border-bottom:1px solid #e4e8ed;text-align:left;padding:9px;vertical-align:middle}th{background:#f7f9fb}.mini{width:42px;height:42px;border-radius:8px;object-fit:cover;background:#eef2f6}.flash{margin-bottom:16px}
.periodnav{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}.periodtab{padding:10px 14px;border-radius:10px;background:#e7edf4;text-decoration:none;color:#17202a;font-weight:800}.periodtab.active{background:#1769aa;color:#fff}.statusgrid{display:grid;grid-template-columns:60px minmax(150px,1fr) 110px 120px minmax(160px,1fr) 75px;gap:7px;align-items:center}.statusrow{padding:10px 0;border-bottom:1px solid #e4e8ed}.statusrow .photo{width:54px;height:54px}.status-select,.time-input,.reason-input{padding:9px;border:1px solid #cbd5e1;border-radius:8px;font-size:14px;width:100%}.savebtn{padding:9px 12px;font-size:14px}.periodbox{border:1px solid #dfe4ea;border-radius:12px;padding:12px;background:#fafbfd}.periodmeta{display:flex;gap:14px;flex-wrap:wrap;align-items:end}.timebox{width:145px}.unstarted{background:#eef2f6;color:#59636e}.status-出席{background:#f2fff5}.status-欠席{background:#fff1f1}.status-遅刻{background:#fff8e5}.status-早退{background:#fff2e8}.status-公欠{background:#eef5ff}.status-出席停止{background:#f4efff}
.summarybar{display:flex;gap:28px;flex-wrap:wrap;align-items:center}.summaryitem .count{line-height:1.05}.attendance-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(245px,1fr));gap:10px}.attendance-card{display:flex;gap:11px;align-items:center;border:2px solid #e1e6eb;border-radius:13px;padding:10px;min-height:88px;background:#fff}.attendance-card.pending{background:#fff5f5;border-color:#efb3b3}.attendance-card.arrived{background:#f1fbf4;border-color:#9dd3ae}.attendance-card .photo{flex:0 0 auto}.attendance-card .student-info{min-width:0;flex:1}.attendance-card .name{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.attendance-card .manualbtn{padding:7px 9px;font-size:12px;white-space:nowrap}.classsection h2{margin:0}.classsummary{font-size:14px;font-weight:700;margin-top:4px}.absent-text{color:#b42318}.arrived-text{color:#166534}.searchbox{max-width:360px}.qr-print{text-align:center;max-width:760px;margin:20px auto;background:#fff}.qr-print .qr{width:110mm;max-width:90vw}.success-photo{width:150px;height:150px;border-radius:20px;object-fit:cover;background:#eef2f6}.success-card{text-align:center}.success-card .student{justify-content:center;border:0}.no-print{display:block}
@media print{@page{size:A4 portrait;margin:14mm}body{background:#fff}.no-print{display:none!important}.qr-print{border:0;margin:0 auto;padding:0}.qr-print .qr{width:110mm}.qr-print .big{font-size:25pt}.qr-print .print-date{font-size:18pt}.qr-print .print-note{font-size:14pt}}
@media(max-width:760px){.statusgrid{grid-template-columns:60px 1fr}.statusgrid>*:nth-child(n+3){grid-column:2}.timebox{width:130px}}
@media(max-width:620px){.wrap{padding:13px}.big{font-size:27px}.card{padding:15px}.photo{width:58px;height:58px}table{font-size:13px}.hide-sm{display:none}}
'''
def page(title,body,script=''):return f"<!doctype html><html lang='ja'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title><style>{CSS}</style></head><body>{body}{script}</body></html>"
def msgbox(text,kind='ok'):return f"<div class='notice {kind} flash'>{html.escape(text)}</div>" if text else ''
def photo_version(photo):
    if not photo:return '0'
    try:return str((PHOTOS_DIR/Path(photo).name).stat().st_mtime_ns)
    except OSError:return '0'
def photo_html(photo,cls='photo'):
    return f"<img class='{cls}' src='/photo/{urllib.parse.quote(photo)}?v={photo_version(photo)}'>" if photo else f"<div class='{cls}'></div>"

def teacher_login(message=''):
    return page('教師ログイン',f"""<div class='center card'><div class='big'>教師ログイン</div><p class='muted'>クラウド版の管理画面です。</p>{msgbox(message,'error') if message else ''}<form method='post' action='/teacher/login'><div class='row'><label>パスワード</label><input autofocus type='password' name='password' required></div><button class='primary' type='submit'>ログイン</button></form></div>""")

def dashboard():
    body=f"""<div class='wrap'>
    <div class='card top'><div><div class='big'>本日の登校状況</div><div class='muted'>{html.escape(today_text())}　全学生をクラスごとに表示します。</div></div>
    <div class='actions'><a class='btn primary' target='_blank' href='/teacher/daily-qr'>本日のQRを印刷</a><a class='btn primary' href='/teacher/barcode'>学生証バーコード受付</a><a class='btn secondary' href='/admin/students'>学生管理</a><a class='btn secondary' href='/teacher/logout'>ログアウト</a></div></div>
    <div class='card'><div class='top'><div class='summarybar'><div class='summaryitem'><div class='muted'>在籍</div><div id='allCount' class='count'>0</div></div><div class='summaryitem'><div class='muted'>登校済み</div><div id='presentCount' class='count arrived-text'>0</div></div><div class='summaryitem'><div class='muted'>未登校・欠席</div><div id='absentCount' class='count absent-text'>0</div></div></div><input id='studentSearch' class='searchbox' placeholder='氏名・学籍番号を検索'></div><div id='lastUpdate' class='small' style='margin-top:10px'></div></div>
    <div id='classGroups'><div class='card'>読み込み中…</div></div></div>"""
    js="""<script>
const esc=s=>String(s??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
let latest=null;
function personCard(s){let pic=s.photo?`<img class="photo" src="/photo/${encodeURIComponent(s.photo)}?v=${encodeURIComponent(s.photo_v||'0')}">`:`<div class="photo"></div>`;let state=s.present?`<div class="time arrived-text">登校 ${esc(s.arrived_at.slice(11,16))}</div>`:`<div class="time absent-text">未登校・欠席</div>`;let action=s.present?'remove':'arrive';let label=s.present?'取消':'手動登録';return `<article class="attendance-card ${s.present?'arrived':'pending'}" data-find="${esc((s.name+' '+s.student_id).toLowerCase())}">${pic}<div class="student-info"><div class="name">${esc(s.name)}</div><div class="sid">${esc(s.student_id)}</div>${state}</div><button class="manualbtn ${s.present?'secondary':'primary'}" data-id="${esc(s.student_id)}" data-action="${action}">${label}</button></article>`}
function render(d){latest=d;document.getElementById('allCount').textContent=d.total;document.getElementById('presentCount').textContent=d.present;document.getElementById('absentCount').textContent=d.absent;document.getElementById('lastUpdate').textContent=`最終更新 ${d.updated_at.slice(11,19)}`;document.getElementById('classGroups').innerHTML=d.groups.length?d.groups.map(g=>`<section class="card classsection"><div class="top"><div><h2>${esc(g.class_name)}</h2><div class="classsummary">在籍 ${g.total}名　<span class="arrived-text">登校 ${g.present}名</span>　<span class="absent-text">未登校・欠席 ${g.absent}名</span></div></div></div><div class="attendance-list">${g.students.map(personCard).join('')}</div></section>`).join(''):'<div class="card">在籍学生が登録されていません。</div>';bind();filter()}
async function load(){let r=await fetch('/api/daily-status',{cache:'no-store'});if(r.ok)render(await r.json())}
function bind(){document.querySelectorAll('.manualbtn').forEach(b=>b.onclick=async()=>{let removing=b.dataset.action==='remove';if(removing&&!confirm('この学生の本日の登校記録を取り消しますか？'))return;let q=new URLSearchParams({action:b.dataset.action});let r=await fetch('/api/daily-record/'+encodeURIComponent(b.dataset.id),{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:q});let d=await r.json();if(!r.ok)alert(d.error||'処理できませんでした');else load()})}
function filter(){let q=document.getElementById('studentSearch').value.trim().toLowerCase();document.querySelectorAll('.attendance-card').forEach(x=>x.style.display=!q||x.dataset.find.includes(q)?'flex':'none')}
document.getElementById('studentSearch').oninput=filter;load();setInterval(()=>{if(document.visibilityState==='visible'&&document.activeElement.id!=='studentSearch')load()},3000);
</script>"""
    return page('登校管理',body,js)

def barcode_page():
    body=f"""<div class='wrap'>
    <div class='card top'><div><div class='big'>学生証バーコード受付</div><div id='scanDate' class='muted'>{html.escape(today_text())}　1日1回の登校確認</div></div><a class='btn secondary' href='/teacher'>登校一覧へ</a></div>
    <div class='card'><p>学生証のバーコードを読み取ってください。学籍番号の手入力もできます。</p>
    <form id='scanForm'><label for='barcodeInput'>学籍番号</label><div class='actions row'><input id='barcodeInput' style='flex:1;min-width:200px' autofocus autocomplete='off' inputmode='numeric' maxlength='128' required><button class='primary' type='submit'>登校登録</button></div></form>
    <div class='small'>リーダーは「USBキーボード入力」「読取後にEnter」の設定で使用してください。</div><div id='queueState' class='small' aria-live='polite'></div></div>
    <div id='scanResult' class='card' role='status' aria-live='polite'>読み取り待ちです。</div>
    <div class='card'><h2>このPCでの受付履歴</h2><p class='small'>この画面を開いている間の直近20件を表示します。</p><div style='overflow:auto'><table><thead><tr><th>学籍番号</th><th>氏名</th><th>結果</th><th>登校時刻</th></tr></thead><tbody id='scanHistory'></tbody></table></div></div></div>"""
    js="""<script>
const csrf=__CSRF__;
const esc=s=>String(s??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const input=document.getElementById('barcodeInput'),result=document.getElementById('scanResult');
const pending=[];let running=false,halted=false;const history=[];
function queueState(){document.getElementById('queueState').textContent=halted?'受付を停止しました。ログインして受付画面を開き直してください。':pending.length?`登録待ち ${pending.length}件`:running?'登録中…':''}
function showResult(sid,d,ok){
 const s=d.student||{},label=ok?(d.already?'登校確認済み':'登校登録しました'):(d.error||'登録できませんでした');
 result.className='card notice '+(ok?'ok':'error');
 const photo=ok&&s.photo?`<img class="success-photo" alt="顔写真" src="/photo/${encodeURIComponent(s.photo)}?v=${encodeURIComponent(s.photo_v)}">`:'';
 result.innerHTML=`<div class="big">${esc(label)}</div><div class="student" style="margin-top:12px">${photo}<div><div class="name" style="font-size:24px">${esc(s.name||'')}</div><div class="sid">${esc(sid)}</div><div>${esc(s.class_name||'')}</div>${ok?`<div class="time" style="font-size:22px">登校時刻 ${esc(d.arrived_at.slice(11,19))}</div>`:''}</div></div>${d.login_required?'<p><a href="/teacher">教師ログインへ</a></p>':''}`;
 if(d.date)document.getElementById('scanDate').textContent=d.date+'　1日1回の登校確認';
 history.unshift({sid,name:s.name||'',label,time:ok?d.arrived_at.slice(11,19):''});history.splice(20);
 document.getElementById('scanHistory').innerHTML=history.map(x=>`<tr><td>${esc(x.sid)}</td><td>${esc(x.name)}</td><td>${esc(x.label)}</td><td>${esc(x.time)}</td></tr>`).join('');
}
async function drain(){
 if(running||halted)return;running=true;queueState();
 while(pending.length&&!halted){
  const sid=pending.shift();queueState();
  try{
   const r=await fetch('/api/barcode-arrival',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({student_id:sid,csrf}),signal:AbortSignal.timeout(15000)});
   const d=await r.json();showResult(sid,d,r.ok);
   if(r.status===401||r.status===403){halted=true;pending.length=0;}
  }catch(e){showResult(sid,{error:'通信を確認できませんでした。同じ学生証をもう一度読み取ってください。'},false)}
 }
 running=false;queueState();
}
document.getElementById('scanForm').addEventListener('submit',e=>{
 e.preventDefault();const sid=input.value.trim();if(!sid||halted)return;
 pending.push(sid);input.value='';input.focus();queueState();drain();
});
document.addEventListener('click',e=>{if(!e.target.closest('a,button,input'))input.focus()});
window.addEventListener('focus',()=>input.focus());input.focus();
</script>""".replace('__CSRF__',json.dumps(sign('barcode-arrival')))
    return page('学生証バーコード受付',body,js)


def daily_qr_print_page(public_base):
    url=f"{public_base}/d/{daily_qr_token()}"
    return page('本日の登校QR',f"""<div class='qr-print card'><div class='no-print actions' style='justify-content:center;margin-bottom:12px'><button class='primary' onclick='window.print()'>印刷する</button><a class='btn secondary' href='/teacher'>登校一覧へ</a></div><div class='big'>登校受付 QRコード</div><div class='print-date' style='margin:8px 0;font-size:24px;font-weight:800'>{html.escape(today_text())}</div><img class='qr' src='/teacher/daily-qr.svg' alt='本日のQRコード'><p class='print-note' style='font-size:20px;font-weight:700'>スマートフォンのカメラで読み取ってください</p><p class='small no-print'>{html.escape(url)}</p></div>""")

def daily_student_register_page(token,message=''):
    return page('端末登録',f"""<div class='center card'><div class='big'>スマホの初回登録</div><p class='muted'>初回だけ学籍番号を入力してください。次回からはQRを開くだけで登校登録されます。</p>{msgbox(message,'error') if message else ''}<form method='post' action='/student/register/{urllib.parse.quote(token)}'><div class='row'><label>学籍番号</label><input autofocus name='student_id' autocomplete='off' required></div><button class='primary studentbtn' type='submit'>このスマホを登録</button></form></div>""")

def daily_done_page(student,arrived_at,already):
    title='本日は登校登録済みです' if already else '登校を受け付けました'
    ph=photo_html(student['photo'],'success-photo')
    return page('登校受付完了',f"""<div class='center card success-card'><div class='big'>✓ {html.escape(title)}</div><div class='student present' style='margin:20px 0'>{ph}<div style='text-align:left'><div class='name' style='font-size:22px'>{html.escape(student['name'])}</div><div class='sid'>{html.escape(student['student_id'])}</div><div class='muted'>{html.escape(student['class_name'])}</div><div class='time' style='font-size:20px'>登校時刻 {html.escape(arrived_at[11:16])}</div></div></div><p class='small'><a href='/student/logout?next={urllib.parse.quote("/d/"+daily_qr_token(),safe="")}'>端末登録を解除</a></p></div>""")

def expired_qr_page():
    return page('QRコードの期限切れ',"<div class='center card'><div class='big'>このQRコードは使用できません</div><p class='muted'>本日のQRコードを読み取ってください。</p></div>")

def ringual_page(message='',kind='ok'):
    last=get_setting(RINGUAL_LAST_KEY,'')
    last_d=parse_date_text(last)
    start=(last_d+timedelta(days=1)).isoformat() if last_d else ''
    today=today_text()
    if last_d:
        main=f"""<div class='card'><h2>RINGUAL 全クラス一括出力</h2><p class='muted'>前回RINGUALへ登録済みの日の翌日から、指定した日までを1ファイルにまとめます。</p>
        <div class='stats' style='margin:18px 0'><div><div class='muted'>前回登録済み</div><div class='count' style='font-size:22px'>{html.escape(last)}</div></div><div><div class='muted'>今回の開始日</div><div class='count' style='font-size:22px'>{html.escape(start)}</div></div></div>
        <form method='get' action='/export/ringual/range.xlsx'><div class='row' style='max-width:300px'><label>今回の終了日</label><input type='date' name='end_date' value='{html.escape(today)}' max='{html.escape(today)}' min='{html.escape(start)}' required></div><button class='primary' type='submit'>全クラス一括Excelを出力</button></form>
        <div class='notice' style='margin-top:18px'>Excelを出力しただけでは「登録済み」にはなりません。RINGUALへのアップロードが正常に完了してから、下のボタンで確定してください。</div>
        <form method='post' action='/teacher/ringual/confirm' onsubmit="return confirm('RINGUALへのアップロードが完了していることを確認しましたか？')"><div class='row' style='max-width:300px'><label>RINGUAL登録完了日</label><input type='date' name='end_date' value='{html.escape(today)}' max='{html.escape(today)}' min='{html.escape(start)}' required></div><button class='primary' type='submit'>この日まで登録完了として記録</button></form></div>"""
    else:
        yesterday=(datetime.now(JST).date()-timedelta(days=1)).isoformat()
        main=f"""<div class='card'><h2>最初に前回登録日を設定</h2><p class='muted'>RINGUALですでに登録済みの最後の日を指定してください。次回から、その翌日が自動的に開始日になります。</p><form method='post' action='/teacher/ringual/set-last'><div class='row' style='max-width:300px'><label>すでにRINGUAL登録済みの最終日</label><input type='date' name='last_date' value='{yesterday}' max='{today}' required></div><button class='primary' type='submit'>最終登録日を設定</button></form></div>"""
    correction=f"""<div class='card'><h2>登録済み日の修正</h2><p class='small'>入力を間違えた場合だけ使ってください。</p><form method='post' action='/teacher/ringual/set-last' onsubmit="return confirm('前回登録済み日を変更しますか？')"><div class='row' style='max-width:300px'><input type='date' name='last_date' value='{html.escape(last or today)}' max='{html.escape(today)}' required></div><button class='secondary' type='submit'>前回登録済み日を修正</button></form></div>""" if last else ''
    return page('RINGUAL一括出力',f"""<div class='wrap'><div class='card top'><div><div class='big'>RINGUAL一括出力</div><div class='muted'>全クラス・複数日を1ファイルにまとめます</div></div><a class='btn secondary' href='/teacher'>クラス一覧へ</a></div>{msgbox(message,kind)}{main}{correction}</div>""")

def teacher_class_page(slug,period_no=1):
    cl=class_info(slug)
    if not cl:return page('クラスなし',"<div class='center card'>クラスが見つかりません。<br><a href='/teacher'>戻る</a></div>")
    period_no=max(1,min(PERIOD_COUNT,int(period_no or 1))); d=period_data(slug,period_no)
    tabs=''.join(f"<a class='periodtab {'active' if n==period_no else ''}' href='/teacher/class/{urllib.parse.quote(slug)}?period={n}'>{n}限</a>" for n in range(1,PERIOD_COUNT+1))
    body=f"""<div class='wrap'><div class='card top'><div><div class='big'>{html.escape(cl['class_name'])} 本日の出欠</div><div class='muted'>QR受付終了後、未登録者は自動で欠席になります。例外だけ教師が修正してください。</div></div><div class='actions'><a class='btn secondary' href='/teacher'>クラス一覧</a><a class='btn secondary' target='_blank' href='/board/{urllib.parse.quote(slug)}'>電子黒板QR</a><a class='btn secondary' href='/export/{urllib.parse.quote(slug)}/today.xlsx'>確認用Excel</a><a class='btn secondary' href='/export/ringual/class/{urllib.parse.quote(slug)}/today.xlsx'>RINGUAL用Excel</a></div></div><div class='periodnav'>{tabs}</div>
    <div class='card'><div class='top'><div><h2>{period_no}限</h2><div id='state' class='pill unstarted'>未実施</div></div><div class='stats'><div><div class='muted'>出席</div><div id='present' class='count'>0</div></div><div><div class='muted'>遅刻</div><div id='late' class='count'>0</div></div><div><div class='muted'>欠席</div><div id='absent' class='count'>0</div></div><div><div class='muted'>未登録</div><div id='pending' class='count'>0</div></div></div></div>
    <div class='periodmeta'><div class='timebox'><label class='small'>標準開始時刻</label><input id='startTime' type='time' value='{html.escape(d['start_time'])}'></div><div class='timebox'><label class='small'>標準終了時刻</label><input id='endTime' type='time' value='{html.escape(d['end_time'])}'></div><div class='actions'><button id='start' class='primary'>この時限のQR受付を開始</button><button id='close' class='danger'>受付終了 → 未登録を欠席</button></div></div><p class='small'>時刻を変更して受付開始すると、その時刻を次回の初期値として記憶します。遅刻・早退はQRでは受け付けず、下の一覧で教師が変更します。</p></div>
    <div class='card'><div class='top'><div><h2>学生一覧</h2><div class='muted'>状態・時刻・理由を修正できます</div></div><button id='refresh' class='btn secondary'>再読込</button></div><div id='students'></div></div></div>"""
    js=f"""<script>
const slug={json.dumps(slug)}, period={period_no};
const esc=s=>String(s??'').replace(/[&<>'"]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}}[c]));
const options={json.dumps(list(STATUS_OPTIONS),ensure_ascii=False)};
function photo(s){{return s.photo?`<img class="photo" src="/photo/${{encodeURIComponent(s.photo)}}?v=${{encodeURIComponent(s.photo_v||'0')}}">`:`<div class="photo"></div>`}}
function row(s){{let st=s.status||'未登録';let opts=(st==='未登録'?`<option value="" selected>未登録</option>`:'')+options.map(x=>`<option ${{x===st?'selected':''}}>${{x}}</option>`).join('');return `<div class="statusrow status-${{esc(st)}} statusgrid" data-id="${{esc(s.student_id)}}">${{photo(s)}}<div><div class="name">${{esc(s.name)}}</div><div class="sid">${{esc(s.student_id)}}</div>${{s.scan_time?`<div class="time">QR ${{esc(s.scan_time.slice(11,19))}}</div>`:''}}</div><select class="status-select">${{opts}}</select><input class="time-input" type="time" value="${{esc(s.status_time)}}" aria-label="時刻"><input class="reason-input" value="${{esc(s.reason)}}" placeholder="理由（例：電車遅延）"><button class="primary savebtn" type="button">保存</button></div>`}}
async function load(){{let r=await fetch(`/api/period-status/${{encodeURIComponent(slug)}}/${{period}}`,{{cache:'no-store'}});if(!r.ok)return;let d=await r.json();let st=document.getElementById('state');st.textContent=d.open?'受付中':(d.started?'受付終了':'未実施');st.className='pill '+(d.open?'open':(d.started?'closed':'unstarted'));document.getElementById('present').textContent=d.counts['出席']||0;document.getElementById('late').textContent=d.counts['遅刻']||0;document.getElementById('absent').textContent=d.counts['欠席']||0;document.getElementById('pending').textContent=d.pending||0;if(document.activeElement===document.body||document.activeElement.id==='refresh')document.getElementById('students').innerHTML=d.students.map(row).join('');bindRows();}}
function bindRows(){{document.querySelectorAll('.statusrow').forEach(el=>{{let sel=el.querySelector('.status-select'),tm=el.querySelector('.time-input'),btn=el.querySelector('.savebtn');sel.onchange=()=>{{if((sel.value==='遅刻'||sel.value==='早退')&&!tm.value){{let n=new Date();tm.value=String(n.getHours()).padStart(2,'0')+':'+String(n.getMinutes()).padStart(2,'0')}}}};btn.onclick=async()=>{{if(!sel.value){{alert('状態を選択してください');return}};let q=new URLSearchParams({{status:sel.value,status_time:tm.value,reason:el.querySelector('.reason-input').value}});let r=await fetch(`/api/update-record/${{encodeURIComponent(slug)}}/${{period}}/${{encodeURIComponent(el.dataset.id)}}`,{{method:'POST',headers:{{'Content-Type':'application/x-www-form-urlencoded'}},body:q}});let d=await r.json();if(!r.ok)alert(d.error||'保存できませんでした');else load()}}}})}}
async function post(url,body=''){{let r=await fetch(url,{{method:'POST',headers:{{'Content-Type':'application/x-www-form-urlencoded'}},body}});let d=await r.json();if(!r.ok)alert(d.error||'処理できませんでした');else load()}}
document.getElementById('start').onclick=()=>{{if(confirm(`${{period}}限のQR受付を開始しますか？`))post(`/api/start-period/${{encodeURIComponent(slug)}}/${{period}}`,new URLSearchParams({{start_time:document.getElementById('startTime').value,end_time:document.getElementById('endTime').value}}))}};
document.getElementById('close').onclick=()=>{{if(confirm('受付を終了し、未登録者を欠席にしますか？'))post(`/api/close-period/${{encodeURIComponent(slug)}}/${{period}}`)}};document.getElementById('refresh').onclick=load;load();setInterval(()=>{{if(!['INPUT','SELECT'].includes(document.activeElement.tagName))load()}},2500);
</script>"""
    return page(cl['class_name']+' 出席',body,js)

def board_page(slug):
    cl=class_info(slug)
    if not cl:return page('クラスなし','not found')
    body=f"""<div class='board'><div><div class='big'>{html.escape(cl['class_name'])} 出席登録</div><div id='period' class='big' style='font-size:25px;margin-top:8px'>受付待機中</div><div id='state' class='pill closed' style='margin:12px'>受付終了</div><div><img class='qr' src='/qr/{urllib.parse.quote(slug)}.svg'></div><div id='count' class='big'>0 / {cl['total']}</div><p class='muted'>先生が受付を開始した時限だけ登録できます</p></div></div>"""
    js=f"""<script>const slug={json.dumps(slug)};async function ref(){{let r=await fetch('/api/board-status/'+encodeURIComponent(slug),{{cache:'no-store'}});let d=await r.json();let s=document.getElementById('state');s.textContent=d.open?'受付中':'受付終了';s.className='pill '+(d.open?'open':'closed');document.getElementById('period').textContent=d.open?d.label:'受付待機中';document.getElementById('count').textContent=d.present+' / '+d.total}}ref();setInterval(ref,1200)</script>"""
    return page(cl['class_name']+' QR',body,js)

def student_login(slug,message=''):
    cl=class_info(slug); cn=cl['class_name'] if cl else slug
    return page('学生登録',f"""<div class='center card'><div class='big'>{html.escape(cn)} 出席登録</div><p class='muted'>初回だけ学籍番号を入力してください。次回からこのスマホで記憶します。</p>{msgbox(message,'error') if message else ''}<form method='post' action='/student/login/{urllib.parse.quote(slug)}'><div class='row'><label>学籍番号</label><input autofocus name='student_id' autocomplete='off' required></div><button class='primary studentbtn' type='submit'>このスマホを登録</button></form></div>""")

def attend_page(slug,student,message=''):
    cl=class_info(slug); cn=cl['class_name'] if cl else slug
    if not cl:return page('クラスなし',"<div class='center card'>クラスが見つかりません。</div>")
    if student and student['class_slug']!=slug:
        return page('クラス違い',f"<div class='center card'><div class='big'>クラスが違います</div><p>{html.escape(student['name'])} さんの登録クラスは {html.escape(student['class_name'])} です。</p><a class='btn secondary' href='/student/logout?next={urllib.parse.quote(slug)}'>端末登録を解除</a></div>")
    ses=current_open_session(slug); ph=photo_html(student['photo'])
    label=ses['label'] if ses else ''
    note=msgbox(message,'error') if message else (f"<div class='notice ok'>{html.escape(label)}の受付中です。</div>" if ses else "<div class='notice'>現在、出席受付は開始されていません。</div>")
    disabled="" if ses else "disabled style='opacity:.45;cursor:not-allowed'"
    return page('出席登録',f"""<div class='center card'><div class='big'>{html.escape(cn)} {html.escape(label)}</div><div class='student' style='margin:18px 0'>{ph}<div><div class='name'>{html.escape(student['name'])}</div><div class='sid'>{html.escape(student['student_id'])}</div></div></div>{note}<form method='post' action='/attend/{urllib.parse.quote(slug)}'><button {disabled} class='primary studentbtn' type='submit'>出席する</button></form><p class='small'>遅刻した場合はQRでは登録せず、先生に申し出てください。</p><p class='small'><a href='/student/logout?next={urllib.parse.quote(slug)}'>この端末の学生登録を解除</a></p></div>""")

def done_page(slug,student,reg,already,ses):
    cl=class_info(slug); cn=cl['class_name'] if cl else slug; text='すでに出席登録済みです' if already else '出席を登録しました'
    return page('登録完了',f"""<div class='center card'><div class='big'>✓ {html.escape(text)}</div><div class='muted' style='margin-top:6px'>{html.escape(cn)} {html.escape(ses['label'])}</div><div class='student present' style='margin:20px 0'>{photo_html(student['photo'])}<div><div class='name'>{html.escape(student['name'])}</div><div class='sid'>{html.escape(student['student_id'])}</div><div class='time'>{html.escape(reg)}</div></div></div></div>""")

def admin_students(message='',kind='ok'):
    with db() as c: rows=c.execute('SELECT * FROM students WHERE active=1 ORDER BY class_name,student_id').fetchall()
    trs=[f"<tr><td>{photo_html(s['photo'],'mini')}</td><td>{html.escape(s['student_id'])}</td><td>{html.escape(s['name'])}</td><td>{html.escape(s['class_name'])}</td><td class='hide-sm'>{html.escape(s['photo'])}</td></tr>" for s in rows]
    return page('学生管理',f"""<div class='wrap'><div class='card top'><div><div class='big'>学生管理</div><div class='muted'>名簿と顔写真はここから更新できます</div></div><a class='btn secondary' href='/teacher'>登校一覧へ</a></div>{msgbox(message,kind)}<div class='grid'><div class='card'><h2>① 名簿Excelをアップロード</h2><p class='small'>.xlsx または .csv。同じ学籍番号は更新します。</p><form method='post' action='/admin/upload-students' enctype='multipart/form-data'><div class='row'><input type='file' name='file' accept='.xlsx,.csv' required></div><label><input style='width:auto' type='checkbox' name='replace' value='1' checked> このExcelを現在の在籍名簿として扱う（ファイルにない学生は非表示）</label><div class='row'><button class='primary' type='submit'>名簿を取り込む</button></div></form></div><div class='card'><h2>② 顔写真ZIPをアップロード</h2><p class='small'>JPG / PNG / WEBP。写真ファイル名を名簿と合わせます。</p><form method='post' action='/admin/upload-photos' enctype='multipart/form-data'><div class='row'><input type='file' name='file' accept='.zip' required></div><button class='primary' type='submit'>写真ZIPを取り込む</button></form></div></div><div class='card'><h2>在籍学生 {len(rows)}名</h2><div style='overflow:auto'><table><thead><tr><th>写真</th><th>学籍番号</th><th>氏名</th><th>クラス</th><th class='hide-sm'>写真ファイル</th></tr></thead><tbody>{''.join(trs)}</tbody></table></div></div></div>""")

# ---- XLSX / CSV import ----
def col_idx(ref):
    m=re.match(r'([A-Z]+)',ref.upper()); n=0
    if not m:return 0
    for ch in m.group(1):n=n*26+(ord(ch)-64)
    return n-1

def read_xlsx_rows(data):
    ns={'a':'http://schemas.openxmlformats.org/spreadsheetml/2006/main','r':'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names=set(z.namelist()); shared=[]
        if 'xl/sharedStrings.xml' in names:
            root=ET.fromstring(z.read('xl/sharedStrings.xml'))
            # Excel may store Japanese phonetic guides (furigana) in <rPh>.
            # They are metadata, not part of the visible cell value, so ignore them.
            for si in root.findall('a:si',ns):
                parts=[]
                direct=si.find('a:t',ns)
                if direct is not None:
                    parts.append(direct.text or '')
                for rich in si.findall('a:r',ns):
                    t=rich.find('a:t',ns)
                    if t is not None: parts.append(t.text or '')
                shared.append(''.join(parts))
        wb=ET.fromstring(z.read('xl/workbook.xml')); sh=wb.find('a:sheets',ns)[0]; rid=sh.attrib['{%s}id'%ns['r']]
        relns={'p':'http://schemas.openxmlformats.org/package/2006/relationships'}; rel=ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
        target={x.attrib['Id']:x.attrib['Target'] for x in rel.findall('p:Relationship',relns)}[rid]; target=('xl/'+target.lstrip('/')) if not target.startswith('xl/') else target
        root=ET.fromstring(z.read(target)); rows=[]
        for row in root.findall('.//a:sheetData/a:row',ns):
            vals={}
            for c in row.findall('a:c',ns):
                idx=col_idx(c.attrib.get('r','A1')); typ=c.attrib.get('t'); v=c.find('a:v',ns); isel=c.find('a:is',ns)
                if typ=='s' and v is not None:val=shared[int(v.text)]
                elif typ=='inlineStr' and isel is not None:
                    parts=[]
                    direct=isel.find('a:t',ns)
                    if direct is not None: parts.append(direct.text or '')
                    for rich in isel.findall('a:r',ns):
                        t=rich.find('a:t',ns)
                        if t is not None: parts.append(t.text or '')
                    val=''.join(parts)
                elif v is not None:val=v.text or ''
                else:val=''
                vals[idx]=val
            if vals:rows.append([str(vals.get(i,'')).strip() for i in range(max(vals)+1)])
        return rows

def norm_header(x):return re.sub(r'\s+','',unicodedata.normalize('NFKC',str(x)).strip().lower())
def rows_to_students(rows):
    if not rows:raise ValueError('Excelが空です。')
    headers=[norm_header(x) for x in rows[0]]
    def find(names):
        for i,h in enumerate(headers):
            if h in names:return i
        # Be tolerant of Excel-added metadata/annotations after a known header.
        for i,h in enumerate(headers):
            if any(h.startswith(n) for n in names if len(n)>=2): return i
        return None
    iid=find({'学籍番号','申請番号','studentid','student_id','id'}); ina=find({'氏名','正式氏名(英字)','name','氏名(英字)'}); icl=find({'クラス','class','classname','class_name'}); iph=find({'顔写真ファイル名','写真ファイル名','顔写真','写真','photo','photofile','photo_filename'})
    if iph is None:
        maxc=max((len(r) for r in rows[1:15]),default=0)
        for j in range(maxc):
            vals=[r[j].strip() for r in rows[1:15] if j<len(r) and r[j].strip()]
            if vals and sum(bool(re.search(r'\.(?:jpe?g|png|webp|jp)$',v,re.I)) for v in vals)>=max(1,len(vals)//2):iph=j;break
    if iid is None or ina is None or icl is None:raise ValueError('Excelには「学籍番号（または申請番号）」「氏名」「クラス」の列が必要です。')
    out=[]
    for r in rows[1:]:
        g=lambda i:r[i].strip() if i is not None and i<len(r) else ''
        sid,name,cn,photo=g(iid),g(ina),g(icl),g(iph)
        if sid and name and cn:out.append({'student_id':sid,'name':name,'class_name':cn,'photo':photo})
    if not out:raise ValueError('取り込める学生データがありませんでした。')
    return out

def import_students(data,filename,replace):
    rows=list(csv.reader(io.StringIO(data.decode('utf-8-sig','replace')))) if filename.lower().endswith('.csv') else read_xlsx_rows(data)
    students=rows_to_students(rows); ts=now_text()
    with db() as c:
        if replace:c.execute('UPDATE students SET active=0')
        for s in students:
            old=c.execute('SELECT photo FROM students WHERE student_id=?',(s['student_id'],)).fetchone(); photo=s['photo'] or (old['photo'] if old else '')
            if photo.lower().endswith('.jp'):photo+='g'
            c.execute("""INSERT INTO students(student_id,name,class_name,class_slug,photo,active,updated_at) VALUES(?,?,?,?,?,1,?)
            ON CONFLICT(student_id) DO UPDATE SET name=excluded.name,class_name=excluded.class_name,class_slug=excluded.class_slug,photo=excluded.photo,active=1,updated_at=excluded.updated_at""",
            (s['student_id'],s['name'],s['class_name'],class_slug(s['class_name']),photo,ts))
    return len(students)

def import_photos(data):
    count=0
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            if info.is_dir():continue
            name=Path(info.filename).name
            if not name or Path(name).suffix.lower() not in {'.jpg','.jpeg','.png','.webp'}:continue
            raw=z.read(info)
            if len(raw)>8*1024*1024:continue
            (PHOTOS_DIR/name).write_bytes(raw);count+=1
    return count

def xml_escape(v):return html.escape(str(v),quote=False)
def export_rows(slug):
    cl=class_info(slug)
    if not cl:return []
    with db() as c:ss=c.execute('SELECT student_id,name FROM students WHERE active=1 AND class_slug=? ORDER BY student_id',(slug,)).fetchall()
    allp={}
    with db() as c:
        for n in range(1,PERIOD_COUNT+1):
            ses=c.execute('SELECT * FROM period_sessions WHERE class_slug=? AND session_date=? AND period_no=?',(slug,today_text(),n)).fetchone()
            allp[n]={} if not ses else {r['student_id']:r for r in c.execute('SELECT * FROM period_attendance WHERE session_id=?',(ses['id'],)).fetchall()}
    headers=['日付','クラス','学籍番号','氏名']
    for n in range(1,PERIOD_COUNT+1):headers += [f'{n}限',f'{n}限時刻',f'{n}限理由']
    rows=[headers]
    for st in ss:
        row=[today_text(),cl['class_name'],st['student_id'],st['name']]
        for n in range(1,PERIOD_COUNT+1):
            r=allp[n].get(st['student_id'])
            tm=''
            if r:
                tm=r['status_time'] or (r['scan_time'][11:16] if r['status']=='出席' and r['scan_time'] else '')
            row += [r['status'] if r else '',tm,r['reason'] if r else '']
        rows.append(row)
    return rows


RINGUAL_STATUS_MAP={'出席':'出','欠席':'欠','遅刻':'遅','早退':'早','公欠':'公','出席停止':'停'}

def _hhmm_minutes(v):
    try:
        h,m=map(int,(v or '').split(':')[:2])
        if 0<=h<=23 and 0<=m<=59:return h*60+m
    except Exception:pass
    return None

def _ringual_duration(status,status_time,start_time,end_time):
    t=_hhmm_minutes(status_time)
    if t is None:return ''
    if status=='遅刻':
        s=_hhmm_minutes(start_time)
        return '' if s is None else max(0,t-s)
    if status=='早退':
        e=_hhmm_minutes(end_time)
        return '' if e is None else max(0,e-t)
    return ''

def _ringual_time_fraction(v):
    m=_hhmm_minutes(v)
    return None if m is None else m/(24*60)

def ringual_export_data(slug=None,date=None):
    date=date or today_text()
    with db() as c:
        if slug:
            students=c.execute('SELECT student_id,name,class_name,class_slug FROM students WHERE active=1 AND class_slug=? ORDER BY student_id',(slug,)).fetchall()
        else:
            students=c.execute('SELECT student_id,name,class_name,class_slug FROM students WHERE active=1 ORDER BY class_name,student_id').fetchall()
        session_cache={}
        attendance_cache={}
        rows=[]
        for st in students:
            out=[st['student_id']]
            for n in range(1,PERIOD_COUNT+1):
                key=(st['class_slug'],n)
                if key not in session_cache:
                    ses=c.execute('SELECT * FROM period_sessions WHERE class_slug=? AND session_date=? AND period_no=?',
                                  (st['class_slug'],date,n)).fetchone()
                    session_cache[key]=ses
                    attendance_cache[key]={} if not ses else {r['student_id']:r for r in c.execute(
                        'SELECT * FROM period_attendance WHERE session_id=?',(ses['id'],)).fetchall()}
                ses=session_cache[key]
                rec=attendance_cache[key].get(st['student_id'])
                if not rec or not rec['status']:
                    out += ['', '', None, '']
                    continue
                status=rec['status']
                code=RINGUAL_STATUS_MAP.get(status,'')
                tm=(rec['status_time'] or '').strip()
                dur=_ringual_duration(status,tm,ses['start_time'] if ses else '',ses['end_time'] if ses else '')
                clock=_ringual_time_fraction(tm) if status in ('遅刻','早退') else None
                out += [code,dur,clock,rec['reason'] or '']
            rows.append(out)
    return rows

def ringual_export_range_data(start_date,end_date):
    dates=date_range_text(start_date,end_date)
    if not dates:return [],[]
    with db() as c:
        # 現在の在籍者に加え、この期間に出欠記録がある学生も含める。
        ids={r['student_id'] for r in c.execute('SELECT student_id FROM students WHERE active=1').fetchall()}
        ids.update(r['student_id'] for r in c.execute('''SELECT DISTINCT pa.student_id FROM period_attendance pa
                 JOIN period_sessions ps ON ps.id=pa.session_id WHERE ps.session_date BETWEEN ? AND ?''',(start_date,end_date)).fetchall())
        student_ids=sorted(ids)
        recs={}
        q='''SELECT ps.id session_id,ps.session_date,ps.period_no,ps.start_time,ps.end_time,
                    pa.student_id,pa.status,pa.scan_time,pa.status_time,pa.reason,pa.updated_at
             FROM period_sessions ps JOIN period_attendance pa ON pa.session_id=ps.id
             WHERE ps.session_date BETWEEN ? AND ? ORDER BY ps.id'''
        for r in c.execute(q,(start_date,end_date)).fetchall():
            recs[(r['student_id'],r['session_date'],int(r['period_no']))]=r
        rows=[]
        for sid in student_ids:
            out=[sid]
            for date in dates:
                for n in range(1,PERIOD_COUNT+1):
                    rec=recs.get((sid,date,n))
                    if not rec or not rec['status']:
                        out += ['', '', None, '']
                        continue
                    status=rec['status']; code=RINGUAL_STATUS_MAP.get(status,'')
                    tm=(rec['status_time'] or '').strip()
                    dur=_ringual_duration(status,tm,rec['start_time'] or '',rec['end_time'] or '')
                    clock=_ringual_time_fraction(tm) if status in ('遅刻','早退') else None
                    out += [code,dur,clock,rec['reason'] or '']
            rows.append(out)
    return rows,dates

def make_ringual_range_xlsx(rows,dates):
    # RINGUAL「日別出欠登録」形式を複数日横並びで作る。
    # 各日4時限 ×（状態・遅刻早退時間・入退室時刻・出欠メモ）の16列。
    def col_letter(n):
        s=''
        while n:
            n,rem=divmod(n-1,26);s=chr(65+rem)+s
        return s
    def str_cell(ref,val,style=''):
        st=f' s="{style}"' if style!='' else ''
        return f'<c r="{ref}" t="inlineStr"{st}><is><t>{xml_escape(val)}</t></is></c>'
    def num_cell(ref,val,style=''):
        st=f' s="{style}"' if style!='' else ''
        return f'<c r="{ref}"{st}><v>{val}</v></c>'
    headers=['学籍番号']
    for _date in dates:
        for n in range(1,PERIOD_COUNT+1):
            headers += [f'{n}限',f'{n}限_遅刻早退時間',f'{n}限_入退室時刻',f'{n}限_出欠メモ']
    maxcol=len(headers);last=col_letter(maxcol)
    row1=[];merges=[]
    for di,date_text in enumerate(dates):
        d=datetime.strptime(date_text,'%Y-%m-%d')
        serial=(d-datetime(1899,12,30)).days
        start_col=2+di*PERIOD_COUNT*4; end_col=start_col+PERIOD_COUNT*4-1
        row1.append(num_cell(f'{col_letter(start_col)}1',serial,'2'))
        merges.append(f'<mergeCell ref="{col_letter(start_col)}1:{col_letter(end_col)}1"/>')
    sheet_rows=['<row r="1">'+''.join(row1)+'</row>']
    sheet_rows.append('<row r="2">'+''.join(str_cell(f'{col_letter(i)}2',v,'1') for i,v in enumerate(headers,1))+'</row>')
    for ri,row in enumerate(rows,3):
        cells=[]
        for ci,val in enumerate(row,1):
            if val is None or val=='':continue
            ref=f'{col_letter(ci)}{ri}'
            within=(ci-2)%4 if ci>=2 else -1
            if ci>=2 and within==2 and isinstance(val,(int,float)):cells.append(num_cell(ref,repr(float(val)),'3'))
            elif isinstance(val,(int,float)):cells.append(num_cell(ref,val))
            else:cells.append(str_cell(ref,val))
        sheet_rows.append(f'<row r="{ri}">'+''.join(cells)+'</row>')
    cols=['<col min="1" max="1" width="18" customWidth="1"/>']
    for di in range(len(dates)):
        for n in range(PERIOD_COUNT):
            base=2+di*PERIOD_COUNT*4+n*4
            cols += [f'<col min="{base}" max="{base}" width="7" customWidth="1"/>',
                     f'<col min="{base+1}" max="{base+1}" width="18" customWidth="1"/>',
                     f'<col min="{base+2}" max="{base+2}" width="16" customWidth="1"/>',
                     f'<col min="{base+3}" max="{base+3}" width="24" customWidth="1"/>']
    sheet_xml=('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
      '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
      f'<dimension ref="A1:{last}{max(2,len(rows)+2)}"/>'
      '<sheetViews><sheetView workbookViewId="0"><pane ySplit="2" topLeftCell="A3" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
      '<sheetFormatPr defaultRowHeight="15"/><cols>'+''.join(cols)+'</cols><sheetData>'+''.join(sheet_rows)+'</sheetData>'
      + (f'<mergeCells count="{len(merges)}">'+''.join(merges)+'</mergeCells>' if merges else '') + '</worksheet>')
    styles='''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="2"><numFmt numFmtId="164" formatCode="yyyy/m/d"/><numFmt numFmtId="165" formatCode="h:mm"/></numFmts>
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FFE7EDF4"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="4"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/><xf numFmtId="164" fontId="1" fillId="2" borderId="0" xfId="0" applyNumberFormat="1" applyFont="1" applyFill="1"/><xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''
    files={
      '[Content_Types].xml':'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/><Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/><Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/></Types>',
      '_rels/.rels':'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/></Relationships>',
      'xl/workbook.xml':'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="日別出欠登録" sheetId="1" r:id="rId1"/></sheets></workbook>',
      'xl/_rels/workbook.xml.rels':'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>',
      'xl/worksheets/sheet1.xml':sheet_xml,'xl/styles.xml':styles,
      'docProps/core.xml':'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>RINGUAL 日別出欠登録</dc:title><dc:creator>QR Attendance</dc:creator></cp:coreProperties>',
      'docProps/app.xml':'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>QR Attendance</Application></Properties>'}
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        for name,data in files.items():z.writestr(name,data.encode('utf-8'))
    return out.getvalue()

def make_ringual_xlsx(rows,date_text):
    # RINGUAL「日別出欠登録」形式: 1行目=日付、2行目=項目名。
    # 各時限に、状態・遅刻早退時間(分)・入退室時刻・出欠メモを用意する。
    def col_letter(n):
        s=''
        while n:
            n,rem=divmod(n-1,26);s=chr(65+rem)+s
        return s
    def str_cell(ref,val,style=''):
        st=f' s="{style}"' if style!='' else ''
        return f'<c r="{ref}" t="inlineStr"{st}><is><t>{xml_escape(val)}</t></is></c>'
    def num_cell(ref,val,style=''):
        st=f' s="{style}"' if style!='' else ''
        return f'<c r="{ref}"{st}><v>{val}</v></c>'
    try:
        d=datetime.strptime(date_text,'%Y-%m-%d')
    except ValueError:
        d=datetime.now(JST).replace(tzinfo=None)
    excel_date=(d-datetime(1899,12,30)).days
    headers=['学籍番号']
    for n in range(1,PERIOD_COUNT+1):
        headers += [f'{n}限',f'{n}限_遅刻早退時間',f'{n}限_入退室時刻',f'{n}限_出欠メモ']
    maxcol=len(headers);last=col_letter(maxcol)
    sheet_rows=[]
    sheet_rows.append('<row r="1">'+num_cell('B1',excel_date,'2')+'</row>')
    sheet_rows.append('<row r="2">'+''.join(str_cell(f'{col_letter(i)}2',v,'1') for i,v in enumerate(headers,1))+'</row>')
    for ri,row in enumerate(rows,3):
        cells=[]
        for ci,val in enumerate(row,1):
            if val is None or val=='':
                continue
            ref=f'{col_letter(ci)}{ri}'
            within=(ci-2)%4 if ci>=2 else -1
            if ci>=2 and within==2 and isinstance(val,(int,float)):
                cells.append(num_cell(ref,repr(float(val)),'3'))
            elif isinstance(val,(int,float)):
                cells.append(num_cell(ref,val))
            else:
                cells.append(str_cell(ref,val))
        sheet_rows.append(f'<row r="{ri}">'+''.join(cells)+'</row>')
    cols=['<col min="1" max="1" width="18" customWidth="1"/>']
    for n in range(1,PERIOD_COUNT+1):
        base=2+(n-1)*4
        cols += [
            f'<col min="{base}" max="{base}" width="7" customWidth="1"/>',
            f'<col min="{base+1}" max="{base+1}" width="18" customWidth="1"/>',
            f'<col min="{base+2}" max="{base+2}" width="16" customWidth="1"/>',
            f'<col min="{base+3}" max="{base+3}" width="24" customWidth="1"/>',
        ]
    merge=f'B1:{last}1'
    sheet_xml=(
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
      '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
      f'<dimension ref="A1:{last}{max(2,len(rows)+2)}"/>'
      '<sheetViews><sheetView workbookViewId="0"><pane ySplit="2" topLeftCell="A3" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
      '<sheetFormatPr defaultRowHeight="15"/>'
      '<cols>'+''.join(cols)+'</cols>'
      '<sheetData>'+''.join(sheet_rows)+'</sheetData>'
      f'<mergeCells count="1"><mergeCell ref="{merge}"/></mergeCells>'
      '</worksheet>'
    )
    styles="""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="2"><numFmt numFmtId="164" formatCode="yyyy/m/d"/><numFmt numFmtId="165" formatCode="h:mm"/></numFmts>
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FFE7EDF4"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="4"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/><xf numFmtId="164" fontId="1" fillId="2" borderId="0" xfId="0" applyNumberFormat="1" applyFont="1" applyFill="1"/><xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""
    files={
      '[Content_Types].xml':'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/><Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/><Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/></Types>',
      '_rels/.rels':'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/></Relationships>',
      'xl/workbook.xml':'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="日別出欠登録" sheetId="1" r:id="rId1"/></sheets></workbook>',
      'xl/_rels/workbook.xml.rels':'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>',
      'xl/worksheets/sheet1.xml':sheet_xml,
      'xl/styles.xml':styles,
      'docProps/core.xml':'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>RINGUAL 日別出欠登録</dc:title><dc:creator>QR Attendance</dc:creator></cp:coreProperties>',
      'docProps/app.xml':'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>QR Attendance</Application></Properties>'
    }
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        for name,data in files.items():z.writestr(name,data.encode('utf-8'))
    return out.getvalue()

def make_xlsx(rows):
    def col_letter(n):
        out=''
        while n:n,rem=divmod(n-1,26);out=chr(65+rem)+out
        return out
    maxcol=max((len(r) for r in rows),default=1); last=col_letter(maxcol)
    sheet=[]
    for ri,row in enumerate(rows,1):
        cells=[]
        for ci,val in enumerate(row,1):
            ref=f'{col_letter(ci)}{ri}';style=' s="1"' if ri==1 else ''
            cells.append(f'<c r="{ref}" t="inlineStr"{style}><is><t>{xml_escape(val)}</t></is></c>')
        sheet.append(f'<row r="{ri}">'+''.join(cells)+'</row>')
    cols=[]
    for ci in range(1,maxcol+1):
        width=34 if ci==4 else (13 if ci in (1,3) else (10 if ci==2 else (12 if (ci-5)%3==0 else 18)))
        cols.append(f'<col min="{ci}" max="{ci}" width="{width}" customWidth="1"/>')
    sheet_xml='<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><sheetFormatPr defaultRowHeight="15"/><cols>'+''.join(cols)+'</cols><sheetData>'+''.join(sheet)+f'</sheetData><autoFilter ref="A1:{last}{len(rows)}"/></worksheet>'
    files={'[Content_Types].xml':'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/><Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/><Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/></Types>',
    '_rels/.rels':'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/></Relationships>',
    'xl/workbook.xml':'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="本日の出席" sheetId="1" r:id="rId1"/></sheets></workbook>',
    'xl/_rels/workbook.xml.rels':'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>',
    'xl/worksheets/sheet1.xml':sheet_xml,
    'xl/styles.xml':'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF1769AA"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>',
    'docProps/core.xml':'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>本日の出席</dc:title><dc:creator>QR Attendance</dc:creator></cp:coreProperties>',
    'docProps/app.xml':'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>QR Attendance</Application></Properties>'}
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        for n,d in files.items():z.writestr(n,d.encode('utf-8'))
    return out.getvalue()

class Handler(BaseHTTPRequestHandler):
    def log_message(self,fmt,*args):print(f"[{datetime.now(JST).strftime('%H:%M:%S')}] {self.client_address[0]} - {fmt%args}")
    def ck(self):
        j=cookies.SimpleCookie()
        try:j.load(self.headers.get('Cookie',''))
        except cookies.CookieError:return {}
        return {k:v.value for k,v in j.items()}
    def request_base_url(self):
        host=(self.headers.get('X-Forwarded-Host') or self.headers.get('Host') or '').split(',')[0].strip();proto=(self.headers.get('X-Forwarded-Proto') or '').split(',')[0].strip()
        if not proto:proto='https' if IS_RAILWAY else 'http'
        return f'{proto}://{host}' if host else base_url()
    def is_https(self):return self.request_base_url().startswith('https://')
    def cookie(self,name,value,max_age):return f'{name}={value}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Lax'+('; Secure' if self.is_https() else '')
    def teacher_ok(self):
        if not IS_RAILWAY and not TEACHER_PASSWORD:return True
        return bool(TEACHER_PASSWORD and verify(self.ck().get('teacher_session'))=='teacher')
    def student(self):
        sid=verify(self.ck().get('student_session'))
        if not sid:return None
        with db() as c:return c.execute('SELECT * FROM students WHERE student_id=? AND active=1',(sid,)).fetchone()
    def send_html(self,x,status=200,headers=()):
        b=x.encode();self.send_response(status);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(b)));self.send_header('Cache-Control','no-store')
        for k,v in headers:self.send_header(k,v)
        self.end_headers();self.wfile.write(b)
    def send_json(self,x,status=200):
        b=json.dumps(x,ensure_ascii=False).encode();self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(b)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(b)
    def redirect(self,loc,headers=()):
        self.send_response(303);self.send_header('Location',loc)
        for k,v in headers:self.send_header(k,v)
        self.end_headers()
    def form(self):
        n=min(int(self.headers.get('Content-Length','0') or 0),100000);return {k:v[0] for k,v in urllib.parse.parse_qs(self.rfile.read(n).decode('utf-8','replace'),keep_blank_values=True).items()}
    def multipart(self):
        n=int(self.headers.get('Content-Length','0') or 0)
        if n<=0 or n>MAX_UPLOAD:raise ValueError('アップロードファイルが大きすぎます。')
        ct=self.headers.get('Content-Type','');raw=self.rfile.read(n);msg=BytesParser(policy=email_policy).parsebytes((f'Content-Type: {ct}\r\nMIME-Version: 1.0\r\n\r\n').encode()+raw)
        if not msg.is_multipart():raise ValueError('アップロード形式を読み取れません。')
        fields={};files={}
        for part in msg.iter_parts():
            name=part.get_param('name',header='content-disposition');fn=part.get_filename();data=part.get_payload(decode=True) or b''
            if not name:continue
            if fn:files[name]=(Path(fn).name,data)
            else:fields[name]=data.decode('utf-8','replace')
        return fields,files
    def require_teacher(self):
        if self.teacher_ok():return True
        self.redirect('/teacher');return False
    def send_photo(self,name):
        safe=Path(name).name;fp=PHOTOS_DIR/safe
        if safe!=name or not fp.is_file():return self.send_html('not found',404)
        st=self.student()
        if not self.teacher_ok() and (not st or st['photo']!=safe):return self.send_html('forbidden',403)
        b=fp.read_bytes();self.send_response(200);self.send_header('Content-Type',mimetypes.guess_type(safe)[0] or 'application/octet-stream');self.send_header('Content-Length',str(len(b)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(b)
    def do_GET(self):
        u=urllib.parse.urlparse(self.path);p=u.path;qs=urllib.parse.parse_qs(u.query)
        if p=='/':return self.redirect('/teacher')
        if p=='/teacher':return self.send_html(dashboard() if self.teacher_ok() else teacher_login('' if TEACHER_PASSWORD else '教師パスワードが未設定です。'))
        if p=='/teacher/logout':return self.redirect('/teacher',[('Set-Cookie',self.cookie('teacher_session','',0))])
        if p=='/teacher/barcode':
            if not self.require_teacher():return
            return self.send_html(barcode_page())
        if p=='/teacher/daily-qr':
            if not self.require_teacher():return
            return self.send_html(daily_qr_print_page(self.request_base_url()))
        if p=='/teacher/daily-qr.svg':
            if not self.require_teacher():return
            qr=qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,box_size=12,border=4)
            qr.add_data(f'{self.request_base_url()}/d/{daily_qr_token()}');qr.make(fit=True)
            im=qr.make_image(image_factory=qrcode.image.svg.SvgPathImage);o=io.BytesIO();im.save(o);b=o.getvalue()
            self.send_response(200);self.send_header('Content-Type','image/svg+xml');self.send_header('Content-Length',str(len(b)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(b);return
        if p=='/api/daily-status':
            if not self.teacher_ok():return self.send_json({'error':'unauthorized'},401)
            return self.send_json(daily_status_data())
        if p.startswith('/d/'):
            token=urllib.parse.unquote(p[len('/d/'):])
            if not valid_daily_qr_token(token):return self.send_html(expired_qr_page(),410)
            s=self.student()
            if not s:return self.send_html(daily_student_register_page(token))
            try:already,arrived=record_daily_arrival(s['student_id'],'qr')
            except ValueError as e:return self.send_html(page('登校受付',f"<div class='center card'>{msgbox(str(e),'error')}</div>"),400)
            return self.send_html(daily_done_page(s,arrived,already))
        if p.startswith('/teacher/class/'):
            if not self.require_teacher():return
            slug=urllib.parse.unquote(p[len('/teacher/class/'):]); period=int((qs.get('period') or ['1'])[0] or 1)
            return self.send_html(teacher_class_page(slug,period))
        if p=='/teacher/ringual':
            if not self.require_teacher():return
            return self.send_html(ringual_page())
        if p=='/admin/students':
            if not self.require_teacher():return
            return self.send_html(admin_students())
        if p.startswith('/board/'):
            if not self.require_teacher():return
            return self.send_html(board_page(urllib.parse.unquote(p[len('/board/'):])) )
        if p.startswith('/class/'):
            slug=urllib.parse.unquote(p[len('/class/'):]);s=self.student();return self.send_html(attend_page(slug,s) if s else student_login(slug))
        if p=='/student/logout':
            nxt=(qs.get('next') or [''])[0]
            if not (nxt.startswith('/d/') and len(nxt)<80):nxt='/'
            return self.redirect(nxt,[('Set-Cookie',self.cookie('student_session','',0))])
        if p.startswith('/api/period-status/'):
            if not self.teacher_ok():return self.send_json({'error':'unauthorized'},401)
            rest=p[len('/api/period-status/'):].split('/')
            if len(rest)!=2:return self.send_json({'error':'bad request'},400)
            slug=urllib.parse.unquote(rest[0]);period=int(rest[1]);d=period_data(slug,period,self.request_base_url())
            return self.send_json(d or {'error':'not found'},200 if d else 404)
        if p.startswith('/api/board-status/'):
            slug=urllib.parse.unquote(p[len('/api/board-status/'):]);cl=class_info(slug)
            if not cl:return self.send_json({'error':'not found'},404)
            ses=current_open_session(slug);present=0;label=''
            if ses:
                label=ses['label']
                with db() as c:present=c.execute("SELECT count(*) FROM period_attendance WHERE session_id=? AND status='出席'",(ses['id'],)).fetchone()[0]
            return self.send_json({'open':bool(ses),'label':label,'total':cl['total'],'present':present})
        if p.startswith('/qr/') and p.endswith('.svg'):
            slug=urllib.parse.unquote(p[len('/qr/'):-4]);cl=class_info(slug)
            if not cl:return self.send_html('not found',404)
            im=qrcode.make(f'{self.request_base_url()}/class/{urllib.parse.quote(slug)}',image_factory=qrcode.image.svg.SvgPathImage,box_size=10,border=4);o=io.BytesIO();im.save(o);b=o.getvalue();self.send_response(200);self.send_header('Content-Type','image/svg+xml');self.send_header('Content-Length',str(len(b)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(b);return
        if p=='/export/ringual/range.xlsx':
            if not self.require_teacher():return
            last=get_setting(RINGUAL_LAST_KEY,''); last_d=parse_date_text(last)
            if not last_d:return self.send_html(ringual_page('先に前回RINGUAL登録済み日を設定してください。','error'),400)
            start=(last_d+timedelta(days=1)).isoformat(); end=(qs.get('end_date') or [''])[0]
            end_d=parse_date_text(end)
            if not end_d or end_d<last_d+timedelta(days=1):return self.send_html(ringual_page('終了日を正しく指定してください。','error'),400)
            if end_d>datetime.now(JST).date():return self.send_html(ringual_page('未来の日付は指定できません。','error'),400)
            if (end_d-(last_d+timedelta(days=1))).days>365:return self.send_html(ringual_page('一度に出力できる期間は366日までです。','error'),400)
            rows,dates=ringual_export_range_data(start,end)
            if not rows:return self.send_html(ringual_page('対象となる学生がいません。','error'),404)
            b=make_ringual_range_xlsx(rows,dates);fn=f"ringual_attendance_{start.replace('-','')}_{end.replace('-','')}_all.xlsx"
            self.send_response(200);self.send_header('Content-Type','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');self.send_header('Content-Disposition',f'attachment; filename="{fn}"');self.send_header('Content-Length',str(len(b)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(b);return
        if p=='/export/ringual/today.xlsx':
            if not self.require_teacher():return
            rows=ringual_export_data()
            if not rows:return self.send_html('在籍学生がいません。',404)
            b=make_ringual_xlsx(rows,today_text());fn=f"ringual_attendance_{datetime.now(JST).strftime('%Y%m%d')}.xlsx"
            self.send_response(200);self.send_header('Content-Type','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');self.send_header('Content-Disposition',f'attachment; filename="{fn}"');self.send_header('Content-Length',str(len(b)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(b);return
        if p.startswith('/export/ringual/class/') and p.endswith('/today.xlsx'):
            if not self.require_teacher():return
            slug=urllib.parse.unquote(p[len('/export/ringual/class/'):-len('/today.xlsx')])
            rows=ringual_export_data(slug)
            if not rows:return self.send_html('クラスが見つかりません。',404)
            b=make_ringual_xlsx(rows,today_text());fn=f"ringual_attendance_{slug}_{datetime.now(JST).strftime('%Y%m%d')}.xlsx"
            self.send_response(200);self.send_header('Content-Type','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');self.send_header('Content-Disposition',f'attachment; filename="{fn}"');self.send_header('Content-Length',str(len(b)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(b);return
        if p.startswith('/export/') and p.endswith('/today.xlsx'):
            if not self.require_teacher():return
            slug=urllib.parse.unquote(p[len('/export/'):-len('/today.xlsx')]);rows=export_rows(slug)
            if not rows:return self.send_html('not found',404)
            b=make_xlsx(rows);fn=f"attendance_{slug}_{datetime.now(JST).strftime('%Y%m%d')}.xlsx";self.send_response(200);self.send_header('Content-Type','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');self.send_header('Content-Disposition',f'attachment; filename="{fn}"');self.send_header('Content-Length',str(len(b)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(b);return
        if p.startswith('/photo/'):return self.send_photo(urllib.parse.unquote(p[len('/photo/'):]))
        if p=='/health':return self.send_html('ok')
        return self.send_html('not found',404)
    def do_POST(self):
        p=urllib.parse.urlparse(self.path).path
        if p=='/teacher/login':
            pw=str(self.form().get('password',''))
            if not TEACHER_PASSWORD:return self.send_html(teacher_login('Railway Variables に TEACHER_PASSWORD を設定してください。'),503)
            if not hmac.compare_digest(pw,TEACHER_PASSWORD):return self.send_html(teacher_login('パスワードが違います。'),401)
            return self.redirect('/teacher',[('Set-Cookie',self.cookie('teacher_session',sign('teacher'),15552000))])
        if p=='/teacher/ringual/set-last':
            if not self.teacher_ok():return self.redirect('/teacher')
            d=str(self.form().get('last_date','')).strip();dd=parse_date_text(d)
            if not dd or dd>datetime.now(JST).date():return self.send_html(ringual_page('日付を正しく指定してください。','error'),400)
            set_setting(RINGUAL_LAST_KEY,d);return self.send_html(ringual_page(f'前回RINGUAL登録済み日を {d} に設定しました。'))
        if p=='/teacher/ringual/confirm':
            if not self.teacher_ok():return self.redirect('/teacher')
            last=get_setting(RINGUAL_LAST_KEY,'');last_d=parse_date_text(last);d=str(self.form().get('end_date','')).strip();dd=parse_date_text(d)
            if not last_d:return self.send_html(ringual_page('先に前回登録日を設定してください。','error'),400)
            if not dd or dd<=last_d or dd>datetime.now(JST).date():return self.send_html(ringual_page('登録完了日を正しく指定してください。','error'),400)
            set_setting(RINGUAL_LAST_KEY,d);return self.send_html(ringual_page(f'RINGUAL登録済み日を {d} まで進めました。次回はその翌日から出力します。'))
        if p.startswith('/api/start-period/'):
            if not self.teacher_ok():return self.send_json({'error':'unauthorized'},401)
            rest=p[len('/api/start-period/'):].split('/')
            if len(rest)!=2:return self.send_json({'error':'bad request'},400)
            slug=urllib.parse.unquote(rest[0]);period=int(rest[1]);f=self.form()
            if not class_info(slug):return self.send_json({'error':'not found'},404)
            open_period(slug,period,f.get('start_time',''),f.get('end_time',''));return self.send_json({'ok':True})
        if p.startswith('/api/close-period/'):
            if not self.teacher_ok():return self.send_json({'error':'unauthorized'},401)
            rest=p[len('/api/close-period/'):].split('/')
            if len(rest)!=2:return self.send_json({'error':'bad request'},400)
            close_period(urllib.parse.unquote(rest[0]),int(rest[1]));return self.send_json({'ok':True})
        if p.startswith('/api/update-record/'):
            if not self.teacher_ok():return self.send_json({'error':'unauthorized'},401)
            rest=p[len('/api/update-record/'):].split('/')
            if len(rest)<3:return self.send_json({'error':'bad request'},400)
            slug=urllib.parse.unquote(rest[0]);period=int(rest[1]);sid=urllib.parse.unquote('/'.join(rest[2:]));f=self.form()
            try:update_record(slug,period,sid,f.get('status',''),f.get('status_time',''),f.get('reason',''))
            except ValueError as e:return self.send_json({'error':str(e)},400)
            return self.send_json({'ok':True})
        if p=='/api/barcode-arrival':
            if not self.teacher_ok():return self.send_json({'error':'教師ログインが必要です。','login_required':True},401)
            f=self.form()
            if verify(f.get('csrf'))!='barcode-arrival':return self.send_json({'error':'受付画面を開き直してください。','login_required':True},403)
            sid=str(f.get('student_id','')).strip()
            if not sid or len(sid)>128 or any(ord(ch)<32 for ch in sid):return self.send_json({'error':'学籍番号を正しく読み取ってください。'},400)
            with db() as c:st=c.execute('SELECT * FROM students WHERE student_id=? AND active=1',(sid,)).fetchone()
            if not st:return self.send_json({'error':'在籍学生が見つかりません。学籍番号と名簿を確認してください。'},404)
            try:already,arrived=record_daily_arrival(sid,'barcode')
            except ValueError as e:return self.send_json({'error':str(e)},400)
            return self.send_json({'ok':True,'already':already,'arrived_at':arrived,'date':arrived[:10],
                                   'student':{'student_id':st['student_id'],'name':st['name'],'class_name':st['class_name'],
                                              'photo':st['photo'],'photo_v':photo_version(st['photo'])}})
        if p.startswith('/api/daily-record/'):
            if not self.teacher_ok():return self.send_json({'error':'unauthorized'},401)
            sid=urllib.parse.unquote(p[len('/api/daily-record/'):]);action=self.form().get('action','')
            try:
                if action=='arrive':already,arrived=record_daily_arrival(sid,'teacher')
                elif action=='remove':remove_daily_arrival(sid);already=False;arrived=''
                else:return self.send_json({'error':'bad request'},400)
            except ValueError as e:return self.send_json({'error':str(e)},400)
            return self.send_json({'ok':True,'already':already,'arrived_at':arrived})
        if p.startswith('/student/register/'):
            token=urllib.parse.unquote(p[len('/student/register/'):])
            if not valid_daily_qr_token(token):return self.send_html(expired_qr_page(),410)
            sid=str(self.form().get('student_id','')).strip()
            with db() as c:s=c.execute('SELECT * FROM students WHERE student_id=? AND active=1',(sid,)).fetchone()
            if not s:return self.send_html(daily_student_register_page(token,'学籍番号が見つかりません。'),401)
            return self.redirect('/d/'+urllib.parse.quote(token),[('Set-Cookie',self.cookie('student_session',sign(sid),31536000))])
        if p.startswith('/student/login/'):
            slug=urllib.parse.unquote(p[len('/student/login/'):]);sid=str(self.form().get('student_id','')).strip()
            with db() as c:s=c.execute('SELECT * FROM students WHERE student_id=? AND active=1',(sid,)).fetchone()
            if not s:return self.send_html(student_login(slug,'学籍番号が見つかりません。'),401)
            if s['class_slug']!=slug:return self.send_html(student_login(slug,f"この学籍番号は {s['class_name']} の学生です。"),401)
            return self.redirect('/class/'+urllib.parse.quote(slug),[('Set-Cookie',self.cookie('student_session',sign(sid),15552000))])
        if p.startswith('/attend/'):
            slug=urllib.parse.unquote(p[len('/attend/'):]);s=self.student()
            if not s:return self.redirect('/class/'+urllib.parse.quote(slug))
            if s['class_slug']!=slug:return self.send_html(attend_page(slug,s,'クラスが一致しません。'),403)
            ses=current_open_session(slug)
            if not ses:return self.send_html(attend_page(slug,s,'現在、出席受付は開始されていません。'),409)
            ses,already,reg=record_qr(slug,s['student_id']);return self.send_html(done_page(slug,s,reg,already,ses))
        if p=='/admin/upload-students':
            if not self.teacher_ok():return self.redirect('/teacher')
            try:
                fields,files=self.multipart();fn,data=files.get('file',(None,None))
                if not fn:raise ValueError('ファイルを選択してください。')
                if not fn.lower().endswith(('.xlsx','.csv')):raise ValueError('Excel (.xlsx) または CSV を選択してください。')
                n=import_students(data,fn,fields.get('replace')=='1');return self.send_html(admin_students(f'{n}名の名簿を取り込みました。'))
            except Exception as e:return self.send_html(admin_students(str(e),'error'),400)
        if p=='/admin/upload-photos':
            if not self.teacher_ok():return self.redirect('/teacher')
            try:
                _,files=self.multipart();fn,data=files.get('file',(None,None))
                if not fn or not fn.lower().endswith('.zip'):raise ValueError('写真ZIPを選択してください。')
                n=import_photos(data);return self.send_html(admin_students(f'{n}枚の写真を取り込みました。写真は差し替え後すぐ反映されます。'))
            except Exception as e:return self.send_html(admin_students(str(e),'error'),400)
        return self.send_html('not found',404)

def main():
    global SECRET
    init_db();SECRET=load_secret();print('='*60);print('QR Attendance v19 Daily + Barcode + Gmail');print(f'Listening on 0.0.0.0:{PORT}');print(f'Data directory: {DATA_DIR}')
    start_reporter(db)
    if IS_RAILWAY:
        print('Railway cloud mode')
        if not TEACHER_PASSWORD:print('WARNING: TEACHER_PASSWORD is not set. Teacher page is locked.')
    else:
        print(f'教師画面: http://127.0.0.1:{PORT}/teacher');threading.Thread(target=lambda:(time.sleep(.8),webbrowser.open(f'http://127.0.0.1:{PORT}/teacher')),daemon=True).start()
    print('終了: Ctrl+C');print('='*60);srv=ThreadingHTTPServer(('0.0.0.0',PORT),Handler)
    try:srv.serve_forever()
    except KeyboardInterrupt:print('\n終了します。')
    finally:srv.server_close()
if __name__=='__main__':main()
