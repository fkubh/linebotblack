/** Bind this script to the SAME Bot record spreadsheet.
 * Set Script Properties: BOT_BASE_URL and REPLAY_SECRET.
 * Install a time-driven trigger for replayPendingOrders (every minute).
 * No messages, email, or LINE notifications are sent by this script.
 */
function replayPendingOrders() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) return;
  try {
    const props = PropertiesService.getScriptProperties();
    const base = (props.getProperty('BOT_BASE_URL') || '').replace(/\/$/, '');
    const secret = props.getProperty('REPLAY_SECRET') || '';
    const tab = props.getProperty('BOT_INBOX_TAB') || '訂單收件匣';
    if (!/^https:\/\//.test(base) || secret.length < 32) {
      throw new Error('Set BOT_BASE_URL (https) and REPLAY_SECRET (32+ characters) in Script Properties.');
    }
    const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(tab);
    if (!sheet) throw new Error('Inbox tab missing; first deploy the bot and confirm it created the inbox.');
    const last = sheet.getLastRow();
    const terminal = new Set(['DONE', 'REVIEW', 'COMMAND', 'RESOLVED']);
    const states = new Map();
    // Scan narrow columns in bounded pages; do not load raw order text.
    for (let start = 2; start <= last; start += 500) {
      const count = Math.min(500, last - start + 1);
      const rows = sheet.getRange(start, 1, count, 3).getValues();
      rows.forEach(row => {
        if (!row[0]) return;
        const id = String(row[0]), state = String(row[2]);
        if (!states.has(id) || (!terminal.has(states.get(id)) && terminal.has(state))) {
          states.set(id, state);
        }
      });
    }
    let pending = 0, review = 0, done = 0;
    states.forEach(state => {
      if (!terminal.has(state)) pending++;
      if (state === 'REVIEW') review++;
      if (state === 'DONE') done++;
    });
    const book = SpreadsheetApp.getActiveSpreadsheet();
    const monitor = book.getSheetByName('收件監控') || book.insertSheet('收件監控');
    monitor.getRange(1, 1, 8, 2).setValues([
      ['項目', '狀態'], ['排程最近執行', new Date().toISOString()],
      ['待重試', pending], ['待人工確認', review], ['已完成', done],
      ['本次補處理', pending ? '準備呼叫' : '無待處理，不呼叫服務'],
      ['範圍', '只統計已保存的事件，無法偵測從未送達的訊息'],
      ['說明', 'COMMITTING 結果不明時會等待五分鐘再重試']
    ]);
    if (!pending) return; // No pending work: avoid unnecessary requests to Render.
    const response = UrlFetchApp.fetch(base + '/internal/replay', {
      method: 'post', headers: {Authorization: 'Bearer ' + secret},
      muteHttpExceptions: true, followRedirects: false
    });
    const code = response.getResponseCode();
    if (code !== 200) {
      monitor.getRange(6, 2).setValue('HTTP ' + code + '；保留待重試');
      throw new Error('Replay HTTP ' + code + '; inbox records remain for retry.');
    }
    const result = JSON.parse(response.getContentText());
    monitor.getRange(6, 2).setValue('已呼叫；剩餘待重試 ' + result.pending + ' 筆');
    console.log(JSON.stringify({pending: result.pending, counts: result.counts,
                                last_error_type: result.last_error_type}));
  } finally {
    lock.releaseLock();
  }
}
