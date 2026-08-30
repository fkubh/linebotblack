import os
import re
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote

from order_summary_v1 import (
    Event, EventStore, LineOrderStore, generate_daily_summary, generate_unassigned_summary, parse_line_command, parse_rows, parse_line_order_text, merge_sheet_and_line_orders, orders_core_equal, extract_driver_info, detect_line_event_alias, PARSER_VERSION
)

from google.oauth2 import service_account
from google.auth.transport.requests import Request as GoogleAuthRequest
import requests

from flask import Flask, request, abort

from linebot import (
    LineBotApi,
    WebhookHandler
)

from linebot.exceptions import (
    InvalidSignatureError
)

from linebot.models import (
    MessageEvent,
    TextMessage,
    TextSendMessage,
)


# =====================================================
# Flask
# =====================================================

app = Flask(__name__)


# =====================================================
# LINE Bot 設定
# =====================================================

CHANNEL_ACCESS_TOKEN = os.environ.get("CHANNEL_ACCESS_TOKEN")

CHANNEL_SECRET = os.environ.get("CHANNEL_SECRET")


if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise RuntimeError(
        "缺少 LINE 環境變數：請設定 CHANNEL_ACCESS_TOKEN 與 CHANNEL_SECRET"
    )


line_bot_api = LineBotApi(
    CHANNEL_ACCESS_TOKEN
)

handler = WebhookHandler(
    CHANNEL_SECRET
)

# =====================================================
# Google Sheet 司機資料
# =====================================================

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SHEET_TAB = os.environ.get("GOOGLE_SHEET_TAB", "drivers")

if not GOOGLE_SHEET_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
    raise RuntimeError(
        "缺少 Google Sheet 環境變數：請設定 GOOGLE_SHEET_ID 與 GOOGLE_SERVICE_ACCOUNT_JSON"
    )

try:
    GOOGLE_SERVICE_ACCOUNT_INFO = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
except json.JSONDecodeError as exc:
    raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON 不是有效的 JSON") from exc

GOOGLE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
GOOGLE_CREDENTIALS = service_account.Credentials.from_service_account_info(
    GOOGLE_SERVICE_ACCOUNT_INFO,
    scopes=GOOGLE_SCOPES,
)

# =====================================================
# 訂單每日簡表 V1
# =====================================================
# 訂單可使用另一份 Google Sheet；若未設定則停用簡表功能，不影響原本司機黑名單功能。
ORDER_SHEET_ID = os.environ.get("ORDER_SHEET_ID", "").strip()
ORDER_EVENTS_PATH = os.environ.get("ORDER_EVENTS_PATH", "order_events_v1.json")
ORDER_LINE_ORDERS_PATH = os.environ.get("ORDER_LINE_ORDERS_PATH", "line_orders_v1.json")
ORDER_SHEET_MAX_COL = os.environ.get("ORDER_SHEET_MAX_COL", "H")
ORDER_SHEET_TAB_TEMPLATE = os.environ.get("ORDER_SHEET_TAB_TEMPLATE", "{year}/{month}月")
ORDER_SHOW_UNASSIGNED = os.environ.get("ORDER_SHOW_UNASSIGNED", "false").lower() in {"1", "true", "yes", "on"}
order_event_store = EventStore(ORDER_EVENTS_PATH)
line_order_store = LineOrderStore(ORDER_LINE_ORDERS_PATH)


def _taipei_now_iso():
    return datetime.now(ZoneInfo("Asia/Taipei")).isoformat(timespec="minutes")


def _sheet_values(spreadsheet_id, range_name):
    if not GOOGLE_CREDENTIALS.valid:
        GOOGLE_CREDENTIALS.refresh(GoogleAuthRequest())
    headers = {"Authorization": f"Bearer {GOOGLE_CREDENTIALS.token}"}
    # A1 range 可能包含 /、空白、中文等字元，因此整段做 URL encode。
    encoded_range = quote(range_name, safe="")
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/"
        f"{encoded_range}"
    )
    response = requests.get(url, headers=headers, timeout=15)
    if response.status_code != 200:
        app.logger.error("訂單 Google Sheets API 錯誤 %s：%s", response.status_code, response.text)
    response.raise_for_status()
    return response.json().get("values", [])


