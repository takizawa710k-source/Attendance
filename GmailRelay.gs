// Deploy as a web app: Execute as Me; access Anyone.
// Script Properties: RELAY_TOKEN (32+ characters), REPORT_TO (comma-separated recipients).
function doPost(e) {
  var lock = LockService.getScriptLock();
  var locked = false;
  try {
    var data = JSON.parse(e.postData.contents);
    var props = PropertiesService.getScriptProperties();
    // Separate authentication for student account messages; attendance reports
    // retain their existing token, recipients, subject checks and deduplication.
    if (data.kind === 'student_account') return sendStudentAccount_(data, props);
    var token = props.getProperty('RELAY_TOKEN');
    var recipient = props.getProperty('REPORT_TO');
    var recipients = (recipient || '').split(',').map(function(value) { return value.trim(); });
    if (!token || token.length < 32 || !recipients.every(function(value) {
      return /^[^\s@,;]+@[^\s@,;]+\.[^\s@,;]+$/.test(value);
    })) {
      return reply_({ok: false, error: 'config'});
    }
    if (data.token !== token) return reply_({ok: false, error: 'auth'});
    if (typeof data.body !== 'string' || data.body.length > 200000 ||
        typeof data.subject !== 'string' || !/^【登校状況】\d{4}-\d{2}-\d{2} (?:09|10):00時点$/.test(data.subject) ||
        !/^[a-f0-9]{64}$/.test(data.report_id)) {
      return reply_({ok: false, error: 'invalid'});
    }
    locked = lock.tryLock(1000);
    if (!locked) return reply_({ok: false, error: 'busy'});
    // Keep only the latest successful report id to avoid unbounded property storage.
    var reportKey = data.subject.indexOf('10:00') >= 0 ? 'REPORT_10' : 'REPORT_09';
    if (props.getProperty(reportKey) === data.report_id) {
      return reply_({ok: true, duplicate: true});
    }
    MailApp.sendEmail({to: recipients.join(','), subject: data.subject, body: data.body});
    props.setProperty(reportKey, data.report_id);
    return reply_({ok: true});
  } catch (error) {
    // Do not expose student data, tokens or recipient details in responses.
    return reply_({ok: false, error: 'temporary'});
  } finally {
    if (locked) lock.releaseLock();
  }
}

function reply_(value) {
  return ContentService.createTextOutput(JSON.stringify(value))
    .setMimeType(ContentService.MimeType.JSON);
}

// Run once from the editor to authorize mail sending; does not send any mail.
function authorizeMail() {
  return MailApp.getRemainingDailyQuota();
}

// New Script Properties: CHAT_RELAY_TOKEN (32+ random characters),
// CHAT_BASE_URL (exact https:// school-chat origin, with no trailing slash).
function sendStudentAccount_(data, props) {
  var expected = props.getProperty('CHAT_RELAY_TOKEN') || '';
  var base = (props.getProperty('CHAT_BASE_URL') || '').replace(/\/$/, '');
  if (expected.length < 32 || !/^https:\/\/[^\s/?#@]+$/.test(base)) return reply_({ok:false,error:'config'});
  if (typeof data.token !== 'string' || data.token !== expected) return reply_({ok:false,error:'auth'});
  var path = data.purpose === 'verify' ? '/verify-email' : data.purpose === 'reset' ? '/reset-password' : '';
  var prefix = base + path + '?token=';
  if (!path || typeof data.to !== 'string' || data.to.length > 254 ||
      !/^[^\s@,;]+@[^\s@,;]+\.[^\s@,;]+$/.test(data.to) ||
      typeof data.url !== 'string' || data.url.indexOf(prefix) !== 0 ||
      !/^[A-Za-z0-9_-]{40,100}$/.test(data.url.slice(prefix.length)) ||
      typeof data.request_id !== 'string' || !/^[a-f0-9]{64}$/.test(data.request_id)) {
    return reply_({ok:false,error:'invalid'});
  }
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) return reply_({ok:false,error:'busy'});
  try {
    var cache = CacheService.getScriptCache();
    var cacheKey = 'student_mail_' + data.request_id;
    if (cache.get(cacheKey)) return reply_({ok:true,duplicate:true});
    var verify = data.purpose === 'verify';
    var subject = '軽井沢国際日本語学校：' + (verify ? 'メールアドレスの確認' : 'パスワードの再設定');
    var body = '以下のリンクを開いて' + (verify ? 'メールアドレスを確認' : 'パスワードを再設定') + 'してください。\n\n' + data.url +
      '\n\n有効期限は' + (verify ? '30' : '15') + '分です。リンクは一度だけ使用できます。心当たりがなければ、このメールは破棄してください。';
    MailApp.sendEmail({to:data.to,subject:subject,body:body});
    cache.put(cacheKey, 'sent', 21600);
    return reply_({ok:true});
  } catch (error) {
    return reply_({ok:false,error:'temporary'});
  } finally {
    lock.releaseLock();
  }
}
