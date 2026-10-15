# LINE Bot 啟動與限流修正版

版本：reliable-inbox-v1.1-startup。

請先閱讀 [本次更新與部署.md](本次更新與部署.md)，完整功能設定見 [部署指南.md](部署指南.md)。

Build：`pip install --no-cache-dir -r requirements.txt`

Start：`gunicorn --config gunicorn.conf.py lineborblack_cloud:app`

Render Health Check Path：`/ready`；只有資料就緒才回傳 200。

務必上傳新增的 sheets_runtime.py，並保留 reliable_orders.py、既有收件匣與環境變數。

保留原有收件、補處理、黑名單及 Google Sheets 功能；資料尚未就緒時不確認真實訂單訊息。這版仍不保證 LINE 到主機絕對零遺漏。