def _resolve_order_sheet_date(date_text):
    """支援 M/D 與 YYYY/M/D；未填年份時使用台北目前年份。"""
    full = re.search(r"(?<!\d)(20\d{2})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})(?!\d)", date_text)
    if full:
        return int(full.group(1)), int(full.group(2)), int(full.group(3))

    short = re.search(r"(?<!\d)(\d{1,2})\s*/\s*(\d{1,2})(?!\d)", date_text)
    if not short:
        raise ValueError("日期格式請輸入 M/D，例如：簡表 8/28；也可輸入 YYYY/M/D")

    year = datetime.now(ZoneInfo("Asia/Taipei")).year
    return year, int(short.group(1)), int(short.group(2))


def load_orders_for_date(date_text):
    if not ORDER_SHEET_ID:
        raise RuntimeError("尚未設定 ORDER_SHEET_ID")

    year, month, day = _resolve_order_sheet_date(date_text)
    tab_name = ORDER_SHEET_TAB_TEMPLATE.format(year=year, month=month, day=day)

    # 分頁名稱含 / 時，A1 notation 必須用單引號包起來，例如：'2026/8月'!A:H
    range_name = f"'{tab_name}'!A:{ORDER_SHEET_MAX_COL}"
    app.logger.info("讀取訂單分頁：%s", tab_name)
    rows = _sheet_values(ORDER_SHEET_ID, range_name)
    sheet_orders = parse_rows(rows)
    return merge_sheet_and_line_orders(sheet_orders, line_order_store)


def order_history_by_id(order_id):
    events = [e for e in order_event_store.events if e.order_id.upper() == order_id.upper()]
    events.sort(key=lambda e: e.at)
    if not events:
        return f"{order_id}\n尚無調度／異動紀錄"
    assigned = 0
    recalled = 0
    edited = 0
    cancelled = False
    active_driver = ""
    active_plate = ""
    for e in events:
        if e.event_type == "assigned":
            assigned += 1
            active_driver, active_plate = e.driver_name, e.plate
            cancelled = False
        elif e.event_type in {"recalled", "unassigned"}:
            recalled += 1
            active_driver = active_plate = ""
        elif e.event_type == "edited":
            edited += 1
        elif e.event_type == "cancelled":
            cancelled = True
            active_driver = active_plate = ""
        elif e.event_type == "restored":
            cancelled = False
    if cancelled:
        current = "取消"
    elif active_driver or active_plate:
        current = "重派・已派" if assigned >= 2 and recalled >= 1 else "已派"
        if edited:
            current = "異動・" + current
    elif edited:
        current = "異動・未派"
    else:
        current = "未派"
    lines = [
        f"{order_id}｜目前：{current}",
        f"異動 {edited} 次／指派 {assigned} 次／拉回 {recalled} 次",
    ]
    if active_driver or active_plate:
        lines.append(f"目前司機：{active_driver} {active_plate}".rstrip())
    lines.append("")
    for e in events:
        detail = " ".join(x for x in [e.driver_name, e.plate, e.note] if x)
        lines.append(f"{e.at}｜{e.event_type}{('｜' + detail) if detail else ''}")
    return "\n".join(lines)


def _split_line_text(text, max_chars=4800):
    """依換行切 LINE 長文字，避免直接截斷造成訂單看起來像漏抓。"""
    text = str(text or "")
    if len(text) <= max_chars:
        return [text]

    chunks = []
    current = []
    current_len = 0
    for line in text.splitlines():
        add_len = len(line) + (1 if current else 0)
        if current and current_len + add_len > max_chars:
            chunks.append("\n".join(current))
            current = [line]
            current_len = len(line)
        elif len(line) > max_chars:
            if current:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            for start in range(0, len(line), max_chars):
                chunks.append(line[start:start + max_chars])
        else:
            current.append(line)
            current_len += add_len
    if current:
        chunks.append("\n".join(current))
    return [c for c in chunks if c]


def _line_source_target_id(event):
    """取得群組 / 聊天室 / 個人 push target。"""
    source = getattr(event, "source", None)
    if source is None:
        return ""
    return (
        getattr(source, "group_id", "")
        or getattr(source, "room_id", "")
        or getattr(source, "user_id", "")
        or ""
    )



def _events_for_order_id(order_id):
    return sorted(
        [e for e in order_event_store.events if e.order_id.upper() == order_id.upper()],
        key=lambda e: e.at,
    )


