import os
import re
import json
import time
from datetime import datetime, timedelta
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

GOOGLE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
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

# V1.6.9：Render Free 沒有 Persistent Disk，因此把訂單狀態永久存到獨立 Google Sheet。
# 可在 Render 用 BOT_RECORD_SHEET_ID 覆蓋；未設定時使用目前指定的 Bot 紀錄表。
BOT_RECORD_SHEET_ID = os.environ.get(
    "BOT_RECORD_SHEET_ID",
    "1upIa52zC6J_UEDz700Lv2F-hlbgU_x7p-ZZfVfCND5M",
).strip()
BOT_EVENT_TAB = os.environ.get("BOT_EVENT_TAB", "訂單事件").strip() or "訂單事件"
BOT_LINE_ORDER_TAB = os.environ.get("BOT_LINE_ORDER_TAB", "LINE訂單").strip() or "LINE訂單"

# V1.6.10：只有包車群＋測試群可寫入 LINE 訂單／調度狀態。
# 先部署後在兩群輸入「群組ID」取得 ID，再填入 Render 環境變數。
PACKAGE_GROUP_ID = os.environ.get("PACKAGE_GROUP_ID", "Cbec1868a3b62f4210cb9ade284cd8409").strip()
TEST_GROUP_ID = os.environ.get("TEST_GROUP_ID", "C89a73c61413eaa4d761ae9e2e11cf96f").strip()
BOT_RECORD_RETENTION_DAYS = int(os.environ.get("BOT_RECORD_RETENTION_DAYS", "60"))
_cleanup_state = {"last_run": None}

# V1.6.3 簡表密碼與管理員權限
# 使用方式：簡表 9/2 9353、未派 9/2 9353
SUMMARY_ACCESS_CODE = os.environ.get("SUMMARY_ACCESS_CODE", "9353").strip()
SUMMARY_PRIVATE_ONLY = False  # V1.6.5：簡表允許群組與私訊，僅以密碼控管

# 只有這個 LINE User ID 可以查詢「誰曾經私訊過 Bot」。
# 請先私訊 Bot 輸入「我的ID」，再把取得的 U... 填到 Render：
# SUMMARY_ADMIN_USER_ID=Uxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
SUMMARY_ADMIN_USER_ID = os.environ.get("SUMMARY_ADMIN_USER_ID", "").strip()
BOT_INTERACTIONS_PATH = os.environ.get("BOT_INTERACTIONS_PATH", "bot_interactions_v1.json")
BOT_USAGE_PATH = os.environ.get("BOT_USAGE_PATH", "bot_usage_v1.json")
# 啟動初期先建立相容物件；V1.6.9 在 Google Sheet helper 定義完成後
# 會用永久 Google Sheet Store 取代。
order_event_store = EventStore(ORDER_EVENTS_PATH)
line_order_store = LineOrderStore(ORDER_LINE_ORDERS_PATH)



def _line_user_id(event):
    source = getattr(event, "source", None)
    return (getattr(source, "user_id", "") or "").strip() if source else ""


def _line_source_type(event):
    source = getattr(event, "source", None)
    if source is None:
        return ""
    source_type = (getattr(source, "type", "") or "").strip().lower()
    if source_type:
        return source_type
    if getattr(source, "group_id", ""):
        return "group"
    if getattr(source, "room_id", ""):
        return "room"
    if getattr(source, "user_id", ""):
        return "user"
    return ""


def _line_group_id(event):
    source = getattr(event, "source", None)
    return (getattr(source, "group_id", "") or "").strip() if source else ""


def _authorized_order_group_ids():
    return {gid for gid in (PACKAGE_GROUP_ID, TEST_GROUP_ID) if gid}


def _is_authorized_order_source(event):
    """V1.6.10：LINE 訂單/派車狀態只接受包車群與測試群。"""
    if _line_source_type(event) != "group":
        return False
    gid = _line_group_id(event)
    allowed = _authorized_order_group_ids()
    # 尚未填 Group ID 時先進入設定模式：不寫訂單，避免誤收其他群資料。
    return bool(allowed and gid in allowed)


