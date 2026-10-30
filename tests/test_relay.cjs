const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const path = require('path');
const props = new Map([['RELAY_TOKEN','r'.repeat(40)],['REPORT_TO','office@example.com'],['CHAT_RELAY_TOKEN','c'.repeat(40)],['CHAT_BASE_URL','https://chat.example.com']]);
const cache = new Map(), sent = [];
const context = {
  LockService:{getScriptLock:()=>({tryLock:()=>true,releaseLock:()=>{}})},
  PropertiesService:{getScriptProperties:()=>({getProperty:k=>props.get(k),setProperty:(k,v)=>props.set(k,v)})},
  CacheService:{getScriptCache:()=>({get:k=>cache.get(k),put:(k,v)=>cache.set(k,v)})},
  ContentService:{MimeType:{JSON:'json'},createTextOutput:text=>({setMimeType:()=>JSON.parse(text)})},
  MailApp:{sendEmail:message=>sent.push(message),getRemainingDailyQuota:()=>100}
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../GmailRelay.gs'),'utf8'),context);
const send = value=>context.doPost({postData:{contents:JSON.stringify(value)}});
const report={token:'r'.repeat(40),body:'report',subject:'【登校状況】2026-10-30 09:00時点',report_id:'a'.repeat(64)};
assert.equal(send(report).ok,true);
assert.equal(sent[0].to,'office@example.com');
assert.equal(send(report).duplicate,true);
assert.equal(sent.length,1);
const account={kind:'student_account',token:'c'.repeat(40),to:'student@example.com',purpose:'reset',url:'https://chat.example.com/reset-password?token='+'t'.repeat(43),request_id:'b'.repeat(64)};
assert.equal(send(account).ok,true);
assert.equal(sent[1].to,'student@example.com');
assert.equal(send(account).duplicate,true);
assert.equal(sent.length,2);
assert.equal(send({...account,token:report.token}).error,'auth');
assert.equal(send({...account,to:'one@example.com,two@example.com'}).error,'invalid');
assert.equal(send({...account,url:'https://evil.example/reset-password?token='+'t'.repeat(43)}).error,'invalid');
assert.equal(send({...account,purpose:'verify',url:'https://chat.example.com/verify-email?token='+'t'.repeat(43),request_id:'c'.repeat(64)}).ok,true);
assert.equal(send({...report,subject:'【登校状況】2026-10-30 10:00時点',report_id:'d'.repeat(64)}).ok,true);
console.log('Apps Script relay: report compatibility, account mail, auth, URL/recipient validation and deduplication passed');
