"""Authenticated server-to-server bridge; the attendance DB remains authoritative."""
import hmac
import io
import json
import os
import re


def handle(h, app):
    try:
        length=int(h.headers.get('Content-Length','0'))
        if not 0<length<=8192:raise ValueError()
    except ValueError:return h.send_json({'error':'Invalid request size'},400)
    raw=h.rfile.read(length)
    key=os.environ.get('CHAT_BRIDGE_TOKEN','').strip()
    if len(key)<32:return h.send_json({'error':'チャット連携が未設定です。'},503)
    supplied=h.headers.get('Authorization','')
    if not hmac.compare_digest(supplied.encode(),('Bearer '+key).encode()):
        return h.send_json({'error':'チャット連携の認証に失敗しました。'},401)
    try:
        data=json.loads(raw)
        if not isinstance(data,dict):raise ValueError('Invalid request')
        action=data.get('action','')
        if action in ('arrival','student_status'):
            sid=data.get('student_id','')
            if not isinstance(sid,str) or not re.fullmatch(r'[\w-]{1,60}',sid):raise ValueError('学籍番号が不正です。')
            with app['db']() as c:
                student=c.execute('SELECT student_id,name,class_name FROM students WHERE student_id=? AND active=1',(sid,)).fetchone()
            if not student:return h.send_json({'error':'出席名簿に学籍番号が見つかりません。学校へお問い合わせください。'},404)
            if action=='arrival':
                qr=data.get('qr_token','')
                if not isinstance(qr,str) or len(qr)>128 or not app['valid_daily_qr_token'](qr):
                    return h.send_json({'error':'このQRコードは使用できません。学校の最新の印刷用QRを読み取ってください。'},410)
                already,arrived=app['record_daily_arrival'](sid,'chat_qr')
                return h.send_json({'ok':True,'already':already,'arrived_at':arrived,'date':arrived[:10], 'student':dict(student)})
            with app['db']() as c:
                row=c.execute('SELECT arrived_at FROM daily_attendance WHERE attendance_date=? AND student_id=?',(app['today_text'](),sid)).fetchone()
            return h.send_json({'ok':True,'date':app['today_text'](),'student':dict(student),'arrived_at':row['arrived_at'] if row else ''})
        if action=='daily_status':
            status=app['daily_status_data']()
            # Do not send photos or device/session credentials to the chat service.
            groups=[]
            for group in status['groups']:
                groups.append({k:group[k] for k in ('class_name','total','present','absent')} | {
                    'students':[{k:s[k] for k in ('student_id','name','arrived_at','present')} for s in group['students']]})
            return h.send_json({k:status[k] for k in ('date','total','present','absent')} | {'ok':True,'groups':groups})
        if action=='daily_qr':
            token=app['daily_qr_token'](refresh=True)
            url=h.request_base_url()+'/d/'+token
            qr=app['qrcode'].QRCode(error_correction=app['qrcode'].constants.ERROR_CORRECT_M,box_size=12,border=4)
            qr.add_data(url);qr.make(fit=True)
            out=io.BytesIO();qr.make_image(image_factory=app['qrcode'].image.svg.SvgPathImage).save(out)
            return h.send_json({'ok':True,'svg':out.getvalue().decode(),'url':url,'date':app['today_text']()})
        return h.send_json({'error':'Unknown action'},400)
    except (ValueError,TypeError,KeyError):
        return h.send_json({'error':'リクエストの形式を確認してください。'},400)