def _is_private_line_chat(event):
    return _line_source_type(event) == "user"


def _summary_password_ok(text: str) -> bool:
    if not SUMMARY_ACCESS_CODE:
        return False
    parts = (text or "").strip().split()
    return bool(parts) and parts[-1] == SUMMARY_ACCESS_CODE


def _strip_summary_password(text: str) -> str:
    parts = (text or "").strip().split()
    if parts and parts[-1] == SUMMARY_ACCESS_CODE:
        return " ".join(parts[:-1]).strip()
    return (text or "").strip()


def _my_line_identity_text(event):
    user_id = _line_user_id(event)
    if not user_id:
        return "目前無法取得你的 LINE User ID。"

    display_name = ""
    try:
        profile = line_bot_api.get_profile(user_id)
        display_name = getattr(profile, "display_name", "") or ""
    except Exception as exc:
        app.logger.info("取得 LINE Profile 失敗：%s", exc)

    source_type = _line_source_type(event)
    source_text = {
        "user": "私訊",
        "group": "群組",
        "room": "聊天室",
    }.get(source_type, source_type or "未知")

    lines = ["你的 LINE 資料："]
    if display_name:
        lines.append(f"顯示名稱：{display_name}")
    lines.append(f"User ID：{user_id}")
    lines.append(f"目前來源：{source_text}")
    lines.append("")
    if SUMMARY_ADMIN_USER_ID and user_id == SUMMARY_ADMIN_USER_ID:
        lines.append("管理員權限：✅")
    else:
        lines.append("管理員權限：❌")
    return "\n".join(lines)


def _load_interactions():
    try:
        with open(BOT_INTERACTIONS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_interactions(data):
    try:
        tmp = BOT_INTERACTIONS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, BOT_INTERACTIONS_PATH)
    except Exception as exc:
        app.logger.warning("互動紀錄儲存失敗：%s", exc)


def _display_name_for_event(event):
    user_id = _line_user_id(event)
    if not user_id:
        return ""

    source = getattr(event, "source", None)
    source_type = _line_source_type(event)

    try:
        if source_type == "group":
            group_id = getattr(source, "group_id", "") or ""
            if group_id and hasattr(line_bot_api, "get_group_member_profile"):
                profile = line_bot_api.get_group_member_profile(group_id, user_id)
                return getattr(profile, "display_name", "") or ""
        if source_type == "room":
            room_id = getattr(source, "room_id", "") or ""
            if room_id and hasattr(line_bot_api, "get_room_member_profile"):
                profile = line_bot_api.get_room_member_profile(room_id, user_id)
                return getattr(profile, "display_name", "") or ""

        profile = line_bot_api.get_profile(user_id)
        return getattr(profile, "display_name", "") or ""
    except Exception:
        return ""


def _load_usage():
    try:
        with open(BOT_USAGE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_usage(data):
    try:
        data = list(data)[-500:]
        tmp = BOT_USAGE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, BOT_USAGE_PATH)
    except Exception as exc:
        app.logger.warning("Bot 使用紀錄儲存失敗：%s", exc)


def _record_bot_usage(event, action, result="成功"):
    """只記誰、何時、在哪裡觸發了哪個 Bot 功能；不保存原始訊息內容或密碼。"""
    user_id = _line_user_id(event)
    if not user_id:
        return

    source = getattr(event, "source", None)
    source_type = _line_source_type(event)

    item = {
        "at": _taipei_now_iso(),
        "user_id": user_id,
        "display_name": _display_name_for_event(event),
        "source": source_type or "unknown",
        "action": action,
        "result": result,
    }

    if source_type == "group":
        item["source_id"] = getattr(source, "group_id", "") or ""
    elif source_type == "room":
        item["source_id"] = getattr(source, "room_id", "") or ""

    data = _load_usage()
    data.append(item)
    _save_usage(data)