def _latest_event(order_id, event_type=None):
    events = _events_for_order_id(order_id)
    if event_type:
        events = [e for e in events if e.event_type == event_type]
    return events[-1] if events else None


def _add_event(event_obj):
    # LINE webhook retry / repeated paste protection: skip an identical latest event
    # within the same Taipei minute.
    events = _events_for_order_id(event_obj.order_id)
    if events:
        old = events[-1]
        if (
            old.at == event_obj.at
            and old.event_type == event_obj.event_type
            and old.driver_name == event_obj.driver_name
            and old.plate == event_obj.plate
            and old.note == event_obj.note
            and (old.changes or {}) == (event_obj.changes or {})
        ):
            return
    order_event_store.add(event_obj)


def _sheet_orders_only_for_date(date_text):
    if not ORDER_SHEET_ID:
        return []
    year, month, day = _resolve_order_sheet_date(date_text)
    tab_name = ORDER_SHEET_TAB_TEMPLATE.format(year=year, month=month, day=day)
    range_name = f"'{tab_name}'!A:{ORDER_SHEET_MAX_COL}"
    rows = _sheet_values(ORDER_SHEET_ID, range_name)
    return parse_rows(rows)


def capture_line_activity(event, text):
    """Capture full LINE orders and standardized order events.

    Returns True for standalone event messages so they do not fall through to
    unrelated blacklist matching.
    """
    now = _taipei_now_iso()
    full_order = parse_line_order_text(text)
    oid_match = re.search(r"\b(?:\d{2}KK\d{9}|ORD\d{10}|[A-Z]{3}\d{6})\b", text, re.I)
    oid = full_order.order_id if full_order else (oid_match.group(0).upper() if oid_match else "")
    alias_event = detect_line_event_alias(text)

    if full_order:
        existing_line = line_order_store.get(full_order.order_id)
        try:
            sheet_orders = _sheet_orders_only_for_date(full_order.date)
        except Exception as exc:
            app.logger.warning("讀取 Sheet 做 LINE 衝突檢查失敗：%s", exc)
            sheet_orders = []

        sheet_same_id = [o for o in sheet_orders if o.order_id.upper() == full_order.order_id.upper()]
        reference = existing_line.order if existing_line else (sheet_same_id[0] if len(sheet_same_id) == 1 else None)

        explicit_edit = alias_event == "edited"
        latest_any = _latest_event(full_order.order_id)
        pending_edit = bool(
            latest_any
            and latest_any.event_type == "edited"
            and (not existing_line or latest_any.at >= existing_line.updated_at)
        )

        conflict = False
        if len(sheet_same_id) > 1 and not existing_line:
            conflict = True
        elif reference and not orders_core_equal(reference, full_order) and not (explicit_edit or pending_edit):
            conflict = True

        if conflict:
            _add_event(Event(
                order_id=full_order.order_id,
                event_type="conflict",
                at=now,
                note="同訂單編號但核心內容不同；未偵測到可接受的訂單修正",
            ))
        else:
            if existing_line:
                mode = "override" if (explicit_edit or pending_edit or not orders_core_equal(existing_line.order, full_order)) else existing_line.mode
                line_order_store.upsert(full_order, now, mode=mode)
            elif not sheet_same_id:
                line_order_store.upsert(full_order, now, mode="line_added")
            elif explicit_edit or pending_edit:
                line_order_store.upsert(full_order, now, mode="override")

            if any(e.event_type == "conflict" for e in _events_for_order_id(full_order.order_id)):
                _add_event(Event(
                    order_id=full_order.order_id,
                    event_type="conflict_resolved",
                    at=now,
                    note="已收到可接受的最新版完整訂單",
                ))

            if explicit_edit:
                _add_event(Event(
                    order_id=full_order.order_id,
                    event_type="edited",
                    at=now,
                    note="LINE 完整訂單修正",
                ))

        # 取消／拉回／改司機 may be posted together with the full order.
        if alias_event in {"cancelled", "recalled", "driver_change"}:
            _add_event(Event(
                order_id=full_order.order_id,
                event_type=alias_event,
                at=now,
                note=text[:500],
            ))

        driver_name, plate = extract_driver_info(text)
        if driver_name or plate:
            events = _events_for_order_id(full_order.order_id)
            active_name = ""
            active_plate = ""
            assigned = False
            for e in events:
                if e.event_type == "assigned":
                    assigned = True
                    active_name, active_plate = e.driver_name, e.plate
                elif e.event_type in {"recalled", "unassigned", "driver_change", "cancelled"}:
                    assigned = False
                    active_name = active_plate = ""

            same_driver = assigned and (
                (not driver_name or not active_name or driver_name == active_name)
                and (not plate or not active_plate or plate == active_plate)
            )
            if not same_driver:
                _add_event(Event(
                    order_id=full_order.order_id,
                    event_type="assigned",
                    at=now,
                    driver_name=driver_name,
                    plate=plate,
                    note="由完整訂單下方司機資料自動辨識",
                ))
        return False

    if alias_event and oid:
        _add_event(Event(
            order_id=oid,
            event_type=alias_event,
            at=now,
            note=text[:500],
        ))
        return True

    return False

