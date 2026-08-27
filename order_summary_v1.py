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
ORDER_ID_RE = re.compile(r"\b(?:\d{2}KK\d{9}|ORD\d{10}|[A-Z]{3}\d{6})\b", re.I)
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
    dep = fields.get("出發日期", "")
    flight = fields.get("航班編號", "")
    # Explicit designated pickup time wins for airport pickup.
    if trip_type == "接機":
        m = re.search(r"指定時間\s*([0-2]?\d\s*[:：]\s*[0-5]\d)", dep)
        if m:
            return time_hhmm(m.group(1))
        # Prefer 【HH:MM】, then （HH:MM）/(HH:MM), then any time in flight text.
        for pattern in [r"【\s*([^】]+)\s*】", r"[（(]\s*([^）)]+)\s*[）)]"]:
            m = re.search(pattern, flight)
            if m:
                t = time_hhmm(m.group(1))
                if t:
                    return t
        return time_hhmm(flight)
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
        r"嬰兒座椅|兒童安全座椅|向後式嬰兒安全座椅|向後式座椅|"
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
    }
    for e in events:
        et = e.event_type.lower()
        if et == "assigned":
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
        elif et == "edited":
            state["edit_count"] += 1
        elif et == "cancelled":
            state["cancelled"] = True
            state["assigned"] = False
        elif et == "restored":
            state["cancelled"] = False

    if state["cancelled"]:
        return "取消", state
    reassigned = state["assigned"] and state["assigned_count"] >= 2 and state["recall_count"] >= 1
    edited = state["edit_count"] > 0
    if reassigned and edited:
        return "異動・重派・已派", state
    if reassigned:
        return "重派・已派", state
    if edited and state["assigned"]:
        return "異動・已派", state
    if edited:
        return "異動・未派", state
    if state["assigned"]:
        return "已派", state
    return "未派", state


def compact_tags(order: Order) -> str:
    tags = []
    if order.vehicle_tag:
        tags.append(order.vehicle_tag)
    tags.extend(order.extra_tags)
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
    """Parse deterministic V1 LINE commands.

    Supported:
      派車 KBM318185 王小明 ABC-1234
      拉回 KBM318185 班機延誤
      取消 KBM318185 客人取消
      恢復 KBM318185
      異動 KBM318185 班機延誤 10:25→11:40
      歷程 KBM318185
      簡表 2/1
    """
    text = normalize_spaces(text)
    m = re.match(r"^(派車|拉回|取消|恢復|異動|歷程|簡表)\s+(.+)$", text)
    if not m:
        return None
    command, rest = m.group(1), m.group(2).strip()
    if command == "簡表":
        return {"command": "summary", "date": rest}
    oid_m = ORDER_ID_RE.search(rest)
    if not oid_m:
        return None
    oid = oid_m.group(0).upper()
    tail = normalize_spaces(rest[oid_m.end():])
    if command == "派車":
        parts = tail.split(" ") if tail else []
        return {
            "command": "event", "event_type": "assigned", "order_id": oid,
            "driver_name": parts[0] if parts else "",
            "plate": parts[1] if len(parts) > 1 else "",
            "note": " ".join(parts[2:]) if len(parts) > 2 else "",
        }
    mapping = {"拉回": "recalled", "取消": "cancelled", "恢復": "restored", "異動": "edited"}
    if command == "歷程":
        return {"command": "history", "order_id": oid}
    changes = parse_change_note(tail) if command == "異動" else {}
    return {"command": "event", "event_type": mapping[command], "order_id": oid, "note": tail, "changes": changes}


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