def _usage_report(event):
    user_id = _line_user_id(event)

    if not SUMMARY_ADMIN_USER_ID or user_id != SUMMARY_ADMIN_USER_ID:
        return "無權限使用此功能。"

    if not _is_private_line_chat(event):
        return "使用紀錄屬機密資料，請私訊 Bot 查詢。"

    data = _load_usage()
    if not data:
        return "目前還沒有 Bot 使用紀錄。"

    lines = [f"Bot 使用紀錄（最近 {min(len(data), 50)} 筆）："]

    for item in reversed(data[-50:]):
        source = item.get("source") or "unknown"
        source_text = {
            "user": "私訊",
            "group": "群組",
            "room": "聊天室",
        }.get(source, source)

        name = item.get("display_name") or "未取得名稱"
        uid = item.get("user_id") or ""
        at = item.get("at") or ""
        action = item.get("action") or "未知功能"
        result = item.get("result") or ""

        lines.append("")
        lines.append(f"{at}｜{source_text}")
        lines.append(f"{name}｜{uid}")
        lines.append(f"觸發：{action}｜{result}")

    return "\n".join(lines)


def _record_private_interaction(event):
    """只記錄誰私訊過 Bot，不保存使用者訊息內容。"""
    if not _is_private_line_chat(event):
        return
    user_id = _line_user_id(event)
    if not user_id:
        return

    display_name = _display_name_for_event(event)

    data = _load_interactions()
    item = data.get(user_id, {})
    item["user_id"] = user_id
    if display_name:
        item["display_name"] = display_name
    item["last_seen"] = _taipei_now_iso()
    item["count"] = int(item.get("count", 0) or 0) + 1
    data[user_id] = item
    _save_interactions(data)


def _interaction_report(event):
    user_id = _line_user_id(event)
    if not SUMMARY_ADMIN_USER_ID or user_id != SUMMARY_ADMIN_USER_ID:
        return "無權限使用此功能。"

    data = _load_interactions()
    if not data:
        return "目前還沒有私訊紀錄。"

    items = sorted(
        data.values(),
        key=lambda x: x.get("last_seen", ""),
        reverse=True
    )

    lines = [f"私訊 Bot 的使用者（共 {len(items)} 人）："]
    for item in items[:50]:
        name = item.get("display_name") or "未取得名稱"
        uid = item.get("user_id") or ""
        last_seen = item.get("last_seen") or ""
        count = item.get("count", 0)
        lines.append("")
        lines.append(name)
        lines.append(f"User ID：{uid}")
        lines.append(f"最後私訊：{last_seen}")
        lines.append(f"互動次數：{count}")
    if len(items) > 50:
        lines.append("")
        lines.append(f"另有 {len(items) - 50} 人未顯示。")
    return "\n".join(lines)

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



def _google_headers():
    if not GOOGLE_CREDENTIALS.valid:
        GOOGLE_CREDENTIALS.refresh(GoogleAuthRequest())
    return {
        "Authorization": f"Bearer {GOOGLE_CREDENTIALS.token}",
        "Content-Type": "application/json; charset=utf-8",
    }


def _sheet_metadata(spreadsheet_id):
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}?fields=sheets.properties"
    response = requests.get(url, headers=_google_headers(), timeout=15)
    response.raise_for_status()
    return response.json()


def _ensure_sheet_tab(spreadsheet_id, tab_name, headers):
    """不存在就自動建立分頁；空白分頁則自動補表頭。"""
    meta = _sheet_metadata(spreadsheet_id)
    titles = {
        s.get("properties", {}).get("title", "")
        for s in meta.get("sheets", [])
    }
    if tab_name not in titles:
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}:batchUpdate"
        payload = {"requests": [{"addSheet": {"properties": {"title": tab_name}}}]}
        response = requests.post(url, headers=_google_headers(), json=payload, timeout=15)
        response.raise_for_status()

    existing = _sheet_values(spreadsheet_id, f"'{tab_name}'!A1:Z1")
    if not existing:
        encoded_range = quote(f"'{tab_name}'!A1", safe="")
        url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/"
            f"{encoded_range}?valueInputOption=RAW"
        )
        response = requests.put(
            url,
            headers=_google_headers(),
            json={"range": f"'{tab_name}'!A1", "majorDimension": "ROWS", "values": [headers]},
            timeout=15,
        )
        response.raise_for_status()


