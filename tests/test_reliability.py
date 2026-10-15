import sys, os, json, re, hmac, hashlib, base64, unittest, copy
from pathlib import Path
from urllib.parse import unquote
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.update(CHANNEL_ACCESS_TOKEN='test', CHANNEL_SECRET='secret', GOOGLE_SHEET_ID='drivers',
                  GOOGLE_SERVICE_ACCOUNT_JSON='{}', BOT_RECORD_SHEET_ID='records', ORDER_SHEET_ID='orders',
                  PACKAGE_GROUP_ID='Ctest', TEST_GROUP_ID='Ctest2', SUMMARY_ADMIN_USER_ID='Uadmin',
                  REPLAY_SECRET='test-only-secret-' + 'x' * 40, BOT_RAW_EVENT_LOG_ENABLED='false')


class Sheets:
    def __init__(self):
        self.tabs = {name: {'id': i, 'rows': [['header']]} for i, name in enumerate(
            ['訂單事件', 'LINE訂單', 'LINE原始事件', '訂單收件匣', '2026/10月'], 1)}
        self.tabs['訂單收件匣']['rows'][0] = ['event_id', 'received_at', 'status', 'event_json', 'detail', 'updated_at', 'attempts', 'result_json']
        self.fail_receive = None
        self.fail_commit = None
        self.fail_reads = False
        self.last_batch = None

    def response(self, data):
        result = Mock(status_code=200, text='{}')
        result.json.return_value = data
        return result

    def values(self, rng):
        if self.fail_reads:
            raise ConnectionError('read offline')
        m = re.match(r"'?(.+?)'?!([A-Z]+)(\d*):([A-Z]+)(\d*)$", rng)
        if not m:
            raise AssertionError('Unexpected range: ' + rng)
        title, firstcol, firstrow, lastcol, lastrow = m.groups()
        def col(s):
            n = 0
            for c in s:
                n = n * 26 + ord(c) - 64
            return n - 1
        rows = self.tabs[title]['rows']
        start, end = int(firstrow or 1)-1, int(lastrow or len(rows))
        result = [r[col(firstcol):col(lastcol)+1] for r in rows[start:end]]
        while result and not result[-1]:
            result.pop()
        return result

    def request(self, method, url, **kw):
        decoded = unquote(url)
        if '?fields=sheets.properties' in url:
            return self.response({'sheets': [{'properties': {'title': name, 'sheetId': data['id'],
                                  'gridProperties': {'rowCount': max(1000, len(data['rows']))}}}
                                 for name, data in self.tabs.items()]})
        if 'values:batchGet' in url:
            return self.response({'valueRanges': [{'values': self.values(r)} for r in kw['params']['ranges']]})
        if ':batchUpdate' in url:
            requests = kw['json']['requests']
            self.last_batch = copy.deepcopy(requests)
            is_order_commit = any('appendCells' in r for r in requests)
            if self.fail_commit == 'before' and is_order_commit:
                self.fail_commit = None
                raise ConnectionError('commit rejected')
            original = copy.deepcopy(self.tabs)
            try:
                for req in requests:
                    if 'appendCells' in req:
                        op = req['appendCells']
                        tab = next(t for t in self.tabs.values() if t['id'] == op['sheetId'])
                        tab['rows'].extend([[c['userEnteredValue']['stringValue'] for c in r['values']] for r in op['rows']])
                    elif 'updateCells' in req:
                        op = req['updateCells']; start = op['start']
                        tab = next(t for t in self.tabs.values() if t['id'] == start['sheetId'])
                        row = tab['rows'][start['rowIndex']]
                        values = [c['userEnteredValue']['stringValue'] for c in op['rows'][0]['values']]
                        row.extend([''] * max(0, start['columnIndex'] + len(values) - len(row)))
                        row[start['columnIndex']:start['columnIndex']+len(values)] = values
                    else:
                        raise AssertionError('Unexpected operation')
            except Exception:
                self.tabs = original
                raise
            if self.fail_commit in {'after', 'after-offline'} and is_order_commit:
                offline = self.fail_commit == 'after-offline'
                self.fail_commit = None
                self.fail_reads = offline
                raise ConnectionError('response lost after commit')
            return self.response({'replies': [{} for _ in requests]})
        if '/values/' in url:
            rng = decoded.split('/values/', 1)[1].split('?', 1)[0]
            if ':append' in rng:
                title = rng.split('!')[0].strip("'")
                if self.fail_receive == 'before' and title == '訂單收件匣':
                    self.fail_receive = None
                    raise ConnectionError('receive unavailable')
                rows = kw['json']['values']
                self.tabs[title]['rows'].extend(copy.deepcopy(rows))
                rownum = len(self.tabs[title]['rows'])
                if self.fail_receive == 'after' and title == '訂單收件匣':
                    self.fail_receive = None
                    raise ConnectionError('receive response lost')
                return self.response({'updates': {'updatedRange': f"'{title}'!A{rownum}:H{rownum}"}})
            if method == 'GET':
                return self.response({'values': self.values(rng)})
        raise AssertionError((method, decoded))