def handle_order_v1_command(event, text):
    cmd = parse_line_command(text)
    if not cmd:
        return False

    try:
        if cmd["command"] in {"summary", "summary_unassigned"}:
            orders = load_orders_for_date(cmd["date"])
            if cmd["command"] == "summary_unassigned":
                summary = generate_unassigned_summary(
                    orders, cmd["date"], order_event_store
                )
            else:
                summary = generate_daily_summary(
                    orders, cmd["date"], order_event_store,
                    include_cancelled=True,
                    show_unassigned=ORDER_SHOW_UNASSIGNED,
                )
            # LINE 單一文字訊息有長度上限。舊版使用 summary[:5000]，
            # 訂單量大時會直接把後半段簡表截掉。V1.2 改成依換行安全分段，
            # 一次 reply 最多可帶 5 則訊息；8/28 這類 7k+ 字簡表會完整分成 2 則。
            chunks = _split_line_text(summary, max_chars=4800)
            messages = [TextSendMessage(text=chunk) for chunk in chunks[:5]]
            line_bot_api.reply_message(event.reply_token, messages)

            # 極端情況若超過 5 段，再用 push 補送剩餘內容。
            if len(chunks) > 5:
                target_id = _line_source_target_id(event)
                if target_id:
                    for i in range(5, len(chunks), 5):
                        batch = [TextSendMessage(text=chunk) for chunk in chunks[i:i + 5]]
                        line_bot_api.push_message(target_id, batch)
            return True

        if cmd["command"] == "history":
            text_out = order_history_by_id(cmd["order_id"])
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=text_out[:5000]))
            return True

        if cmd["command"] == "event":
            event_obj = Event(
                order_id=cmd["order_id"],
                event_type=cmd["event_type"],
                at=_taipei_now_iso(),
                driver_name=cmd.get("driver_name", ""),
                plate=cmd.get("plate", ""),
                note=cmd.get("note", ""),
                changes=cmd.get("changes", {}),
            )
            order_event_store.add(event_obj)
            labels = {
                "assigned": "已派", "recalled": "已拉回", "edited": "已記錄異動",
                "cancelled": "已取消", "restored": "已恢復", "driver_change": "已記錄改司機",
            }
            reply = f"✅ {cmd['order_id']} {labels.get(cmd['event_type'], '已更新')}"
            if cmd.get("driver_name") or cmd.get("plate"):
                reply += f"\n司機：{cmd.get('driver_name','')} {cmd.get('plate','')}".rstrip()
            if cmd.get("note"):
                reply += f"\n備註：{cmd['note']}"
            if cmd.get("changes"):
                display = "、".join(f"{k}={v}" for k, v in cmd["changes"].items())
                reply += f"\n套用：{display}"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return True
    except Exception as exc:
        app.logger.exception("訂單 V1 指令失敗：%s", exc)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"⚠️ 訂單功能處理失敗：{exc}")
        )
        return True

    return False

# 快取 30 秒：修改試算表後，最慢約 30 秒套用；避免每一則 LINE 訊息都打一次 Google API。
SHEET_CACHE_SECONDS = int(os.environ.get("SHEET_CACHE_SECONDS", "30"))
_sheet_cache = {"loaded_at": 0.0, "blocked_list": [], "special_notes": []}

blocked_list = []
special_notes = []


def _cell(row, index):
    if index >= len(row):
        return ""
    return str(row[index]).strip()


def _enabled(value):
    return str(value).strip().lower() not in {"false", "0", "no", "off", "停用"}