def _sheet_append(spreadsheet_id, tab_name, values):
    encoded_range = quote(f"'{tab_name}'!A:Z", safe="")
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/"
        f"{encoded_range}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS"
    )
    response = requests.post(
        url,
        headers=_google_headers(),
        json={"majorDimension": "ROWS", "values": [values]},
        timeout=15,
    )
    if response.status_code >= 300:
        app.logger.error("Bot紀錄 Google Sheets 寫入失敗 %s：%s", response.status_code, response.text)
    response.raise_for_status()


def _sheet_clear(spreadsheet_id, range_name):
    encoded_range = quote(range_name, safe="")
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/"
        f"{encoded_range}:clear"
    )
    response = requests.post(url, headers=_google_headers(), json={}, timeout=15)
    response.raise_for_status()


def _sheet_write_rows(spreadsheet_id, start_range, rows):
    if not rows:
        return
    encoded_range = quote(start_range, safe="")
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/"
        f"{encoded_range}?valueInputOption=RAW"
    )
    response = requests.put(
        url, headers=_google_headers(),
        json={"majorDimension": "ROWS", "values": rows}, timeout=15,
    )
    response.raise_for_status()


def _parse_record_time(value):
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo("Asia/Taipei"))
            return dt.astimezone(ZoneInfo("Asia/Taipei"))
        except ValueError:
            pass
    return None


class GoogleSheetEventStore:
    HEADERS = [
        "order_id", "event_type", "at", "driver_name", "plate",
        "note", "instance_key", "changes_json",
    ]

    def __init__(self, spreadsheet_id, tab_name):
        self.spreadsheet_id = spreadsheet_id
        self.tab_name = tab_name
        self.events = []
        _ensure_sheet_tab(spreadsheet_id, tab_name, self.HEADERS)
        self.load()

    def load(self):
        rows = _sheet_values(self.spreadsheet_id, f"'{self.tab_name}'!A2:H")
        events = []
        for row in rows:
            row = list(row) + [""] * (8 - len(row))
            if not str(row[0]).strip():
                continue
            try:
                changes = json.loads(row[7]) if str(row[7]).strip() else {}
                if not isinstance(changes, dict):
                    changes = {}
            except Exception:
                changes = {}
            events.append(Event(
                order_id=str(row[0]).strip(),
                event_type=str(row[1]).strip(),
                at=str(row[2]).strip(),
                driver_name=str(row[3]).strip(),
                plate=str(row[4]).strip(),
                note=str(row[5]),
                instance_key=str(row[6]).strip(),
                changes=changes,
            ))
        self.events = events

    def add(self, event):
        _sheet_append(
            self.spreadsheet_id,
            self.tab_name,
            [
                event.order_id, event.event_type, event.at, event.driver_name,
                event.plate, event.note, event.instance_key,
                json.dumps(event.changes or {}, ensure_ascii=False, separators=(",", ":")),
            ],
        )
        self.events.append(event)

    def cleanup_older_than(self, cutoff):
        kept = []
        for event in self.events:
            dt = _parse_record_time(event.at)
            # 無法辨識日期的舊資料保留，避免誤刪。
            if dt is None or dt >= cutoff:
                kept.append(event)
        if len(kept) == len(self.events):
            return 0
        rows = [[
            e.order_id, e.event_type, e.at, e.driver_name, e.plate, e.note,
            e.instance_key, json.dumps(e.changes or {}, ensure_ascii=False, separators=(",", ":")),
        ] for e in kept]
        _sheet_clear(self.spreadsheet_id, f"'{self.tab_name}'!A2:H")
        _sheet_write_rows(self.spreadsheet_id, f"'{self.tab_name}'!A2", rows)
        removed = len(self.events) - len(kept)
        self.events = kept
        return removed

    def for_order(self, order):
        matched = []
        for event in self.events:
            if event.order_id.upper() != order.order_id.upper():
                continue
            if event.instance_key and event.instance_key != order.instance_key:
                continue
            matched.append(event)
        return sorted(matched, key=lambda e: e.at)