db = Sheets()
google_patch = patch('requests.request', side_effect=lambda *a, **k: db.request(*a, **k))
creds_patch = patch('google.oauth2.service_account.Credentials.from_service_account_info', return_value=Mock(valid=True, token='dummy'))
google_patch.start(); creds_patch.start()
with patch('threading.Thread.start'):
    import lineborblack_cloud as bot
bot.app.logger.disabled = True
bot._initialize_storage()
assert bot._storage_state['ready']

ORDER = 'MKJ423828 送機\n出發日期：10/15 10:45\n乘車人數：2\n上車地點：台北市信義區\n下車地點：桃園機場'


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        global db
        db = Sheets()
        bot._sheet_metadata_cache.clear()
        bot._sheets_gate.reset()
        bot._storage_state.update(ready=True, initializing=False, last_error=None, next_retry=0.0)
        bot._processed_webhook_event_ids = set()
        bot._order_write_batch = None
        bot.order_event_store.load(); bot.line_order_store.load()
        bot.reliable_orders = bot.ReliableOrders(bot, bot.BOT_INBOX_TAB)
        self.client = bot.app.test_client()
        self.reply = patch.object(bot.line_bot_api, 'reply_message').start()
        self.usage = patch.object(bot, '_record_bot_usage').start()
        self.driver = patch.object(bot, 'load_driver_lists').start()
        self.profile = patch.object(bot, '_record_private_interaction').start()
        bot.blocked_list = []

    def tearDown(self):
        patch.stopall()
        google_patch.start(); creds_patch.start()

    def event(self, text, eid='e1', message_type='text', source=None):
        message = {'id': eid, 'type': message_type}
        if message_type == 'text':
            message['text'] = text
        return {'type': 'message', 'message': message, 'timestamp': 1792000000000,
                'source': source or {'type': 'group', 'groupId': 'Ctest', 'userId': 'Utest'},
                'replyToken': 'reply', 'webhookEventId': eid, 'deliveryContext': {'isRedelivery': False}}

    def send(self, text=ORDER, eid='e1', **kw):
        body = json.dumps({'events': [self.event(text, eid, **kw)]}, ensure_ascii=False)
        sig = base64.b64encode(hmac.new(b'secret', body.encode(), hashlib.sha256).digest()).decode()
        return self.client.post('/callback', data=body.encode(), headers={'X-Line-Signature': sig})

    def replay(self):
        return self.client.post('/internal/replay', headers={'Authorization': 'Bearer ' + bot.REPLAY_SECRET})

    def status(self):
        return db.tabs['訂單收件匣']['rows'][1][2]

    def test_receipt_fails_no_false_ack(self):
        db.fail_receive = 'before'
        self.assertEqual(self.send().status_code, 500)
        self.assertEqual(len(db.tabs['LINE訂單']['rows']), 1)
        self.assertEqual(len(db.tabs['訂單收件匣']['rows']), 1)

    def test_saved_atomic_and_duplicate_delivery(self):
        self.assertEqual(self.send().status_code, 200)
        self.assertEqual(self.status(), 'DONE')
        self.assertEqual(len(db.tabs['LINE訂單']['rows']), 2)
        self.assertIn('appendCells', db.last_batch[0])
        self.assertIn('updateCells', db.last_batch[-1])
        self.send()
        self.assertEqual(len(db.tabs['LINE訂單']['rows']), 2)
        self.assertEqual(len(db.tabs['訂單收件匣']['rows']), 2)

    def test_commit_fails_then_timer_recovers(self):
        db.fail_commit = 'before'
        self.assertEqual(self.send().status_code, 200)  # receipt is durable
        self.assertEqual(self.status(), 'COMMITTING')
        self.assertEqual(len(db.tabs['LINE訂單']['rows']), 1)
        self.replay()  # An uncertain commit is not immediately repeated.
        self.assertEqual(len(db.tabs['LINE訂單']['rows']), 1)
        db.tabs['訂單收件匣']['rows'][1][5] = '2020-01-01T00:00:00+00:00'
        self.assertEqual(self.replay().status_code, 200)
        self.assertEqual(self.status(), 'DONE')
        self.assertEqual(len(db.tabs['LINE訂單']['rows']), 2)

    def test_response_lost_after_atomic_commit(self):
        db.fail_commit = 'after'
        self.assertEqual(self.send().status_code, 200)
        self.assertEqual(self.status(), 'DONE')
        self.replay(); self.send()
        self.assertEqual(len(db.tabs['LINE訂單']['rows']), 2)

    def test_response_and_reads_lost_then_restart(self):
        db.fail_commit = 'after-offline'
        self.assertEqual(self.send().status_code, 200)
        db.fail_reads = False
        bot.order_event_store.load(); bot.line_order_store.load()
        bot.reliable_orders = bot.ReliableOrders(bot, bot.BOT_INBOX_TAB)
        self.replay()
        self.assertEqual(len(db.tabs['LINE訂單']['rows']), 2)
        self.assertEqual(self.status(), 'DONE')

    def test_receive_response_lost_redelivery_finds_receipt(self):
        db.fail_receive = 'after'
        self.assertEqual(self.send().status_code, 500)
        self.assertEqual(self.status(), 'PENDING')
        self.assertEqual(self.send().status_code, 200)
        self.assertEqual(len(db.tabs['訂單收件匣']['rows']), 2)
        self.assertEqual(len(db.tabs['LINE訂單']['rows']), 2)

    def test_restart_between_receipt_and_processing(self):
        event = bot.MessageEvent.new_from_json_dict(self.event(ORDER))
        bot.reliable_orders.receive(event)
        self.assertEqual(self.status(), 'PENDING')
        bot.reliable_orders = bot.ReliableOrders(bot, bot.BOT_INBOX_TAB)
        self.replay()
        self.assertEqual(self.status(), 'DONE')

    def test_unparsed_text_preserved_for_review(self):
        text = '這筆是特殊格式訂單，時間明天早上，地址稍後補'
        self.send(text)
        self.assertEqual(self.status(), 'REVIEW')
        saved = json.loads(db.tabs['訂單收件匣']['rows'][1][3])
        self.assertEqual(saved['message']['text'], text)
        self.assertNotIn('replyToken', saved)

    def test_conflict_goes_to_review_not_false_success(self):
        self.send()
        self.send(ORDER.replace('10:45', '11:45'), 'e2')
        self.assertEqual(db.tabs['訂單收件匣']['rows'][2][2], 'REVIEW')
        self.assertEqual(len(db.tabs['LINE訂單']['rows']), 2)
        self.assertTrue(any(r[1] == 'conflict' for r in db.tabs['訂單事件']['rows'][1:]))

    def test_distinct_events_same_minute_not_suppressed(self):
        self.send('MKJ423828 取消', 'e1')
        self.send('MKJ423828 取消', 'e2')
        self.assertEqual(len(db.tabs['訂單事件']['rows']), 3)

    def test_explicit_dispatch_only_once(self):
        self.send('派車 MKJ423828 王小明 ABC-1234')
        self.assertEqual(self.status(), 'DONE')
        self.assertEqual(len(db.tabs['訂單事件']['rows']), 2)
        self.assertEqual(self.reply.call_count, 1)
        self.replay()
        self.assertEqual(self.reply.call_count, 1)  # replay never reuses reply tokens

    def test_failed_earlier_event_blocks_later_event(self):
        first = bot.MessageEvent.new_from_json_dict(self.event(ORDER, 'e1'))
        second = bot.MessageEvent.new_from_json_dict(self.event('MKJ423828 取消', 'e2'))
        bot.reliable_orders.receive(first); bot.reliable_orders.receive(second)
        db.fail_commit = 'before'
        bot.reliable_orders.drain()
        self.assertEqual(db.tabs['訂單收件匣']['rows'][2][2], 'PENDING')
        db.tabs['訂單收件匣']['rows'][1][5] = '2020-01-01T00:00:00+00:00'
        bot.reliable_orders.drain()
        self.assertEqual(db.tabs['訂單收件匣']['rows'][2][2], 'DONE')
        self.assertEqual(bot.order_event_store.events[-1].event_type, 'cancelled')

    def test_media_preserved_for_review(self):
        self.assertEqual(self.send('', message_type='image').status_code, 200)
        self.assertEqual(self.status(), 'REVIEW')

    def test_signatures_and_replay_auth(self):
        self.assertEqual(self.client.post('/callback', data='{}').status_code, 400)
        self.assertEqual(self.client.post('/callback', data='{}', headers={'X-Line-Signature':'bad'}).status_code, 400)
        self.assertEqual(self.client.post('/internal/replay').status_code, 403)
        self.assertEqual(len(db.tabs['訂單收件匣']['rows']), 1)

    def test_blacklist_still_replies(self):
        bot.blocked_list = [{'name':'測試司機', 'phone':['0912345678'], 'plate':'ABC-1234', 'account':'123456789012'}]
        self.send('測試司機')
        self.assertIn('黑名單警告', self.reply.call_args.args[1].text)
        self.assertEqual(self.status(), 'REVIEW')

    def test_admin_command_private_only(self):
        self.send('漏單檢查', source={'type':'user', 'userId':'Uadmin'})
        self.assertIn('訂單收件匣', self.reply.call_args.args[1].text)
        self.send('漏單檢查', source={'type':'user', 'userId':'Uother'})
        self.assertIn('管理員', self.reply.call_args.args[1].text)

    def test_manual_review_close_is_audited(self):
        self.send('不明格式訂單')
        self.send('結案訊息 e1', eid='admin1', source={'type':'user', 'userId':'Uadmin'})
        self.assertEqual(self.status(), 'RESOLVED')
        result = json.loads(db.tabs['訂單收件匣']['rows'][1][7])
        self.assertEqual(result['reviewed_by'], 'Uadmin')
        self.assertEqual(len(db.tabs['LINE訂單']['rows']), 1)

    def test_manual_review_retry(self):
        self.send('不明格式訂單')
        self.send('重試訊息 e1', eid='admin1', source={'type':'user', 'userId':'Uadmin'})
        self.assertEqual(self.status(), 'PENDING')
        self.replay()
        self.assertEqual(self.status(), 'REVIEW')

    def test_private_dispatch_cannot_bypass_inbox(self):
        self.send('派車 MKJ423828 王小明 ABC-1234', source={'type':'user','userId':'Utest'})
        self.assertEqual(len(db.tabs['訂單事件']['rows']), 1)
        self.assertIn('已授權群組', self.reply.call_args.args[1].text)

    def test_uncertain_commit_cooldown_survives_restart(self):
        db.fail_commit = 'before'
        self.send()
        bot.order_event_store.load(); bot.line_order_store.load()
        bot.reliable_orders = bot.ReliableOrders(bot, bot.BOT_INBOX_TAB)
        self.replay()
        self.assertEqual(self.status(), 'COMMITTING')
        self.assertEqual(len(db.tabs['LINE訂單']['rows']), 1)

    def test_source_sheet_failure_not_assumed_empty(self):
        original = db.values
        def fail_month(rng):
            if '2026/10月' in rng:
                raise ConnectionError('monthly source offline')
            return original(rng)
        with patch.object(db, 'values', side_effect=fail_month):
            self.assertEqual(self.send().status_code, 200)
        self.assertEqual(self.status(), 'ERROR')
        self.assertEqual(len(db.tabs['LINE訂單']['rows']), 1)
        self.replay()
        self.assertEqual(self.status(), 'DONE')

    def test_index_preserves_real_row_number_across_blank_pages(self):
        event = bot.MessageEvent.new_from_json_dict(self.event(ORDER))
        bot.reliable_orders.receive(event)
        header, row = db.tabs['訂單收件匣']['rows']
        db.tabs['訂單收件匣']['rows'] = [header] + [[] for _ in range(1000)] + [row]
        bot._sheet_metadata_cache.clear()  # a fresh process has no metadata cache
        bot.reliable_orders = bot.ReliableOrders(bot, bot.BOT_INBOX_TAB)
        self.assertEqual(bot.reliable_orders.index['e1']['row'], 1002)
        self.replay()
        self.assertEqual(db.tabs['訂單收件匣']['rows'][1001][2], 'DONE')

    def test_duplicate_receipt_final_row_wins(self):
        self.send()
        done = db.tabs['訂單收件匣']['rows'][1]
        duplicate = copy.deepcopy(done); duplicate[2] = 'PENDING'
        db.tabs['訂單收件匣']['rows'].append(duplicate)
        bot.reliable_orders.refresh()
        self.replay()
        self.assertEqual(bot.reliable_orders.index['e1']['status'], 'DONE')
        self.assertEqual(len(db.tabs['LINE訂單']['rows']), 2)

    def test_private_summary_does_not_hide_pending_orders(self):
        event = bot.MessageEvent.new_from_json_dict(self.event(ORDER))
        bot.reliable_orders.receive(event)
        db.fail_commit = 'before'
        with patch.object(bot.line_bot_api, 'push_message') as push:
            self.send('簡表 10/15 9353', eid='summary1', source={'type':'user', 'userId':'Utest'})
            self.assertFalse(push.called)
            self.assertIn('暫不產生簡表', self.reply.call_args.args[1].text)

    def test_summary_warns_about_review_records(self):
        self.send('尚未確認格式的訂單')
        with patch.object(bot.line_bot_api, 'push_message') as push:
            self.send('簡表 10/15 9353', eid='summary1', source={'type':'user', 'userId':'Utest'})
            self.assertTrue(push.called)
            self.assertIn('待人工確認 1 筆', push.call_args.args[1][0].text)

    def test_completed_receipt_cannot_be_requeued(self):
        self.send()
        with self.assertRaises(ValueError):
            bot.reliable_orders.review_action('e1', 'Uadmin', retry=True)

    def test_web_stays_live_but_orders_not_acknowledged_before_storage_ready(self):
        bot._storage_state.update(ready=False, initializing=True)
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertIn('storage=NOT_READY', self.client.get('/').text)
        self.assertEqual(self.client.get('/ready').status_code, 503)
        response = self.send()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers['Retry-After'], '60')
        self.assertEqual(len(db.tabs['訂單收件匣']['rows']), 1)
        self.assertEqual(self.replay().status_code, 503)
        self.assertEqual(self.client.post('/callback', data='{}', headers={'X-Line-Signature':'bad'}).status_code, 400)

    def test_bootstrap_failure_does_not_exit_or_ack_orders(self):
        bot._storage_state.update(ready=False, initializing=True)
        with patch.object(bot, '_init_google_sheet_persistence', side_effect=ConnectionError('offline')):
            bot._initialize_storage()
        self.assertFalse(bot._storage_state['ready'])
        self.assertFalse(bot._storage_state['initializing'])
        self.assertEqual(bot._storage_state['last_error'], 'ConnectionError')
        self.assertGreater(bot._storage_state['next_retry'], 0)
        self.assertEqual(self.send().status_code, 503)
        self.assertEqual(self.client.get('/').status_code, 200)

    def test_bootstrap_starts_only_one_background_thread(self):
        bot._storage_state.update(ready=False, initializing=False)
        with patch.object(bot.threading, 'Thread') as thread:
            bot._start_storage_init(); bot._start_storage_init()
            self.assertEqual(thread.call_count, 1)
            self.assertTrue(thread.call_args.kwargs['daemon'])
            self.assertEqual(thread.call_args.kwargs['name'], 'sheet-bootstrap')

    def test_metadata_cache_and_invalidation_after_grid_growth(self):
        bot._sheet_metadata_cache.clear()
        original = db.request
        seen = []
        def count(*args, **kwargs):
            if '?fields=sheets.properties' in args[1]:
                seen.append(1)
            return original(*args, **kwargs)
        with patch.object(db, 'request', side_effect=count):
            bot._sheet_metadata('records'); bot._sheet_metadata('records')
            self.assertEqual(len(seen), 1)
            bot._sheet_append('records', 'LINE原始事件', ['new-data'])
            bot._sheet_metadata('records')
            self.assertEqual(len(seen), 2)

    def test_429_does_not_immediately_bombard_api(self):
        response = Mock(status_code=429, headers={'Retry-After': '90'})
        response.raise_for_status.side_effect = bot.requests.HTTPError('quota', response=response)
        with patch.object(bot.requests, 'request', return_value=response) as request, patch.object(bot.time, 'sleep') as sleep:
            with self.assertRaises(bot.requests.HTTPError):
                bot._google_request('GET', 'https://sheets.googleapis.com/v4/spreadsheets/records', timeout=15)
            self.assertEqual(request.call_count, 1)
            self.assertFalse(sleep.called)
            with self.assertRaises(bot.SheetsBusy) as pending:
                bot._google_request('GET', 'https://sheets.googleapis.com/v4/spreadsheets/records', timeout=15)
            self.assertGreaterEqual(pending.exception.retry_after, 89)
            self.assertEqual(request.call_count, 1)

    def test_local_quota_defers_foreground_but_reserves_write_bucket(self):
        gate = bot.QuotaGate(limit=2)
        gate.take('read'); gate.take('read')
        with self.assertRaises(bot.SheetsBusy):
            gate.take('read')
        gate.take('write')

    def test_narrow_index_scans_5000_rows_without_loading_payloads(self):
        event = bot.MessageEvent.new_from_json_dict(self.event(ORDER))
        bot.reliable_orders.receive(event)
        header, row = db.tabs['訂單收件匣']['rows']
        db.tabs['訂單收件匣']['rows'] = [header] + [[] for _ in range(9998)] + [row]
        bot._sheet_metadata_cache.clear()
        original = db.request
        ranges_seen = []
        def count(*args, **kwargs):
            if 'values:batchGet' in args[1]:
                ranges_seen.append(kwargs['params']['ranges'])
            return original(*args, **kwargs)
        with patch.object(db, 'request', side_effect=count):
            bot.reliable_orders.refresh()
        self.assertEqual(len(ranges_seen), 2)
        self.assertTrue(all(all('!A' in r or '!C' in r for r in ranges) for ranges in ranges_seen))
        self.assertEqual(bot.reliable_orders.index['e1']['row'], 10000)

    def test_successful_reinitialization_enables_order_receipt(self):
        bot._storage_state.update(ready=False, initializing=True)
        bot._initialize_storage()
        self.assertEqual(self.client.get('/ready').status_code, 200)
        self.assertEqual(self.send().status_code, 200)
        self.assertEqual(self.status(), 'DONE')


if __name__ == '__main__':
    unittest.main(verbosity=2)
