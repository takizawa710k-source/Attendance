// Deploy as a web app: Execute as Me; access Anyone.
// Script Properties: RELAY_TOKEN (32+ characters), REPORT_TO (comma-separated recipients).
function doPost(e) {
  var lock = LockService.getScriptLock();
  var locked = false;
  try {
    var data = JSON.parse(e.postData.contents);
    var props = PropertiesService.getScriptProperties();
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
