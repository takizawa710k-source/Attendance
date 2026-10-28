# Gmail出席報告：Railway Hobby対応版（9時・10時）

RailwayからGoogle Apps ScriptへHTTPSで依頼し、Google側からGmailのメールを送ります。
Railwayのプラン変更やGmailアプリパスワードは不要です。
Apps Scriptを登録するGoogleアカウントが送信元になります。

## 1. Googleの設定

1. 送信元のGmailで https://script.google.com/ にログインします。
2. 「新しいプロジェクト」を作り、名前を「出席報告」にします。
3. 最初のコードをすべて消し、同梱の `GmailRelay.gs` の内容をすべて貼り付けて保存します。
4. 左の歯車「プロジェクトの設定」→「スクリプト プロパティ」に以下を追加します。

   | プロパティ | 値 |
   |---|---|
   | RELAY_TOKEN | 自分で用意したランダムな32文字以上の文字列 |
   | REPORT_TO | 宛先メールアドレス。複数の場合は半角カンマ区切り（例：teacher1@gmail.com,teacher2@gmail.com） |

   ランダムなトークンはPowerShellで次を実行して生成できます。

   ```powershell
   [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
   ```

   表示された文字列をコピーして登録します。同じ文字列を後でRailwayにも登録します。
   チャットには貼らなくて大丈夫です。宛先は送信元と同じでも構いません。
5. エディタに戻り、関数欄で `authorizeMail` を選択して「実行」を押します。
   Googleアカウントを選択し、メール送信の権限を許可します。この関数はメールを送りません。
6. 右上「デプロイ」→「新しいデプロイ」→種類の歯車→「ウェブアプリ」を選びます。
7. 「次のユーザーとして実行」は「自分」、アクセスできるユーザーは「全員」にします。
   コード内でトークンの一致を確認し、登録した宛先だけに送る仕組みです。
   組織の制限で「全員」を選べない場合は、設定を進めず相談してください。
8. 「デプロイ」を押し、発行される `/exec` で終わるURLをコピーします。

## 2. Railwayの設定

既存の出席システムのサービス→Variablesに追加します。

| 変数名 | 値 |
|---|---|
| GMAIL_RELAY_URL | コピーしたウェブアプリURL |
| GMAIL_RELAY_TOKEN | Googleに登録したRELAY_TOKENと同じ文字列 |

GMAIL_USER、GMAIL_APP_PASSWORD、REPORT_TOはRailwayには不要です。
DiscordのWebhook変数も使いません。
既存のTEACHER_PASSWORD、DATA_DIR、Volumeは維持します。
サービスのServerless／自動スリープを無効にし、1レプリカで運用します。

## 3. 更新版を反映

GitHubの最新版をRailwayへ反映します。10時メールを使うため、Google Apps Script側も同梱GmailRelay.gsで更新し、ウェブアプリを新バージョンで再デプロイしてください。詳しい動作条件はREADME.mdを参照してください。

ZIP内の M1_QR_Attendance_v19_gmail_cloud フォルダを C:\railway に置き、PowerShellで実行します。

```powershell
cd "C:\railway\M1_QR_Attendance_v19_gmail_cloud"
C:\railway\railway.exe link
C:\railway\railway.exe up --service attendance-web
```

linkは既存プロジェクト・productionを選びます。サービス名は既存の名前を指定します。
既存の出席データとVolumeを引き継ぎます。元のローカルフォルダは変更していません。

## 4. 動作確認

`[Gmail] Weekdays 09:00 and 10:00 JST enabled` は設定を読み込んだことを示します。
実際のGoogle認証やメール送信の確認ではありません。
次の対象日の朝9時にメールが届くことを確認してください。迷惑メールも確認します。
`[Gmail] 日付 時刻: sent (1 messages)` はGoogle側の送信受付が成功した結果です。

## 投稿内容・運用

- 平日9時（判定は日本時間）に人数とクラス別の未登校者名を1通で送ります。
- 9時までの登校登録が0件の日は9時メールを省略します。10時は9時の未登校者がいる場合に状況を送ります。土日は送らず、祝日は個別判定しません。
- 「（日本時間）」・注記・ページ番号は本文に付けません。
- 一時障害は5分間隔で当日中に再試行します。認証・設定エラーは当日の再送を停止します。
- 9時の送信履歴はgmail_daily_reports、未登校対象者はgmail_pending_students、10時の送信履歴はgmail_followup_reportsに保存します。
- Google側でも9時・10時別に直近の報告IDを保存して通常の再試行による重複を防ぎます。
  送信直後にGoogle側が停止した場合などには重複の可能性が残ります。
- 9:00〜9:09の再起動時は9時以前の登校時刻を使って復旧集計します。
  9:10以降は新たな報告を作りません。集計後の訂正ではメールを変更しません。
- Googleの送信上限・組織ポリシーが適用されます。
- Google側のコードを変更した場合は、ウェブアプリの新バージョンをデプロイします。
- ローカルの一時DBと模擬HTTPSで検証済みです。実際のデプロイ・メール送信は未実施です。

## 本文例

```text
【登校状況】2026-10-16 09:00時点
在籍 30名 ／ 登校 27名 ／ 未登校 3名

■ Aクラス：登校 14/15名・未登校 1名
・学生A

■ Bクラス：登校 13/15名・未登校 2名
・学生B
・学生C
```

公式資料：
- https://developers.google.com/apps-script/guides/web
- https://developers.google.com/apps-script/reference/mail/mail-app
- https://docs.railway.com/networking/outbound-networking

