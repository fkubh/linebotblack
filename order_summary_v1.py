"""Airport transfer order summary V1.

Features
- Parse monthly Google Sheet rows or a local .xlsx export.
- Build daily summaries grouped into 送機 / 接機 and sorted by time.
- Keep duplicate order numbers as separate route instances (e.g. A車/B車/C車).
- Overlay LINE-side dispatch events: assigned / recalled / edited / cancelled / restored.
- Preserve full event history while showing only the latest compact status in the daily summary.

This module intentionally keeps AI out of deterministic fields such as order number,
time, passenger count and current dispatch state.
"""

from __future__ import annotations

PARSER_VERSION = "V1.6.15-20260902-time-priority"

import argparse
import html
import json
import re
import zipfile
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional
import xml.etree.ElementTree as ET


FIELD_LABELS = [
    "出發日期", "乘車人數", "行李數量", "航班編號", "上車地點", "下車地點",
    "其他備註", "聯絡人", "電話", "結算價", "客收", "費用",
]
FIELD_RE = re.compile(r"^\s*(%s)\s*[：:]\s*(.*)$" % "|".join(map(re.escape, FIELD_LABELS)))
ORDER_ID_RE = re.compile(r"\b(?:\d{2}KK\d{9}|ORD\d{10}|[A-Z]{3}\d{6}|[A-Z]{2}\d{2}[A-Z]\d{4})\b", re.I)
DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\s*/\s*(\d{1,2})(?!\d)")
TIME_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3])\s*[:：]\s*([0-5]\d)(?!\d)")

# Normal districts. For ambiguous compass districts we keep the city prefix when available.
CITY_PREFIX_RE = re.compile(r"(台北市|臺北市|新北市|桃園市|台中市|臺中市|台南市|臺南市|高雄市|基隆市|新竹市|嘉義市|新竹縣|苗栗縣|彰化縣|南投縣|雲林縣|嘉義縣|屏東縣|宜蘭縣|花蓮縣|台東縣|臺東縣|澎湖縣)([\u4e00-\u9fff]{1,5}?(?:區|鎮|鄉|市))")
LOCAL_DISTRICT_RE = re.compile(r"([\u4e00-\u9fff]{1,4}(?:區|鎮|鄉))")
COUNTY_CITY_NAMES = {"竹北市", "苗栗市", "彰化市", "南投市", "斗六市", "太保市", "朴子市", "屏東市", "宜蘭市", "花蓮市", "台東市", "馬公市"}
AMBIGUOUS_DISTRICTS = {"東區", "西區", "南區", "北區", "中區"}
FALSE_DISTRICT_SUFFIXES = {"社區", "園區", "校區"}
INFORMAL_AREA_ALIASES = {
    "新北市汐止": "汐止區", "新北市八里": "八里區", "新北市板橋": "板橋區",
    "新北市三重": "三重區", "新北市中和": "中和區", "新北市永和": "永和區",
    "新北市新莊": "新莊區", "新北市新店": "新店區", "新北市樹林": "樹林區",
    "新北市淡水": "淡水區", "新北市土城": "土城區", "新北市蘆洲": "蘆洲區",
    "新北市五股": "五股區", "新北市泰山": "泰山區", "新北市林口": "林口區",
    "新北市瑞芳": "瑞芳區", "新北市三峽": "三峽區", "新北市鶯歌": "鶯歌區",
}



@dataclass
class Order:
    order_id: str
    instance_key: str
    source_row: int
    source_tag: str
    raw_text: str
    date: str
    trip_type: str
    time: str
    pax: int
    pickup: str
    dropoff: str
    district_text: str
    vehicle_tag: str = ""
    extra_tags: list[str] = field(default_factory=list)
    flight: str = ""
    notes: str = ""


@dataclass
class Event:
    order_id: str
    event_type: str
    at: str
    driver_name: str = ""
    plate: str = ""
    note: str = ""
    instance_key: str = ""
    changes: dict[str, str] = field(default_factory=dict)


