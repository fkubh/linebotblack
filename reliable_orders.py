"""Durable Sheets inbox and atomic order commit, for ONE sync worker/instance.

The inbox and both order stores must share one spreadsheet. Never retry an
append-containing batch blindly: its completion marker resolves unknown outcomes.
"""
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from itertools import zip_longest

HEADERS = ['event_id', 'received_at', 'status', 'event_json', 'detail',
           'updated_at', 'attempts', 'result_json']
FINAL = {'DONE', 'REVIEW', 'COMMAND', 'RESOLVED'}


class RetryLater(Exception):
    pass


def cell(value):
    return {'userEnteredValue': {'stringValue': str(value)}}


class ReliableOrders:
    def __init__(self, bot, tab):
        self.bot = bot
        self.tab = tab
        self.index = {}
        self.dirty = False
        self.refresh_needed = True
        self.last_run = None
        self.last_error = ''
        bot._ensure_sheet_tab(bot.BOT_RECORD_SHEET_ID, tab, HEADERS)
        escaped = tab.replace("'", "''")
        header = bot._sheet_values(bot.BOT_RECORD_SHEET_ID, f"'{escaped}'!A1:H1")
        if not header or header[0] != HEADERS:
            raise RuntimeError('Inbox header mismatch; use a new, dedicated inbox tab')
        self.refresh()

    def refresh(self):
        b = self.bot
        properties = [s['properties'] for s in b._sheet_metadata(b.BOT_RECORD_SHEET_ID)['sheets']]
        self.sheet_ids = {p['title']: p['sheetId'] for p in properties}
        props = next(p for p in properties if p['title'] == self.tab)
        escaped = self.tab.replace("'", "''")
        index = {}
        # Read IDs/statuses only; load each payload only when actually processing.
        for start in range(2, props['gridProperties']['rowCount'] + 1, 500):
            end = min(start + 499, props['gridProperties']['rowCount'])
            response = b._google_request(
                'GET', f'https://sheets.googleapis.com/v4/spreadsheets/{b.BOT_RECORD_SHEET_ID}/values:batchGet',
                headers=b._google_headers(), timeout=15,
                params={'ranges': [f"'{escaped}'!A{start}:A{end}", f"'{escaped}'!C{start}:C{end}"],
                        'fields': 'valueRanges(values)'}, operation='INBOX INDEX',
            )
            columns = response.json().get('valueRanges', [])
            if len(columns) != 2:
                raise RuntimeError('Invalid inbox index response')
            rows = zip_longest(*(c.get('values', []) for c in columns), fillvalue=[])
            for offset, (ids, states) in enumerate(rows):
                if not ids or not ids[0]:
                    continue
                eid, state = str(ids[0]), str(states[0]) if states else 'PENDING'
                old = index.get(eid)
                # Unknown receive outcomes may append the same ID twice. A final
                # canonical row always wins; otherwise use the earliest receipt.
                if old is None or (old['status'] not in FINAL and state in FINAL):
                    index[eid] = {'row': start + offset, 'status': state}
        self.index = index
        self.refresh_needed = False

    def receive(self, event, command=False):
        b = self.bot
        if self.refresh_needed:
            self.refresh()
        payload = event.as_json_dict()
        payload.pop('replyToken', None)  # Replay never sends an old reply token.
        eid = b._line_event_id(event)
        if not eid:
            stable = [payload.get('source'), payload.get('message'), payload.get('timestamp')]
            eid = 'fallback-' + hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if eid in self.index:
            return eid, False
        encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        if len(encoded) > 45000:
            raise ValueError('Event exceeds safe inbox cell size; refusing to acknowledge')
        now = b._taipei_now_iso()
        values = [eid, now, 'COMMAND' if command else 'PENDING', encoded, '', now, '0', '{}']
        escaped = self.tab.replace("'", "''")
        url = f'https://sheets.googleapis.com/v4/spreadsheets/{b.BOT_RECORD_SHEET_ID}/values/' + b.quote(f"'{escaped}'!A:H", safe='') + ':append?valueInputOption=RAW&insertDataOption=INSERT_ROWS'
        try:
            response = b._google_request('POST', url, headers=b._google_headers(),
                                         json={'majorDimension': 'ROWS', 'values': [values]},
                                         retry=False, timeout=15, operation='INBOX RECEIVE')
            updated = response.json().get('updates', {}).get('updatedRange', '')
            match = re.search(r'!\$?A\$?(\d+):', updated)
            if not match:
                raise RuntimeError('Inbox append missing updatedRange; outcome unknown')
            self.index[eid] = {'row': int(match.group(1)), 'status': values[2]}
        except Exception:
            self.refresh_needed = True
            raise  # Never return webhook 200 when durable receipt is unconfirmed.
        return eid, True

    def read(self, eid):
        item = self.index[eid]
        escaped = self.tab.replace("'", "''")
        rows = self.bot._sheet_values(self.bot.BOT_RECORD_SHEET_ID,
                                      f"'{escaped}'!A{item['row']}:H{item['row']}")
        if not rows or str(rows[0][0]) != eid:
            raise RuntimeError('Inbox rows changed; do not sort/delete inbox rows')
        return (list(rows[0]) + [''] * 8)[:8]

    def status_update(self, eid, row, status, detail, result, attempts):
        values = [status, row[3], detail[:2000], datetime.now(timezone.utc).isoformat(timespec='seconds'), str(attempts),
                  json.dumps(result, ensure_ascii=False, separators=(',', ':'))]
        return {'updateCells': {
            'start': {'sheetId': self.sheet_ids[self.tab], 'rowIndex': self.index[eid]['row'] - 1, 'columnIndex': 2},
            'rows': [{'values': [cell(v) for v in values]}], 'fields': 'userEnteredValue',
        }}

    def batch(self, requests):
        b = self.bot
        # This batch contains appendCells. Transport failure is ambiguous, so the
        # caller MUST read the completion marker before deciding to retry.
        return b._google_request('POST', f'https://sheets.googleapis.com/v4/spreadsheets/{b.BOT_RECORD_SHEET_ID}:batchUpdate',
                                 headers=b._google_headers(), json={'requests': requests},
                                 retry=False, timeout=20, operation='ATOMIC ORDER COMMIT')

    def process(self, eid):
        b = self.bot
        row = self.read(eid)
        if row[2] in FINAL:
            self.index[eid]['status'] = row[2]
            return json.loads(row[7] or '{}')
        if row[2] == 'COMMITTING':
            # Sheets can still be processing after an HTTP timeout. Do not race
            # that in-flight batch with another append, including after restart.
            updated = datetime.fromisoformat(row[5])
            if (datetime.now(timezone.utc) - updated).total_seconds() < 300:
                raise RetryLater('Unknown commit outcome: wait five minutes before retry')
        # Reload only after a failed/unknown batch. Partial in-memory mutations
        # must never be mistaken for committed orders on the next attempt.
        if self.dirty:
            b.order_event_store.load()
            b.line_order_store.load()
            self.dirty = False
        attempts = int(row[6] or 0) + 1
        commit_started = False
        try:
            event = b.MessageEvent.new_from_json_dict(json.loads(row[3]))
            b._order_write_batch = []
            result = b._plan_reliable_order(event)
            writes = b._order_write_batch
            b._order_write_batch = None
            status = 'REVIEW' if result['kind'] == 'review' else 'DONE'
            requests = []
            for tab, values in writes:
                requests.append({'appendCells': {'sheetId': self.sheet_ids[tab],
                                 'rows': [{'values': [cell(v) for v in values]}],
                                 'fields': 'userEnteredValue'}})
            requests.append(self.status_update(eid, row, status, result.get('detail', ''), result, attempts))
            self.batch([self.status_update(eid, row, 'COMMITTING', '寫入中；結果不明時等待五分鐘再核對', {}, attempts)])
            self.index[eid]['status'] = 'COMMITTING'
            commit_started = True
            self.batch(requests)
            self.index[eid]['status'] = status
            return result
        except Exception as exc:
            b._order_write_batch = None
            self.dirty = True
            self.last_error = type(exc).__name__
            # The server might have committed but the HTTP response was lost.
            # Read that SAME row before any replay, never append again blindly.
            try:
                observed = self.read(eid)
                if observed[2] in FINAL:
                    self.index[eid]['status'] = observed[2]
                    b.order_event_store.load()
                    b.line_order_store.load()
                    self.dirty = False
                    return json.loads(observed[7] or '{}')
                if not commit_started:
                    self.batch([self.status_update(eid, row, 'ERROR', type(exc).__name__, {}, attempts)])
                    self.index[eid]['status'] = 'ERROR'
                # Never overwrite an unresolved COMMITTING marker: a late
                # successful batch may still replace it with DONE atomically.
            except Exception:
                self.refresh_needed = True
            raise

    def drain(self, limit=3, budget=25, refresh=False):
        if refresh or self.refresh_needed:
            self.refresh()
        self.last_run = self.bot._taipei_now_iso()
        started = time.monotonic()
        results = {}
        pending = sorted(((eid, item) for eid, item in self.index.items()
                          if item['status'] not in FINAL), key=lambda pair: pair[1]['row'])
        for eid, _ in pending[:limit]:
            if time.monotonic() - started >= budget:
                break
            try:
                results[eid] = self.process(eid)
            except RetryLater:
                break
            except Exception:
                self.bot.app.logger.exception('[INBOX PENDING] event=%s', eid)
                break  # Preserve receipt order; do not overtake a failed event.
        return results

    def review_action(self, eid, admin_id, retry=False):
        self.refresh()
        if eid not in self.index:
            raise ValueError('找不到事件 ID')
        row = self.read(eid)
        if row[2] != 'REVIEW':
            raise ValueError('僅能對待人工確認的事件操作')
        status = 'PENDING' if retry else 'RESOLVED'
        result = json.loads(row[7] or '{}')
        result['reviewed_by'] = admin_id
        detail = '管理員要求重新解析原始事件' if retry else '管理員已人工核對並處理；此操作不會新增訂單'
        self.batch([self.status_update(eid, row, status, detail, result, int(row[6] or 0))])
        self.index[eid]['status'] = status

    def report(self):
        counts = {}
        for item in self.index.values():
            counts[item['status']] = counts.get(item['status'], 0) + 1
        return {'version': 'reliable-inbox-v1', 'counts': counts,
                'pending': sum(v for k, v in counts.items() if k not in FINAL),
                'last_replay': self.last_run, 'last_error_type': self.last_error,
                'review_event_ids': [eid for eid, item in self.index.items() if item['status'] == 'REVIEW'][:20]}