def load_driver_lists(force=False):
    global blocked_list, special_notes

    now = time.time()
    if (
        not force
        and _sheet_cache["loaded_at"]
        and now - _sheet_cache["loaded_at"] < SHEET_CACHE_SECONDS
    ):
        blocked_list = _sheet_cache["blocked_list"]
        special_notes = _sheet_cache["special_notes"]
        return blocked_list, special_notes

    range_name = f"{GOOGLE_SHEET_TAB}!A:J"
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_SHEET_ID}/values/"
        f"{range_name}"
    )

    # 使用 Service Account 取得 OAuth Access Token
    if not GOOGLE_CREDENTIALS.valid:
        GOOGLE_CREDENTIALS.refresh(GoogleAuthRequest())

    headers = {
        "Authorization": f"Bearer {GOOGLE_CREDENTIALS.token}"
    }

    response = requests.get(
        url,
        headers=headers,
        timeout=10
    )

    if response.status_code != 200:
        app.logger.error(
            "Google Sheets API 錯誤 %s：%s",
            response.status_code,
            response.text
        )

    response.raise_for_status()
    values = response.json().get("values", [])

    if not values:
        blocked_list = []
        special_notes = []
        return blocked_list, special_notes

    headers = [str(x).strip().lower() for x in values[0]]
    expected = [
        "type", "name", "plate", "phone", "vehicle",
        "color", "bank_code", "account", "note", "enabled"
    ]
    if headers[:10] != expected:
        raise RuntimeError(
            "drivers 工作表欄位不正確。A1:J1 必須是：" + ", ".join(expected)
        )

    new_blocked = []
    new_special = []

    for row in values[1:]:
        row_type = _cell(row, 0).lower()
        name = _cell(row, 1)

        if not name or not _enabled(_cell(row, 9)):
            continue

        phone_text = _cell(row, 3)
        phones = [
            p.strip() for p in re.split(r"[,，;；/\n]+", phone_text) if p.strip()
        ]

        item = {
            "name": name,
            "plate": _cell(row, 2),
            "phone": phones,
            "vehicle": _cell(row, 4),
            "color": _cell(row, 5),
            "bank_code": _cell(row, 6),
            "account": _cell(row, 7),
            "note": _cell(row, 8),
        }

        if row_type == "blacklist":
            new_blocked.append(item)
        elif row_type == "special":
            new_special.append(item)

    blocked_list = new_blocked
    special_notes = new_special
    _sheet_cache["loaded_at"] = now
    _sheet_cache["blocked_list"] = new_blocked
    _sheet_cache["special_notes"] = new_special

    app.logger.info(
        "Google Sheet loaded: blacklist=%s, special=%s",
        len(blocked_list),
        len(special_notes),
    )
    return blocked_list, special_notes


def display_phone(value):
    if isinstance(value, list):
        cleaned = [normalize_phone(v) for v in value if normalize_phone(v)]
        return "、".join(cleaned) if cleaned else "未提供"
    normalized = normalize_phone(value)
    return normalized or "未提供"


# =====================================================
# 資料格式化
# =====================================================

def normalize_phone(value):
    """
    電話格式統一

    0932-395-446
    0932395446
    +886932395446
    886932395446

    最後都轉成：

    0932395446
    """

    if not value:
        return ""

    value = str(value).strip()

    # 移除空白、-、括號
    value = re.sub(r"[\s\-\(\)]", "", value)

    # +886932395446
    if value.startswith("+886"):
        value = "0" + value[4:]

    # 886932395446
    elif value.startswith("8869"):
        value = "0" + value[3:]

    return value


def normalize_plate(value):
    """
    車號格式統一

    RFR-9770
    RFR 9770
    RFR9770

    都轉成：

    RFR9770
    """

    if not value:
        return ""

    return re.sub(
        r"[^A-Za-z0-9]",
        "",
        str(value)
    ).upper()


def normalize_account(value):
    """
    匯款帳號只保留數字
    """

    if not value:
        return ""

    return re.sub(
        r"\D",
        "",
        str(value)
    )


# =====================================================
# 回覆黑名單資料
# =====================================================