class EventStore:
    """Simple JSON persistence for V1/local testing.

    On Render, use a persistent disk or later replace this with a Sheet/DB adapter.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.events: list[Event] = []
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.events = []
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.events = [Event(**item) for item in data]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps([asdict(e) for e in self.events], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def add(self, event: Event) -> None:
        self.events.append(event)
        self.save()

    def for_order(self, order: Order) -> list[Event]:
        matched = []
        for event in self.events:
            if event.order_id.upper() != order.order_id.upper():
                continue
            if event.instance_key and event.instance_key != order.instance_key:
                continue
            matched.append(event)
        return sorted(matched, key=lambda e: e.at)



@dataclass
class LineOrderRecord:
    order: Order
    updated_at: str
    mode: str = "line_added"


class LineOrderStore:
    """Persist LINE-only or LINE-corrected full orders as JSON."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.records: dict[str, LineOrderRecord] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.records = {}
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self.records = {}
            return
        records: dict[str, LineOrderRecord] = {}
        for key, item in (data or {}).items():
            try:
                order = Order(**item["order"])
                records[key.upper()] = LineOrderRecord(
                    order=order,
                    updated_at=item.get("updated_at", ""),
                    mode=item.get("mode", "line_added"),
                )
            except Exception:
                continue
        self.records = records

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            key: {
                "order": asdict(rec.order),
                "updated_at": rec.updated_at,
                "mode": rec.mode,
            }
            for key, rec in self.records.items()
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def get(self, order_id: str) -> Optional[LineOrderRecord]:
        return self.records.get((order_id or "").upper())

    def upsert(self, order: Order, updated_at: str, mode: str = "line_added") -> None:
        self.records[order.order_id.upper()] = LineOrderRecord(order=order, updated_at=updated_at, mode=mode)
        self.save()

    def all_orders(self) -> list[Order]:
        return [rec.order for rec in self.records.values()]


