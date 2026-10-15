# linebotblack — Render Free 修正版

保留原有 LINE Bot、黑名單、群組訂單與 Google Sheets 功能。

部署與測試結果請參閱 [部署與排查.md](部署與排查.md)。

Build: `pip install --no-cache-dir -r requirements.txt`

Start: `gunicorn --config gunicorn.conf.py lineborblack_cloud:app`
