# LINE Bot 訂單收件與補處理加固版

版本：reliable-inbox-v1。保留 LINE Bot、黑名單、群組訂單及 Google Sheets。

請依 [部署指南.md](部署指南.md) 設定 REPLAY_SECRET、群組與管理員 ID、LINE Webhook redelivery，以及「自動補處理.gs」的 Google Apps Script 觸發器。

Build：`pip install --no-cache-dir -r requirements.txt`

Start：`gunicorn --config gunicorn.conf.py lineborblack_cloud:app`

這版提供已收訊息的保存、重試與核對，不保證 LINE 到主機的所有訊息都必定送達。