def parse_line_order_text(text: str) -> Optional[Order]:
    raw = html.unescape(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if "出發日期" not in raw or "乘車人數" not in raw:
        return None
    oid = extract_order_id(raw)
    if not oid:
        return None
    order = parse_order_row([raw, oid, "", "LINE"], 0)
    if not order or not order.date or not order.trip_type:
        return None
    order.source_tag = ""
    order.instance_key = f"{order.order_id}#LINE"
    order.source_row = 0
    return order


def _norm_compare(text: str) -> str:
    return re.sub(r"\s+", "", html.unescape(text or "")).replace("臺", "台").lower()


def order_core_signature(order: Order) -> tuple:
    return (
        order.date,
        order.trip_type,
        order.time,
        int(order.pax or 0),
        _norm_compare(order.pickup),
        _norm_compare(order.dropoff),
        order.vehicle_tag,
        tuple(order.extra_tags),
        _norm_compare(order.flight),
    )


def orders_core_equal(a: Order, b: Order) -> bool:
    return order_core_signature(a) == order_core_signature(b)


def merge_sheet_and_line_orders(sheet_orders: list[Order], line_store: Optional[LineOrderStore]) -> list[Order]:
    if line_store is None:
        return list(sheet_orders)

    result = list(sheet_orders)
    by_id: dict[str, list[int]] = {}
    for i, order in enumerate(result):
        by_id.setdefault(order.order_id.upper(), []).append(i)

    for rec in line_store.records.values():
        lo = rec.order
        idxs = by_id.get(lo.order_id.upper(), [])
        if not idxs:
            by_id.setdefault(lo.order_id.upper(), []).append(len(result))
            result.append(lo)
            continue
        if len(idxs) == 1:
            idx = idxs[0]
            original = result[idx]
            lo.instance_key = original.instance_key
            lo.source_tag = original.source_tag
            result[idx] = lo
    return result


DRIVER_NAME_PATTERNS = (
    # 實際群組常見格式：司機 王錫云 / 司機名字：蔡秉夆 / 駕駛：XXX。
    # V1.6.13：也接受中文字中間被人工插入空白，例如「駕  駛：何穎鈞」。
    r"(?:服\s*務\s*駕\s*駛|司\s*機\s*姓\s*名|司\s*機\s*名\s*字|駕\s*駛\s*姓\s*名|司\s*機(?!\s*資\s*料)|駕\s*駛(?!\s*資\s*料))\s*[：:；;]?\s*([^\n\r，,（(]{2,20})",
)
PLATE_PATTERNS = (
    # 車號／車牌／牌照／車輛號碼皆視為車牌欄位；允許 RF R-7330 及「車  號」等格式。
    r"(?:車\s*輛\s*號\s*碼|車\s*牌\s*號\s*碼|車\s*號|車\s*牌|牌\s*照)\s*[：:；;]?\s*([A-Z0-9][A-Z0-9 \-]{3,14})",
)


def _normalize_plate(value: str) -> str:
    compact = re.sub(r"\s+", "", (value or "").upper())
    compact = re.sub(r"[^A-Z0-9-]", "", compact)
    # 防止正則把下一個欄位前的殘字吃進來；台灣營業車常見 ABC-1234。
    m = re.search(r"[A-Z0-9]{2,4}-[A-Z0-9]{3,4}", compact)
    return m.group(0) if m else compact


def extract_driver_info(text: str) -> tuple[str, str]:
    """Extract driver name/plate from real dispatch messages.

    Accepts common human-entered variants, with or without colon, including:
      司機 蔡東雄 / 車號 REB-1662
      姓名：戴恩慈 / 車牌：RAM-5871
      司機名字：蔡秉夆 / 車輛號碼：RFR-7785
      姓名：張奇 / 車號：RF R-7330
      駕駛資料：姓名：凱中 / 牌照：RFT-1993
    Generic 「姓名」 is accepted only when a plate field exists nearby, so the
    passenger/contact name earlier in the order is not treated as the driver.
    """
    raw = html.unescape(text or "").replace("\r\n", "\n").replace("\r", "\n")
    name = ""
    plate = ""

    # Strong driver-name labels first.
    for p in DRIVER_NAME_PATTERNS:
        m = re.search(p, raw, re.I)
        if m:
            name = normalize_spaces(m.group(1)).strip(" ：:；;")
            break

    # Plate/vehicle number is a strong signal that a driver block exists.
    plate_match = None
    for p in PLATE_PATTERNS:
        m = re.search(p, raw, re.I)
        if m:
            plate_match = m
            plate = _normalize_plate(m.group(1))
            break

    # Some dispatches use only 「姓名」. Trust it only close to the plate field.
    if not name and plate_match:
        before = raw[:plate_match.start()]
        lines = [line.strip() for line in before.split("\n") if line.strip()]
        nearby = lines[-6:]
        for line in reversed(nearby):
            m = re.match(r"^姓名\s*[：:；;]?\s*(.+?)\s*$", line)
            if m:
                candidate = normalize_spaces(m.group(1)).strip(" ：:；;")
                if 1 < len(candidate) <= 20:
                    name = candidate
                break

    return name, plate


LINE_EVENT_ALIASES = {
    "recalled": ("收回改派", "拉回改派", "司機取消", "拉回", "收回", "改派"),
    "cancelled": ("訂單取消", "取消"),
    "driver_change": ("換司機", "改司機"),
    "edited": ("延期", "改時間", "改地址", "更換車款", "改航班", "訂單修正"),
}


def detect_line_event_alias(text: str) -> Optional[str]:
    compact = normalize_spaces(text)
    for event_type in ("recalled", "cancelled", "driver_change", "edited"):
        if any(alias in compact for alias in LINE_EVENT_ALIASES[event_type]):
            return event_type
    return None


def generate_unassigned_summary(
    orders: list[Order],
    target_date: str,
    event_store: Optional[EventStore] = None,
) -> str:
    target = extract_date(target_date) or target_date.strip()
    selected: list[tuple[Order, list[Event], str, dict[str, Any]]] = []

    for base_order in orders:
        events = event_store.for_order(base_order) if event_store else []
        order = effective_order(base_order, events)
        if order.date != target:
            continue
        status, state = status_for(order, events)
        if state.get("cancelled") or state.get("assigned"):
            continue
        selected.append((order, events, status, state))

    selected.sort(key=lambda pair: (
        pair[0].trip_type != "送機",
        pair[0].time or "9999",
        pair[0].order_id,
        pair[0].instance_key,
    ))

    groups = {"送機": [], "接機": []}
    for order, events, status, state in selected:
        groups.setdefault(order.trip_type or "其他", []).append(
            summary_line(order, status, show_unassigned=True)
        )

    out = [f"{target} 未派簡表", ""]
    count = 0
    for group_name in ["送機", "接機", "其他"]:
        lines = groups.get(group_name, [])
        if not lines:
            continue
        count += len(lines)
        out.append(group_name)
        out.extend(lines)
        out.append("")
    if count == 0:
        out.append("目前沒有未派訂單")
    return "\n".join(out).rstrip()

def normalize_spaces(text: str) -> str:
    return re.sub(r"[ \t\u3000]+", " ", (text or "").strip())


def parse_fields(raw: str) -> tuple[list[str], dict[str, str]]:
    """Split header lines and labelled multiline fields."""
    headers: list[str] = []
    fields: dict[str, str] = {}
    current: Optional[str] = None

    cleaned_raw = html.unescape(raw or "").replace("\r\n", "\n").replace("\r", "\n")
    for raw_line in cleaned_raw.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        m = FIELD_RE.match(line)
        if m:
            current = m.group(1)
            fields[current] = m.group(2).strip()
            continue
        if current:
            # Stop absorbing data once a common unlabelled payment marker appears.
            if re.match(r"^(?:★|\*|客收|結算|費用)", line):
                current = None
                continue
            # Numbered pickup/dropoff lines belong to the same field.
            fields[current] = (fields[current] + "\n" + line).strip()
        else:
            headers.append(line)
    return headers, fields


def extract_order_id(raw: str, fallback: str = "") -> str:
    for source in (raw, fallback):
        m = ORDER_ID_RE.search(source or "")
        if m:
            return m.group(0).upper()
    # LINE/manual orders may use text as the second column; keep a conservative fallback.
    fb = normalize_spaces(fallback)
    return fb if fb and len(fb) <= 40 else ""


def extract_date(text: str) -> str:
    m = DATE_RE.search(text or "")
    if not m:
        return ""
    return f"{int(m.group(1))}/{int(m.group(2))}"


def time_hhmm(text: str) -> str:
    m = TIME_RE.search(text or "")
    if not m:
        return ""
    return f"{int(m.group(1)):02d}{int(m.group(2)):02d}"


def detect_trip_type(raw: str, fields: dict[str, str]) -> str:
    head = raw.splitlines()[0] if raw else ""
    if "接機" in head or "接機" in raw[:80]:
        return "接機"
    if "送機" in head or "送機" in raw[:80]:
        return "送機"
    pickup = fields.get("上車地點", "")
    dropoff = fields.get("下車地點", "")
    airport_words = r"桃園機場|桃機|TPE|松山機場|機場"
    if re.search(airport_words, pickup, re.I):
        return "接機"
    if re.search(airport_words, dropoff, re.I):
        return "送機"
    return ""


def detect_time(trip_type: str, fields: dict[str, str]) -> str:
    """Choose the operational time using dispatch priority.

    送機：出發日期旁的時間優先。
    接機：指定時間 > 航班時間 > 出發日期旁的時間。

    「指定時間」可能寫成「指定時間 00:10」或「9/3 00:10 指定時間」，
    所以只要同一欄位含「指定時間」，就取該欄位中的時間。
    """
    dep = fields.get("出發日期", "")
    flight = fields.get("航班編號", "")

    # 送機：日期旁時間就是派車/出發時間，永遠優先。
    if trip_type == "送機":
        return time_hhmm(dep)

    if trip_type == "接機":
        # 1) 指定時間優先。通常會寫在出發日期旁，也兼容備註等其他欄位。
        #    支援「指定時間 00:10」與「9/3 00:10 指定時間」兩種順序。
        for value in fields.values():
            text = value or ""
            if "指定時間" in text:
                t = time_hhmm(text)
                if t:
                    return t

        # 2) 再抓航班時間：優先括號內，再抓航班欄位內任何 HH:MM。
        for pattern in [r"【\s*([^】]+)\s*】", r"[（(]\s*([^）)]+)\s*[）)]"]:
            m = re.search(pattern, flight)
            if m:
                t = time_hhmm(m.group(1))
                if t:
                    return t
        flight_time = time_hhmm(flight)
        if flight_time:
            return flight_time

        # 3) 航班欄沒有時間時，最後才使用出發日期旁的時間。
        return time_hhmm(dep)

    return time_hhmm(dep)


def extract_pax(text: str) -> int:
    m = re.search(r"(\d+)", text or "")
    return int(m.group(1)) if m else 0


def extract_districts(address: str) -> list[str]:
    if not address:
        return []
    text = address.replace("臺", "台")
    found: list[str] = []
    occupied: list[tuple[int, int]] = []

    for phrase, label in INFORMAL_AREA_ALIASES.items():
        if phrase in text and label not in found:
            found.append(label)

    # Prefer explicit city/county + district matches first.
    for m in CITY_PREFIX_RE.finditer(text):
        city = m.group(1).replace("臺", "台")
        district = m.group(2)
        occupied.append(m.span())
        if district in FALSE_DISTRICT_SUFFIXES:
            continue
        if district.endswith("市") and district not in COUNTY_CITY_NAMES:
            continue
        label = district
        # Preserve city for compass districts and 桃園市, matching current dispatch habits.
        if district in AMBIGUOUS_DISTRICTS or city == "桃園市":
            label = f"{city}{district}"
        if label not in found:
            found.append(label)

    # Then accept standalone district/town/township names, excluding obvious place-name false positives.
    def overlaps(span: tuple[int, int]) -> bool:
        return any(not (span[1] <= a or span[0] >= b) for a, b in occupied)

    for m in LOCAL_DISTRICT_RE.finditer(text):
        if overlaps(m.span()):
            continue
        district = m.group(1)
        if district in FALSE_DISTRICT_SUFFIXES or district.endswith("社區") or district.endswith("園區"):
            continue
        # Ignore strings directly ending with 門市/商圈 descriptors before the suffix.
        prefix = text[max(0, m.start()-4):m.end()]
        if "門市" in prefix:
            continue
        if district not in found:
            found.append(district)

    # County-administered cities may appear without a county prefix.
    for name in COUNTY_CITY_NAMES:
        if name in text and name not in found:
            found.append(name)
    return found


def district_for_order(trip_type: str, fields: dict[str, str]) -> str:
    address = fields.get("下車地點", "") if trip_type == "接機" else fields.get("上車地點", "")
    districts = extract_districts(address)
    if not districts:
        return "地區待確認"
    return "/".join(districts)


def vehicle_tag(raw: str) -> str:
    """Map vehicle wording using strict priority.

    Only the order header area is inspected. More specific classes are always checked
    before generic classes so 高九 can never fall through to 九座, and 假七/七座/經七
    always become 經七.
    """
    text = html.unescape(raw or "").replace("\r", "\n")
    # Vehicle wording is expected before the labelled fields. Keep enough text to survive
    # odd blank lines/BOMs while avoiding notes that may mention another vehicle class.
    header_area = text.split("出發日期", 1)[0]
    header_area = re.sub(r"[\s\u3000]+", "", header_area)
    lower = header_area.lower()

    # Strict precedence: specific names first.
    if "alphard" in lower or "阿法" in header_area:
        return "Alphard"
    if "保母車" in header_area:
        return "保母車"
    if "高九" in header_area:
        return "高九"
    if "九座" in header_area:
        return "九座"
    if "高七" in header_area:
        return "高七"
    if any(x in header_area for x in ("經七", "七座", "假七")):
        return "經七"
    if "高五" in header_area:
        return "高五"
    if any(x in header_area for x in ("休旅", "休五")):
        return "休旅"
    # 四座 / 經五 / 一般五座 intentionally hidden.
    return ""


def extra_tags(fields: dict[str, str]) -> list[str]:
    """Return only seat/accessory tags that belong inside 【】.

    Priority is exclusive: booster wording wins over generic safety-seat wording.
    This prevents 前向式安全座椅（增高） from becoming 安椅.
    """
    notes = html.unescape(fields.get("其他備註", "") or "")
    compact = re.sub(r"[\s\u3000]+", "", notes)

    booster_patterns = (
        r"前向式安全座椅[（(]?(?:增高|增高型)[）)]?",
        r"兒童增高墊",
        r"增高墊",
    )
    if any(re.search(p, compact, re.I) for p in booster_patterns):
        return ["增高墊"]

    seat_pattern = (
        r"嬰兒座椅|兒童座椅|兒童安全座椅|向後式嬰兒安全座椅|向後式座椅|"
        r"前向式安全座椅|安全座椅|汽座"
    )
    if re.search(seat_pattern, compact, re.I):
        return ["安椅"]
    return []


def external_markers(order: Order) -> str:
    """Markers shown outside 【】 according to dispatch rules.

    - 舉牌 -> *舉牌*
    - 松山機場 -> *(松機)
    - 指定時間 -> *指定時間*
    """
    markers: list[str] = []
    full_text = "\n".join([order.raw_text, order.pickup, order.dropoff, order.notes])
    if "舉牌" in order.notes:
        markers.append("*舉牌*")
    if re.search(r"松山機場|台北松山機場|臺北松山機場", full_text):
        markers.append("*(松機)")
    if "指定時間" in full_text:
        markers.append("*指定時間*")
    return "".join(markers)


def parse_order_row(row: list[str], row_number: int) -> Optional[Order]:
    raw = next((str(v) for v in row if "出發日期" in str(v) and "乘車人數" in str(v)), "")
    if not raw:
        return None
    headers, fields = parse_fields(raw)
    fallback_id = str(row[1]).strip() if len(row) > 1 else ""
    oid = extract_order_id(raw, fallback_id)
    if not oid:
        # Keep LINE/manual rows addressable using their first non-field header.
        oid = fallback_id or next((h for h in headers if h), f"ROW{row_number}")
    trip = detect_trip_type(raw, fields)
    date = extract_date(fields.get("出發日期", ""))
    source_tag = str(row[3]).strip() if len(row) > 3 else ""
    # V1.6.7：像「更新----／更新」是工作表人工註記，不是重複訂單來源標籤。
    # 避免簡表出現 CR01M3898(更新----)。
    if re.match(r"^更新(?:[-—–_\s]*)$", source_tag):
        source_tag = ""
    instance_key = f"{oid}#{source_tag}" if source_tag else f"{oid}#R{row_number}"
    return Order(
        order_id=oid,
        instance_key=instance_key,
        source_row=row_number,
        source_tag=source_tag,
        raw_text=raw,
        date=date,
        trip_type=trip,
        time=detect_time(trip, fields),
        pax=extract_pax(fields.get("乘車人數", "")),
        pickup=fields.get("上車地點", ""),
        dropoff=fields.get("下車地點", ""),
        district_text=district_for_order(trip, fields),
        vehicle_tag=vehicle_tag(raw),
        extra_tags=extra_tags(fields),
        flight=fields.get("航班編號", ""),
        notes=fields.get("其他備註", ""),
    )


def parse_rows(rows: Iterable[list[str]]) -> list[Order]:
    result: list[Order] = []
    for idx, row in enumerate(rows, start=1):
        order = parse_order_row(row, idx)
        if order:
            result.append(order)
    return result


def parse_change_note(note: str) -> dict[str, str]:
    """Parse deterministic correction fields from a LINE edit note.

    Examples:
      時間=11:40 人數=3
      航班延誤 10:25→11:40
      地區=大安區 車型=休旅
      日期=8/29
    """
    text = normalize_spaces(note)
    changes: dict[str, str] = {}

    # Explicit key=value wins.
    patterns = {
        "time": r"(?:時間|接機時間|送機時間)\s*[=＝:]\s*([0-2]?\d(?::?[0-5]\d)?)",
        "pax": r"(?:人數|乘車人數)\s*[=＝:]\s*(\d+)",
        "district": r"(?:地區|區域)\s*[=＝:]\s*([^ ]+)",
        "vehicle_tag": r"(?:車型|車種)\s*[=＝:]\s*([^ ]+)",
        "date": r"(?:日期|出發日期)\s*[=＝:]\s*(\d{1,2}/\d{1,2})",
        "trip_type": r"(?:類型|接送)\s*[=＝:]\s*(接機|送機)",
    }
    for key, pattern in patterns.items():
        m = re.search(pattern, text)
        if m:
            changes[key] = m.group(1)

    # Common human shorthand: 10:25→11:40 / 1025->1140.
    arrows = re.findall(r"([0-2]?\d(?::?[0-5]\d)?)\s*(?:→|->|＞|>)\s*([0-2]?\d(?::?[0-5]\d)?)", text)
    if arrows and "time" not in changes:
        changes["time"] = arrows[-1][1]

    if "time" in changes:
        raw = changes["time"].replace(":", "")
        if raw.isdigit():
            changes["time"] = raw.zfill(4)
    if "date" in changes:
        changes["date"] = extract_date(changes["date"])
    if "vehicle_tag" in changes:
        v = changes["vehicle_tag"]
        vl = v.lower()
        if "alphard" in vl or "阿法" in v:
            changes["vehicle_tag"] = "Alphard"
        elif "保母車" in v:
            changes["vehicle_tag"] = "保母車"
        elif "高九" in v:
            changes["vehicle_tag"] = "高九"
        elif "九座" in v:
            changes["vehicle_tag"] = "九座"
        elif "高七" in v:
            changes["vehicle_tag"] = "高七"
        elif any(x in v for x in ["經七", "七座", "假七"]):
            changes["vehicle_tag"] = "經七"
        elif "高五" in v:
            changes["vehicle_tag"] = "高五"
        elif "休旅" in v or "休五" in v:
            changes["vehicle_tag"] = "休旅"
        elif v in {"無", "一般", "經五", "五座", "四座"}:
            changes["vehicle_tag"] = ""
    return changes


def effective_order(order: Order, events: list[Event]) -> Order:
    """Overlay all LINE correction events without mutating the Google Sheet source order."""
    patch: dict[str, Any] = {}
    for e in events:
        if e.event_type.lower() != "edited":
            continue
        changes = dict(e.changes or {})
        if not changes and e.note:
            changes = parse_change_note(e.note)
        if "time" in changes:
            patch["time"] = changes["time"]
        if "pax" in changes:
            try:
                patch["pax"] = int(changes["pax"])
            except (TypeError, ValueError):
                pass
        if "district" in changes:
            patch["district_text"] = changes["district"]
        if "vehicle_tag" in changes:
            patch["vehicle_tag"] = changes["vehicle_tag"]
        if "date" in changes and changes["date"]:
            patch["date"] = changes["date"]
        if "trip_type" in changes:
            patch["trip_type"] = changes["trip_type"]
    return replace(order, **patch) if patch else order


def status_for(order: Order, events: list[Event]) -> tuple[str, dict[str, Any]]:
    state: dict[str, Any] = {
        "cancelled": False,
        "assigned": False,
        "driver_name": "",
        "plate": "",
        "assigned_count": 0,
        "recall_count": 0,
        "edit_count": 0,
        "driver_change_count": 0,
        "conflict": False,
    }

    for e in events:
        et = e.event_type.lower()
        if et == "assigned":
            if state["assigned"] and (
                (e.driver_name and state["driver_name"] and e.driver_name != state["driver_name"])
                or (e.plate and state["plate"] and e.plate != state["plate"])
            ):
                state["driver_change_count"] += 1
            state["assigned"] = True
            state["driver_name"] = e.driver_name
            state["plate"] = e.plate
            state["assigned_count"] += 1
            state["cancelled"] = False
        elif et in {"recalled", "unassigned"}:
            state["assigned"] = False
            state["driver_name"] = ""
            state["plate"] = ""
            state["recall_count"] += 1
        elif et == "driver_change":
            state["assigned"] = False
            state["driver_name"] = ""
            state["plate"] = ""
            state["driver_change_count"] += 1

            # V1.6.6：舊版若收到「換司機改帳號」＋完整司機資料，
            # 事件雖被記成 driver_change，但其實新司機已經派出。
            # 從既有事件 note 補抓司機，讓舊紀錄不用重貼也能恢復成已派。
            note_driver_name, note_plate = extract_driver_info(e.note or "")
            if note_driver_name or note_plate:
                state["assigned"] = True
                state["driver_name"] = note_driver_name
                state["plate"] = note_plate
                state["assigned_count"] += 1
        elif et == "edited":
            state["edit_count"] += 1
        elif et == "cancelled":
            state["cancelled"] = True
            state["assigned"] = False
            state["driver_name"] = ""
            state["plate"] = ""
        elif et == "restored":
            state["cancelled"] = False
        elif et == "conflict":
            state["conflict"] = True
        elif et == "conflict_resolved":
            state["conflict"] = False

    # V1.6.6：有些已派訊息是「完整訂單 + 姓名/電話/車號/車型」，
    # 舊版若車號用了「；」會漏掉 assigned event。直接從 LINE 訂單原文補判斷。
    if not state["cancelled"] and not state["assigned"]:
        raw_driver_name, raw_plate = extract_driver_info(order.raw_text or "")
        if raw_driver_name or raw_plate:
            state["assigned"] = True
            state["driver_name"] = raw_driver_name
            state["plate"] = raw_plate
            state["assigned_count"] += 1

    if state["cancelled"]:
        return "取消", state
    if state["conflict"]:
        return "資料衝突", state

    edited = state["edit_count"] > 0
    changed = state["driver_change_count"] > 0

    if changed and state["assigned"]:
        return ("異動・改司機・已派" if edited else "改司機・已派"), state
    if changed and not state["assigned"]:
        return ("異動・改司機・未派" if edited else "改司機・未派"), state
    if edited and state["assigned"]:
        return "異動・已派", state
    if edited:
        return "異動・未派", state
    if state["assigned"]:
        if state["assigned_count"] >= 2 and state["recall_count"] >= 1:
            return "重派・已派", state
        return "已派", state
    return "未派", state


def compact_tags(order: Order) -> str:
    tags = []
    if order.vehicle_tag:
        tags.append(order.vehicle_tag)

    # V1.6.8：重新依原始備註補算座椅標籤。
    # 這樣舊版已存進 line_orders_v1.json 的訂單，也不用重新貼單就能補出「安椅」。
    live_extra_tags = extra_tags({"其他備註": order.notes or ""})
    merged_extra_tags = list(order.extra_tags or [])
    for tag in live_extra_tags:
        if tag not in merged_extra_tags:
            merged_extra_tags.append(tag)

    tags.extend(merged_extra_tags)
    return f"【{'+'.join(tags)}】" if tags else ""


def summary_line(order: Order, status: str, show_unassigned: bool = False) -> str:
    time_text = order.time or "????"
    pax = f"{order.pax}人" if order.pax else "?人"
    route = f"接{order.district_text}" if order.trip_type == "接機" else f"{order.district_text}送"
    tags = compact_tags(order)
    markers = external_markers(order)
    status_text = ""
    if status != "未派" or show_unassigned:
        status_text = f"【{status}】"
    source_tag = f"({order.source_tag})" if order.source_tag else ""
    return f"{time_text} {pax} {route}{tags}-{order.order_id}{source_tag}{markers}{status_text}"


def generate_daily_summary(
    orders: list[Order],
    target_date: str,
    event_store: Optional[EventStore] = None,
    include_cancelled: bool = True,
    show_unassigned: bool = False,
) -> str:
    target = extract_date(target_date) or target_date.strip()
    selected: list[tuple[Order, list[Event]]] = []
    for base_order in orders:
        events = event_store.for_order(base_order) if event_store else []
        order = effective_order(base_order, events)
        if order.date == target:
            selected.append((order, events))
    selected.sort(key=lambda pair: (pair[0].trip_type != "送機", pair[0].time or "9999", pair[0].order_id, pair[0].instance_key))

    groups = {"送機": [], "接機": []}
    for order, events in selected:
        status, _ = status_for(order, events)
        if status == "取消" and not include_cancelled:
            continue
        groups.setdefault(order.trip_type or "其他", []).append(summary_line(order, status, show_unassigned))

    out = [target, ""]
    for group_name in ["送機", "接機", "其他"]:
        lines = groups.get(group_name, [])
        if not lines:
            continue
        out.append(group_name)
        out.extend(lines)
        out.append("")
    return "\n".join(out).rstrip()


def event_history_text(order: Order, store: EventStore) -> str:
    events = store.for_order(order)
    status, state = status_for(order, events)
    lines = [f"{order.order_id}｜目前：{status}"]
    if state.get("driver_name"):
        lines.append(f"司機：{state['driver_name']} {state.get('plate','')}".rstrip())
    lines.append(f"異動 {state['edit_count']} 次／指派 {state['assigned_count']} 次／拉回 {state['recall_count']} 次")
    if not events:
        lines.append("尚無 LINE 調度事件")
        return "\n".join(lines)
    lines.append("")
    for e in events:
        detail = " ".join(x for x in [e.driver_name, e.plate, e.note] if x)
        lines.append(f"{e.at}｜{e.event_type}{('｜' + detail) if detail else ''}")
    return "\n".join(lines)


# -----------------------------
# Minimal XLSX reader (stdlib only)
# -----------------------------
XLSX_NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _xlsx_colnum(ref: str) -> int:
    m = re.match(r"([A-Z]+)", ref)
    n = 0
    for ch in m.group(1):
        n = n * 26 + ord(ch) - 64
    return n


def load_rows_from_xlsx(path: str | Path, sheet_name: str) -> list[list[str]]:
    with zipfile.ZipFile(path) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall("a:si", XLSX_NS):
                shared.append("".join((t.text or "") for t in si.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")))

        workbook = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        relmap = {x.attrib["Id"]: x.attrib["Target"] for x in rels}
        target = None
        for s in workbook.find("a:sheets", XLSX_NS):
            if s.attrib["name"] == sheet_name:
                rid = s.attrib[f"{{{REL_NS}}}id"]
                target = "xl/" + relmap[rid]
                break
        if not target:
            raise ValueError(f"找不到工作表：{sheet_name}")

        sheet = ET.fromstring(z.read(target))
        rows: list[list[str]] = []
        for row in sheet.findall(".//a:sheetData/a:row", XLSX_NS):
            vals: dict[int, str] = {}
            max_col = 0
            for cell in row.findall("a:c", XLSX_NS):
                col = _xlsx_colnum(cell.attrib["r"])
                max_col = max(max_col, col)
                typ = cell.attrib.get("t")
                v = cell.find("a:v", XLSX_NS)
                if v is None:
                    inline = cell.find("a:is", XLSX_NS)
                    value = "" if inline is None else "".join((t.text or "") for t in inline.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t"))
                else:
                    raw = v.text or ""
                    value = shared[int(raw)] if typ == "s" and raw else raw
                vals[col] = value
            rows.append([vals.get(i, "") for i in range(1, max(max_col, 4) + 1)])
        return rows


def parse_line_command(text: str) -> Optional[dict[str, str]]:
    """Deterministic LINE commands for V1.6."""
    text = normalize_spaces(text)

    m = re.match(r"^(簡表|未派)\s+(.+)$", text)
    if m:
        return {
            "command": "summary_unassigned" if m.group(1) == "未派" else "summary",
            "date": m.group(2).strip(),
        }

    m = re.match(
        r"^(派車|拉回改派|拉回|改派|取消|訂單取消|恢復|異動|延期|改時間|改地址|更換車款|改航班|訂單修正|換司機|改司機|歷程)\s+(.+)$",
        text,
    )
    if not m:
        return None

    command, rest = m.group(1), m.group(2).strip()
    oid_m = ORDER_ID_RE.search(rest)
    if not oid_m:
        return None
    oid = oid_m.group(0).upper()
    tail = normalize_spaces(rest[oid_m.end():])

    if command == "歷程":
        return {"command": "history", "order_id": oid}

    if command == "派車":
        parts = tail.split(" ") if tail else []
        return {
            "command": "event",
            "event_type": "assigned",
            "order_id": oid,
            "driver_name": parts[0] if parts else "",
            "plate": parts[1] if len(parts) > 1 else "",
            "note": " ".join(parts[2:]) if len(parts) > 2 else "",
        }

    if command in {"拉回改派", "拉回", "改派"}:
        event_type = "recalled"
    elif command in {"取消", "訂單取消"}:
        event_type = "cancelled"
    elif command in {"換司機", "改司機"}:
        event_type = "driver_change"
    elif command == "恢復":
        event_type = "restored"
    else:
        event_type = "edited"

    changes = parse_change_note(tail) if event_type == "edited" else {}
    return {
        "command": "event",
        "event_type": event_type,
        "order_id": oid,
        "note": tail,
        "changes": changes,
    }


def _cli() -> None:
    p = argparse.ArgumentParser(description="訂單每日簡表 V1")
    p.add_argument("xlsx", help="Google Sheet 匯出的 xlsx")
    p.add_argument("--sheet", required=True, help="月份分頁，例如 2月")
    p.add_argument("--date", required=True, help="日期，例如 2/1")
    p.add_argument("--events", default="order_events_v1.json", help="事件 JSON")
    p.add_argument("--show-unassigned", action="store_true", help="顯示【未派】")
    args = p.parse_args()

    orders = parse_rows(load_rows_from_xlsx(args.xlsx, args.sheet))
    store = EventStore(args.events)
    print(generate_daily_summary(orders, args.date, store, show_unassigned=args.show_unassigned))


if __name__ == "__main__":
    _cli()