def reply_blacklist(event, item, match_type):

    name = item.get("name", "未提供")
    phone = display_phone(item.get("phone", []))
    plate = item.get("plate", "")
    vehicle = item.get("vehicle", "未提供")
    color = item.get("color", "未提供")
    bank_code = item.get("bank_code", "")
    account = item.get("account", "")
    note = item.get("note", "").strip()

    if not phone:
        phone = "未提供"

    if not plate:
        plate = "未提供"

    if not bank_code:
        bank_code = "未提供"

    if not account:
        account = "未提供"

    reply_text = (
        f"🚨🚨 黑名單警告 🚨🚨\n"
        f"\n"
        f"匹配方式：{match_type}\n"
        f"\n"
        f"駕駛：{name}\n"
        f"電話：{phone}\n"
        f"車號：{plate}\n"
        f"車型：{vehicle}\n"
        f"顏色：{color}\n"
        f"銀行代碼：{bank_code}\n"
        f"匯款帳號：{account}\n"
    )

    # 有填備註才顯示
    if note:
        reply_text += (
            f"\n"
            f"📝 黑名單備註：\n"
            f"{note}\n"
        )

    reply_text += (
        f"\n"
        f"🚨 此司機永久禁派 🚨"
    )

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

# =====================================================
# 回覆特殊註記
# =====================================================

def reply_special_note(event, item, match_type):

    name = item.get("name", "未提供")
    phone = display_phone(item.get("phone", []))
    plate = item.get("plate", "")
    vehicle = item.get("vehicle", "未提供")
    color = item.get("color", "未提供")
    note = item.get(
        "note",
        "請留意此司機特殊狀況"
    )

    if not phone:
        phone = "未提供"

    if not plate:
        plate = "未提供"

    reply_text = (
        f"⚠️⚠️ 司機特殊提醒 ⚠️⚠️\n"
        f"\n"
        f"匹配方式：{match_type}\n"
        f"\n"
        f"駕駛：{name}\n"
        f"電話：{phone}\n"
        f"車號：{plate}\n"
        f"車型：{vehicle}\n"
        f"顏色：{color}\n"
        f"\n"
        f"📝 特殊註記：\n"
        f"{note}\n"
        f"\n"
        f"⚠️ 此司機並非黑名單\n"
        f"⚠️ 請派單時留意"
    )

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )


# =====================================================
# 姓名比對
# 優先順序 ①
# =====================================================

def check_name(text):

    text_lower = text.lower()

    # -----------------------------
    # 先檢查黑名單
    # -----------------------------

    for item in blocked_list:

        name = item.get(
            "name",
            ""
        ).strip()

        if not name:
            continue

        if name.lower() in text_lower:
            return "blacklist", item, "姓名"


    # -----------------------------
    # 再檢查特殊註記
    # -----------------------------

    for item in special_notes:

        name = item.get(
            "name",
            ""
        ).strip()

        if not name:
            continue

        if name.lower() in text_lower:
            return "special", item, "姓名"

    return None


# =====================================================
# 車號比對
# 優先順序 ②
# =====================================================

def check_plate(text):

    clean_text = normalize_plate(text)

    if not clean_text:
        return None

    # -----------------------------
    # 黑名單
    # -----------------------------

    for item in blocked_list:

        plate = normalize_plate(
            item.get("plate", "")
        )

        if not plate:
            continue

        if plate in clean_text:
            return "blacklist", item, "車號"


    # -----------------------------
    # 特殊註記
    # -----------------------------

    for item in special_notes:

        plate = normalize_plate(
            item.get("plate", "")
        )

        if not plate:
            continue

        if plate in clean_text:
            return "special", item, "車號"

    return None


# =====================================================
# 電話比對
# 優先順序 ③
# =====================================================
def check_phone(text):

    # 抓出訊息中的 8～20 位數字
    numbers = re.findall(
        r"\d{8,20}",
        text
    )

    normalized_numbers = []

    for number in numbers:
        normalized = normalize_phone(number)

        if normalized:
            normalized_numbers.append(normalized)


    # =================================================
    # 黑名單電話比對
    # =================================================

    for item in blocked_list:

        phones = item.get("phone", [])

        # 如果只有一支電話，轉成 list
        if isinstance(phones, str):
            phones = [phones]

        for phone in phones:

            normalized_phone = normalize_phone(phone)

            if normalized_phone in normalized_numbers:
                return "blacklist", item, "電話"


    # =================================================
    # 特殊註記電話比對
    # =================================================

    for item in special_notes:

        phones = item.get("phone", [])

        # 如果只有一支電話，轉成 list
        if isinstance(phones, str):
            phones = [phones]

        for phone in phones:

            normalized_phone = normalize_phone(phone)

            if normalized_phone in normalized_numbers:
                return "special", item, "電話"


    return None
    