class GoogleSheetLineOrderStore:
    HEADERS = ["order_id", "updated_at", "mode", "order_json"]

    def __init__(self, spreadsheet_id, tab_name):
        self.spreadsheet_id = spreadsheet_id
        self.tab_name = tab_name
        self.records = {}
        _ensure_sheet_tab(spreadsheet_id, tab_name, self.HEADERS)
        self.load()

    def load(self):
        rows = _sheet_values(self.spreadsheet_id, f"'{self.tab_name}'!A2:D")
        records = {}
        from order_summary_v1 import Order, LineOrderRecord
        for row in rows:
            row = list(row) + [""] * (4 - len(row))
            oid = str(row[0]).strip().upper()
            if not oid or not str(row[3]).strip():
                continue
            try:
                payload = json.loads(row[3])
                order = Order(**payload)
                # V1.6.15: repair older persisted LINE records whose pickup time was
                # left blank because the time lived in 出發日期 instead of 航班編號.
                if not str(getattr(order, "time", "") or "").strip() and getattr(order, "raw_text", ""):
                    repaired = parse_line_order_text(order.raw_text)
                    if repaired and repaired.time:
                        order.time = repaired.time
                rec = LineOrderRecord(
                    order=order,
                    updated_at=str(row[1]).strip(),
                    mode=str(row[2]).strip() or "line_added",
                )
                # append-only：同一訂單號以最後一列為最新版
                records[oid] = rec
            except Exception as exc:
                app.logger.warning("略過無法解析的 LINE訂單 永久紀錄 %s：%s", oid, exc)
        self.records = records

    def get(self, order_id):
        return self.records.get((order_id or "").upper())

    def upsert(self, order, updated_at, mode="line_added"):
        from dataclasses import asdict
        from order_summary_v1 import LineOrderRecord
        payload = json.dumps(asdict(order), ensure_ascii=False, separators=(",", ":"))
        _sheet_append(
            self.spreadsheet_id,
            self.tab_name,
            [order.order_id, updated_at, mode, payload],
        )
        self.records[order.order_id.upper()] = LineOrderRecord(
            order=order, updated_at=updated_at, mode=mode
        )

    def cleanup_older_than(self, cutoff):
        rows = _sheet_values(self.spreadsheet_id, f"'{self.tab_name}'!A2:D")
        kept_rows = []
        for row in rows:
            row = list(row) + [""] * (4 - len(row))
            dt = _parse_record_time(row[1])
            if dt is None or dt >= cutoff:
                kept_rows.append(row[:4])
        if len(kept_rows) == len(rows):
            return 0
        _sheet_clear(self.spreadsheet_id, f"'{self.tab_name}'!A2:D")
        _sheet_write_rows(self.spreadsheet_id, f"'{self.tab_name}'!A2", kept_rows)
        removed = len(rows) - len(kept_rows)
        self.load()
        return removed

    def all_orders(self):
        return [rec.order for rec in self.records.values()]


def _cleanup_google_sheet_records(force=False):
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    last = _cleanup_state.get("last_run")
    if not force and last and now - last < timedelta(hours=24):
        return
    cutoff = now - timedelta(days=max(1, BOT_RECORD_RETENTION_DAYS))
    removed_events = order_event_store.cleanup_older_than(cutoff)
    removed_line = line_order_store.cleanup_older_than(cutoff)
    _cleanup_state["last_run"] = now
    app.logger.info(
        "V1.6.10 60天清理完成：cutoff=%s events_removed=%s line_rows_removed=%s",
        cutoff.strftime("%Y-%m-%d %H:%M"), removed_events, removed_line,
    )


