import contextlib
import hashlib
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from datetime import datetime
from http.server import ThreadingHTTPServer

import app


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        root=Path(self.temp.name)
        self.patches=[patch.object(app,'DATA_DIR',root),patch.object(app,'DB_PATH',root/'attendance.db'),
            patch.object(app,'PHOTOS_DIR',root/'photos'),patch.object(app,'APP_DIR',root),
            patch.object(app,'SEED_PHOTOS_DIR',root/'seedphotos'),patch.object(app,'SECRET',b'test-secret'),
            patch.dict(os.environ,{'CHAT_BRIDGE_TOKEN':'b'*40})]
        for p in self.patches:p.start()
        raw_db=app.db
        @contextlib.contextmanager
        def closing_db():
            connection=raw_db()
            try:
                with connection:yield connection
            finally:connection.close()
        self.db_patch=patch.object(app,'db',closing_db);self.db_patch.start()
        app.init_db()
        with app.db() as c:
            c.execute('INSERT INTO students(student_id,name,class_name,class_slug) VALUES(?,?,?,?)',('s1','学生','A','A'))

    def tearDown(self):
        self.db_patch.stop()
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()

    def at(self,value):return datetime.fromisoformat(value).replace(tzinfo=app.JST)

    def test_friday_to_monday_and_staff_refresh_at_14(self):
        friday=app.daily_qr_token(refresh=True,now=self.at('2026-10-30T14:00:00'))
        for moment in ('2026-10-30T18:00:00','2026-10-31T14:00:00','2026-11-01T15:00:00','2026-11-02T08:30:00','2026-11-02T13:59:59'):
            self.assertEqual(app.daily_qr_token(refresh=True,now=self.at(moment)),friday)
        # Student scans after 14:00 cannot rotate the printed QR.
        self.assertEqual(app.daily_qr_token(now=self.at('2026-11-02T14:01:00')),friday)
        monday=app.daily_qr_token(refresh=True,now=self.at('2026-11-02T14:01:00'))
        self.assertNotEqual(monday,friday)
        self.assertEqual(app.daily_qr_token(refresh=True,now=self.at('2026-11-02T17:00:00')),monday)
        with patch.object(app,'daily_qr_token',return_value=monday):
            self.assertTrue(app.valid_daily_qr_token(monday));self.assertFalse(app.valid_daily_qr_token(friday))

    def test_no_refresh_no_expiry_and_duplicate_arrival(self):
        token=app.daily_qr_token(now=self.at('2026-10-30T08:00:00'))
        self.assertEqual(app.daily_qr_token(now=self.at('2026-11-03T08:00:00')),token)
        with patch.object(app,'now_text',return_value='2026-11-02 08:31:00'),patch.object(app,'today_text',return_value='2026-11-02'):
            self.assertEqual(app.record_daily_arrival('s1','chat_qr'),(False,'2026-11-02 08:31:00'))
        with patch.object(app,'now_text',return_value='2026-11-02 08:45:00'),patch.object(app,'today_text',return_value='2026-11-02'):
            self.assertEqual(app.record_daily_arrival('s1','chat_qr'),(True,'2026-11-02 08:31:00'))
            status=app.daily_status_data()
            self.assertEqual(status['present'],1)
        with app.db() as c:self.assertEqual(c.execute('SELECT source FROM daily_attendance').fetchone()[0],'chat_qr')

    def test_bridge_http_authorization_and_shared_records(self):
        class QuietHandler(app.Handler):
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),QuietHandler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(fields,key='b'*40):
            connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=10)
            body=json.dumps(fields)
            connection.request('POST','/api/chat-bridge',body,{'Content-Type':'application/json','Authorization':'Bearer '+key})
            response=connection.getresponse();result=(response.status,json.loads(response.read()));connection.close();return result
        try:
            self.assertEqual(request({'action':'daily_status'},'wrong')[0],401)
            self.assertEqual(request({'action':'student_status','student_id':'missing'})[0],404)
            self.assertEqual(request({'action':'arrival','student_id':'s1','qr_token':'invalid'})[0],410)
            qr=request({'action':'daily_qr'})[1]
            self.assertIn('<svg',qr['svg'])
            token=qr['url'].split('/d/')[1]
            result=request({'action':'arrival','student_id':'s1','qr_token':token})
            self.assertEqual(result[0],200);self.assertFalse(result[1]['already'])
            self.assertTrue(request({'action':'arrival','student_id':'s1','qr_token':token})[1]['already'])
            self.assertEqual(request({'action':'daily_status'})[1]['present'],1)
            self.assertNotIn('photo',request({'action':'daily_status'})[1]['groups'][0]['students'][0])
            with app.db() as c:
                c.execute('UPDATE students SET active=0 WHERE student_id=?',('s1',))
            self.assertEqual(request({'action':'arrival','student_id':'s1','qr_token':token})[0],404)
        finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