# =====================================================
# 匯款資料比對
# 優先順序 ④
# =====================================================

def check_account(text):

    numbers = re.findall(
        r"\d{8,20}",
        text
    )

    # -----------------------------
    # 黑名單
    # -----------------------------

    for item in blocked_list:

        account = normalize_account(
            item.get("account", "")
        )

        if not account:
            continue

        for number in numbers:

            if normalize_account(number) == account:
                return "blacklist", item, "匯款帳號"


    # -----------------------------
    # 特殊註記
    # -----------------------------

    for item in special_notes:

        account = normalize_account(
            item.get("account", "")
        )

        if not account:
            continue

        for number in numbers:

            if normalize_account(number) == account:
                return "special", item, "匯款帳號"

    return None


# =====================================================
# 健康檢查（Render / 瀏覽器測試用）
# =====================================================

@app.route("/", methods=["GET"])
def health_check():
    return f"LINE bot is running | order parser {PARSER_VERSION}", 200


# =====================================================
# LINE Webhook
# =====================================================

@app.route("/callback", methods=["POST"])
def callback():

    # 取得 LINE Signature
    signature = request.headers.get(
        "X-Line-Signature"
    )

    if not signature:
        abort(400)


    # 取得訊息內容
    body = request.get_data(
        as_text=True
    )

    app.logger.info(
        "Request body: " + body
    )


    # LINE 驗證
    try:

        handler.handle(
            body,
            signature
        )

    except InvalidSignatureError:

        print(
            "Invalid signature. "
            "Please check your "
            "channel access token/channel secret."
        )

        abort(400)


    return "OK"


# =====================================================
# 收到 LINE 文字訊息
# =====================================================

@handler.add(
    MessageEvent,
    message=TextMessage
)
def handle_message(event):

    text = event.message.text.strip()

    # 訂單解析器版本檢查：可在 LINE 輸入「版本」確認 Render 目前實際載入哪一版。
    if text in {"版本", "訂單版本", "parser version"}:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"訂單解析器：{PARSER_VERSION}")
        )
        return

    # V1.6：先被動記錄 LINE 完整訂單／取消／拉回／改司機／訂單修正。
    try:
        if capture_line_activity(event, text):
            return
    except Exception as exc:
        app.logger.exception("LINE 訂單自動記錄失敗：%s", exc)

    # 每則訊息處理前確認 Google Sheet 快取；預設每 30 秒更新一次。
    try:
        load_driver_lists()
    except Exception as exc:
        app.logger.exception("讀取 Google Sheet 失敗：%s", exc)
        # 若先前已有成功快取，繼續使用舊資料，避免 Google 暫時異常時 Bot 完全失效。
        if not blocked_list and not special_notes:
            return

    if not text:
        return


    # =================================================
    # ① 姓名
    # =================================================

    result = check_name(text)

    if result:

        result_type, item, match_type = result

        if result_type == "blacklist":

            reply_blacklist(
                event,
                item,
                match_type
            )

        else:

            reply_special_note(
                event,
                item,
                match_type
            )

        return


    # =================================================
    # ② 車號
    # =================================================

    result = check_plate(text)

    if result:

        result_type, item, match_type = result

        if result_type == "blacklist":

            reply_blacklist(
                event,
                item,
                match_type
            )

        else:

            reply_special_note(
                event,
                item,
                match_type
            )

        return


    # =================================================
    # ③ 電話
    # =================================================

    result = check_phone(text)

    if result:

        result_type, item, match_type = result

        if result_type == "blacklist":

            reply_blacklist(
                event,
                item,
                match_type
            )

        else:

            reply_special_note(
                event,
                item,
                match_type
            )

        return


    # =================================================
    # ④ 匯款帳號
    # =================================================

    result = check_account(text)

    if result:

        result_type, item, match_type = result

        if result_type == "blacklist":

            reply_blacklist(
                event,
                item,
                match_type
            )

        else:

            reply_special_note(
                event,
                item,
                match_type
            )

        return

    # =================================================
    # ⑤ 訂單每日簡表 / 調度事件 V1
    # 保留原本黑名單與特殊註記優先權，安全檢查通過後才寫入調度事件。
    # =================================================
    if handle_order_v1_command(event, text):
        return


# =====================================================
# 啟動 Flask
# =====================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