def _init_google_sheet_persistence():
    global order_event_store, line_order_store
    if not BOT_RECORD_SHEET_ID:
        raise RuntimeError("尚未設定 BOT_RECORD_SHEET_ID")
    order_event_store = GoogleSheetEventStore(BOT_RECORD_SHEET_ID, BOT_EVENT_TAB)
    line_order_store = GoogleSheetLineOrderStore(BOT_RECORD_SHEET_ID, BOT_LINE_ORDER_TAB)
    app.logger.info(
        "V1.6.10 Bot永久紀錄已啟用：sheet=%s event_tab=%s line_tab=%s events=%s line_orders=%s",
        BOT_RECORD_SHEET_ID, BOT_EVENT_TAB, BOT_LINE_ORDER_TAB,
        len(order_event_store.events), len(line_order_store.records),
    )
    _cleanup_google_sheet_records(force=True)


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

    V1.6.10: only the configured 包車群 / 測試群 may mutate order state.
    Returns True for standalone event messages so they do not fall through to
    unrelated blacklist matching.
    """
    if not _is_authorized_order_source(event):
        return False
    try:
        _cleanup_google_sheet_records()
    except Exception as exc:
        app.logger.warning("60天紀錄清理失敗（不影響本次訊息）：%s", exc)
    now = _taipei_now_iso()
    full_order = parse_line_order_text(text)
    oid_match = re.search(r"\b(?:\d{2}KK\d{9}|ORD\d{10}|[A-Z]{3}\d{6}|[A-Z]{2}\d{2}[A-Z]\d{4})\b", text, re.I)
    oid = full_order.order_id if full_order else (oid_match.group(0).upper() if oid_match else "")
    alias_event = detect_line_event_alias(text)
    detected_driver_name, detected_plate = extract_driver_info(text)

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

        driver_name, plate = detected_driver_name, detected_plate
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
        try:
            _record_bot_usage(
                event,
                "LINE完整訂單/派車資料" if (driver_name or plate) else "LINE完整訂單"
            )
        except Exception:
            pass
        return False

    if alias_event and oid:
        _add_event(Event(
            order_id=oid,
            event_type=alias_event,
            at=now,
            note=text[:500],
        ))

        # V1.6.6：「訂單號 + 新司機資料 + 換司機」代表換司機後已重新派出，
        # 不可以停在「改司機・未派」。
        if detected_driver_name or detected_plate:
            _add_event(Event(
                order_id=oid,
                event_type="assigned",
                at=now,
                driver_name=detected_driver_name,
                plate=detected_plate,
                note="由訂單號＋司機資料自動辨識為已派",
            ))

        try:
            alias_label = {
                "cancelled": "訂單取消",
                "recalled": "拉回/改派",
                "driver_change": "改司機",
                "edited": "訂單修正",
            }.get(alias_event, "訂單異動")
            _record_bot_usage(event, alias_label)
        except Exception:
            pass
        return True

    # V1.6.6：像「MKJ423828 + 姓名/車號」這種沒有重貼完整訂單的派車格式，
    # 只要有有效訂單號與司機資料，也直接記成已派。
    if oid and (detected_driver_name or detected_plate):
        _add_event(Event(
            order_id=oid,
            event_type="assigned",
            at=now,
            driver_name=detected_driver_name,
            plate=detected_plate,
            note="由訂單號＋司機資料自動辨識為已派",
        ))
        try:
            _record_bot_usage(event, "訂單號＋司機資料派車")
        except Exception:
            pass
        return True

    return False

def _refresh_order_state_from_sheet():
    """V1.6.11：查簡表前重新從永久 Google Sheet 載入最新狀態。

    Render/Gunicorn 可能有不同 worker，各 worker 的記憶體快取不一定同步。
    以 Google Sheet 為唯一真實來源，避免已派訂單仍被舊 worker 列成未派。
    """
    if hasattr(order_event_store, "load"):
        order_event_store.load()
    if hasattr(line_order_store, "load"):
        line_order_store.load()


def handle_order_v1_command(event, text):
    raw_text = text
    command_text = _strip_summary_password(raw_text)
    cmd = parse_line_command(command_text)
    if not cmd:
        return False

    # V1.6.3：簡表改為密碼制，且預設只能私訊 Bot 查詢。
    if cmd["command"] in {"summary", "summary_unassigned"}:
        action_name = "未派簡表查詢" if cmd["command"] == "summary_unassigned" else "完整簡表查詢"

        # V1.6.5：群組與私訊都可查簡表，只驗證密碼，不再限制必須私訊。
        if not _summary_password_ok(raw_text):
            try:
                _record_bot_usage(event, action_name, "密碼錯誤")
            except Exception:
                pass
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="簡表密碼錯誤。")
            )
            return True

    try:
        if cmd["command"] in {"summary", "summary_unassigned"}:
            # V1.6.11：每次查詢前都以永久 Google Sheet 最新資料為準，
            # 不依賴目前 Render worker 的舊記憶體快取。
            _refresh_order_state_from_sheet()
            _record_bot_usage(
                event,
                "未派簡表查詢" if cmd["command"] == "summary_unassigned" else "完整簡表查詢",
                "成功",
            )
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
            _record_bot_usage(event, "訂單歷史查詢")
            text_out = order_history_by_id(cmd["order_id"])
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=text_out[:5000]))
            return True

        if cmd["command"] == "event":
            _record_bot_usage(event, "訂單調度/異動指令")
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

    try:
        _record_bot_usage(event, f"黑名單查詢（{match_type}）")
    except Exception:
        pass

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

    try:
        _record_bot_usage(event, f"特殊註記查詢（{match_type}）")
    except Exception:
        pass

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

# V1.6.9：在開始接收 LINE webhook 前，先從永久 Google Sheet 載入歷史狀態。
_init_google_sheet_persistence()


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

    # 記錄「誰曾經私訊過 Bot」；不保存訊息內容。
    try:
        _record_private_interaction(event)
    except Exception as exc:
        app.logger.warning("互動紀錄失敗：%s", exc)

    # 查看自己的 LINE User ID / 管理員狀態。
    if text in {"我的ID", "我的id", "我的 Id", "我的權限", "權限"}:
        try:
            _record_bot_usage(event, "我的ID/權限查詢")
        except Exception:
            pass
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=_my_line_identity_text(event))
        )
        return

    # 只有管理員本人可以查看「誰私訊過 Bot」，且只在私訊中回傳。
    if text in {"誰私訊過機器人", "誰私訊過Bot", "私訊紀錄", "訊息紀錄"}:
        if not _is_private_line_chat(event):
            reply_text = (
                "私訊紀錄屬機密資料，請私訊 Bot 查詢。"
                if _line_user_id(event) == SUMMARY_ADMIN_USER_ID
                else "無權限使用此功能。"
            )
        else:
            reply_text = _interaction_report(event)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return

    # V1.6.4：私訊＋群組，只記錄真正觸發 Bot 功能的人。
    # 只有管理員本人，而且必須私訊 Bot 才能查看。
    if text in {"使用紀錄", "觸發紀錄", "誰使用過機器人", "誰觸發過機器人", "Bot使用紀錄"}:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=_usage_report(event))
        )
        return

    try:
        app.logger.info(
            "LINE互動 user_id=%s source=%s",
            _line_user_id(event) or "(unknown)",
            _line_source_type(event) or "(unknown)",
        )
    except Exception:
        pass

    # V1.6.10：取得目前群組 Group ID。這個指令在尚未設定白名單前也能使用。
    if text in {"群組ID", "群組id", "Group ID", "group id"}:
        gid = _line_group_id(event)
        if gid:
            reply_text = f"目前群組 Group ID：\n{gid}"
        else:
            reply_text = "這裡不是 LINE 群組，請到『包車群』或『測試群』輸入：群組ID"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return

    # 訂單解析器版本檢查：可在 LINE 輸入「版本」確認 Render 目前實際載入哪一版。
    if text in {"版本", "訂單版本", "parser version"}:
        try:
            _record_bot_usage(event, "版本查詢")
        except Exception:
            pass
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
