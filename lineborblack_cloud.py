import os
import re

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
# 🚨 黑名單資料
# =====================================================

blocked_list = [

    {
        "name": "高崇正",
        "plate": "RFR-9770",
        "phone": "0932395446",
        "vehicle": "賓士 V250d 航空椅",
        "color": "黑色",
        "bank_code": "808",
        "account": "0897979011081"
    },

    {
        "name": "高崇中",
        "plate": "RFE-0653",
        "phone": "0910620051",
        "vehicle": "賓士 S350dL",
        "color": "黑色",
        "bank_code": "822",
        "account": "819540373158"
    },

    {
        "name": "李宗頤",
        "plate": "RFT-0107",
        "phone": "0916267185",
        "vehicle": "STARIA 黑",
        "color": "黑色"
    },

    {
        "name": "陳志成",
        "plate": "RCX-5701",
        "phone": "0937464188",
        "vehicle": "CAMRY 五座休旅",
        "color": "黑色／油電",
        "seats": "5人座",
        "bank_code": "822",
        "account": "129540807686"
    },

    {
        "name": "林裕彥",
        "plate": "RCQ-9610",
        "phone": "0916636275",
        "vehicle": "VITO",
        "color": "黑色"
    },

    {
        "name": "吳政澔",
        "plate": "RED-6533",
        "phone": "0958138552",
        "vehicle": "特斯拉 Model X",
        "color": "白色",
        "seats": "正7",
        "bank_code": "822",
        "account": "750540087529"
    },

    {
        "name": "黃承鴻",
        "plate": "RFY-0116",
        "phone": "0926973350",
        "vehicle": "SKODA OCTAVIA",
        "color": "白色",
        "bank_code": "822",
        "account": "026800003647"
    },

    {
        "name": "安達發",
        "plate": "RDM-2586",
        "phone": "0981555059",
        "vehicle": "CC",
        "color": "白色",
        "bank_code": "700",
        "account": "00810110948772"
    },

    {
        "name": "吳志鋒",
        "plate": "RCE-2981",
        "phone": "0963759863",
        "vehicle": "現代八人座",
        "color": "黑色",
        "bank_code": "700",
        "account": "00210880599216"
    },

    {
        "name": "黃竑嘉",
        "plate": "RCZ-3673",
        "phone": "0986932253",
        "vehicle": "Corolla Cross",
        "color": "白色",
        "bank_code": "822",
        "account": "381531663800"
    },

    {
        "name": "賴俊男",
        "plate": "RCR-5636",
        "phone": "0925262619",
        "vehicle": "VW 9座",
        "color": "灰色",
        "seats": "客6",
        "bank_code": "812",
        "account": "21071010133441"
    },

    {
        "name": "陳松柏",
        "plate": "RFK-7118",
        "phone": "0920004221",
        "vehicle": "2024 賓士 Vito 九人座",
        "color": "黑色",
        "bank_code": "822",
        "account": "901562799932"
    },

    {
        "name": "黃仁政",
        "plate": "",
        "phone": "0966239898",
        "vehicle": "STARIA-C",
        "color": "黑色",
        "seats": "8"
    },

    {
        "name": "闕士曄",
        "plate": "RFT-0993",
        "phone": "0919374374",
        "vehicle": "SIENTA 假七人座",
        "color": "黑色",
        "bank_code": "822",
        "account": "495540215449"
    },

    {
        "name": "彭緒宏",
        "plate": "",
        "phone": "0916472399",
        "vehicle": "CAMRY",
        "color": "黑色"
    },

    {
        "name": "謝弦忠",
        "plate": "RFR-3100",
        "phone": "0981746350",
        "vehicle": "Toyota RAV4",
        "color": "白色"
    },

    {
        "name": "康致銘",
        "plate": "RCE-9555",
        "phone": "0972807549",
        "vehicle": "Odyssey 7人座",
        "color": "銀色",
        "bank_code": "822",
        "account": "141540302606"
    },

    {
        "name": "簡伶珊",
        "plate": "RFY-9733",
        "phone": "0926307065",
        "vehicle": "Toyota Cross 五座休旅車",
        "color": "灰色",
        "bank_code": "822",
        "account": "266540114928"
    },

    {
        "name": "李玉綸",
        "plate": "RFR-3157",
        "phone": "0975621975",
        "vehicle": "RAV4",
        "color": "白色",
        "bank_code": "822",
        "account": "222540827987"
    },

    {
        "name": "王彥章",
        "plate": "RFQ-9982",
        "phone": "0932045133",
        "vehicle": "Ford Kuga 休旅 2.0 Turbo",
        "color": "白色",
        "bank_code": "822",
        "account": "635540088287"
    },

    {
        "name": "蘇凡宸",
        "plate": "RFY-5723",
        "phone": "0979123666",
        "vehicle": "Lexus ES300",
        "color": "黑色",
        "bank_code": "822",
        "account": "370532854409"
    },

    {
        "name": "羅邦忠",
        "plate": "RFA-7119",
        "phone": "0909066040",
        "vehicle": "CROSS",
        "color": "黑色",
        "bank_code": "822",
        "account": "901566894668"
    },

    {
        "name": "張頌榮",
        "plate": "RFF-8333",
        "phone": "0983571052",
        "vehicle": "賓士 Vito 9座",
        "color": "黑色",
        "bank_code": "822",
        "account": "864540153446"
    },

    {
        "name": "黃榮鋒",
        "plate": "RFM-3195",
        "phone": "0977706600",
        "vehicle": "V250d"
    },

    {
        "name": "王毓勤",
        "plate": "RAS-3969",
        "phone": "0922827657",
        "vehicle": "Lexus NX200",
        "color": "黑色"
    },

    {
        "name": "陳莛豫",
        "plate": "REA-1930",
        "phone": "0960810511",
        "vehicle": "Tesla Model X",
        "color": "黑色",
        "bank_code": "822",
        "account": "901563176198"
    },

    {
        "name": "李秉憲",
        "plate": "RDM-2285",
        "phone": "0936867773",
        "vehicle": "RAV4",
        "color": "白色"
    },

    {
        "name": "楊祥麟",
        "plate": "RFM-1036",
        "phone": "0926957277",
        "vehicle": "新現代九座",
        "bank_code": "822",
        "account": "417540346464"
    },
    {
        "name": "粘祐誠",
        "plate": "RDY-3907",
        "phone": "0935757802",
        "vehicle": "Camry",
        "color": "黑色"
    },
    {
        "name": "kenny 趙國富",
        "plate": "RAM-2307",
        "phone": "0958355840",
        "vehicle": "現代SantaFe",
        "color": "鐵灰",
        "bank_code": "822",
        "account": "657530129260"
    },
    {
        "name": "黃冠銘",
        "plate": "RFR-2895",
        "phone": "0958355840",
        "vehicle": "ToyotaCC",
        "color": "白色",
        "bank_code": "822",
        "account": "462540482823"
    },
    {
        "name": "王祥",
        "plate": "RAS-7615",
        "phone": "0938677650",
        "vehicle": "sienta",
        "color": "綠色",
        "bank_code": "822",
        "account": "794540115392"
    },
    {
        "name": "何俊志",
        "plate": "RDS-0185",
        "phone": "0972-222-169",
        "vehicle": "V250d",
        "color": "深藍色",
        "bank_code": "822",
        "account": "285600097393"
    },
    {
        "name": "張榮譽",
        "plate": "RCQ-9399",
        "phone": "0989742468",
        "vehicle": "Lexus ES300H",
        "color": "白色",
        "bank_code": "822",
        "account": "761540058123"
    },
    {
        "name": "張頌榮",
        "plate": "RFF-8333",
        "phone": "0983571052",
        "vehicle": "賓士vito",
        "color": "黑色",
        "bank_code": "822",
        "account": "864540153446"
    },
    {
        "name": "林俊宏",
        "plate": "RCE-5617",
        "phone": "0903964909",
        "vehicle": "Foucs",
        "color": "灰色",
        "bank_code": "822",
        "account": "174540648447"
    },
    {
        "name": "蕭志昌",
        "plate": "RDG-9365",
        "phone": "0985810567",
        "vehicle": "現代 Starex",
        "color": "銀色",
        "bank_code": "822",
        "account": "204530059673"
    },
    {
        "name": "蔣建國",
        "plate": "RFD-9252",
        "phone": "0906802200",
        "vehicle": "新現代九座",
        "color": "黑色",
        "bank_code": "822",
        "account": "381540729230"
    },
    {
        "name": "鷹雄（翁義雄)",
        "plate": "RCE-6703",
        "phone": "0936834889",
        "vehicle": "福斯 九人座",
        "color": "黑色",
        "bank_code": "822",
        "account": "142540188560"
    },
    {
        "name": "蔡承展",
        "plate": "RCE-1516",
        "phone": "0981-970-355",
        "vehicle": "WISH",
        "color": "銀色"
    },
    {
        "name": "羅邦忠",
        "plate": "RFA-7119",
        "phone": "0909-066-040",
        "vehicle": "CROSS",
        "color": "黑色",
        "bank_code": "822",
        "account": "901566894668"
    },
    {
        "name": "陳正東",
        "plate": "RFJ-2125",
        "phone": "0900344664",
        "vehicle": "Toyota RAV4休旅",
        "color": "白色黑頂",
        "bank_code": "822",
        "account": "772540243503"
    },
    {
        "name": "卓政義",
        "plate": "RBU-0993",
        "phone": "0935154441",
        "vehicle": "VITO",
        "color": "黑色"
    },
    {
        "name": "王定洲",
        "plate": "RFD-9251",
        "phone": "0952085666",
        "vehicle": " Lexus es300h",
        "color": "白色",
        "bank_code": "822",
        "account": "293540054718"
    },
    {
        "name": "林佩誼",
        "plate": "RFB-6213",
        "phone": "0908873139",
        "vehicle": " wish",
        "color": "黑色"
    },
    {
        "name": "邱品元",
        "plate": "RFT-1370",
        "phone": "0958737025",
        "vehicle": "Toyota CC",
        "color": "白色",
        "bank_code": "700",
        "account": "02910390406138"
    },
    {
        "name": "陳錡【短短】",
        "plate": "RDG-8762",
        "phone": "0976-776-522",
        "vehicle": "福斯T6 - 8座",
        "color": "棕色",
        "bank_code": "822",
        "account": "285540203166"
    },
    {
        "name": "柳朝文",
        "plate": "RFX-2326",
        "phone": "0987807520",
        "vehicle": "Toyota cc",
        "color": "白色",
        "bank_code": "822",
        "account": "152536708708"
    },
    {
        "name": "陳睿霆",
        "plate": "RFG-9507",
        "phone": "0958470999",
        "vehicle": "MGZS",
        "color": "灰色"
    },
    {
        "name": "鄭宇辰",
        "plate": "REB-1907",
        "phone": "0906838919",
        "vehicle": "tesla",
        "bank_code": "822",
        "account": "901510846811"
    },
    {
        "name": "蔡儀龍",
        "plate": "REB-1753",
        "phone": "0979356886",
        "vehicle": "tesla model x",
        "color": "白色",
        "bank_code": "822",
        "account": "163535285402"
    },
    {
        "name": "高志成",
        "plate": "RFH-8281",
        "phone": "0922700016",
        "vehicle": "ES300H油電",
        "color": "鈦色",
        "bank_code": "822",
        "account": "624530197209"
    },
    {
        "name": "陳小宇",
        "plate": "RFD-5926",
        "phone": "0955-888758",
        "vehicle": "豐田 WISH",
        "bank_code": "012",
        "account": "401168061069"
    },   
    {
        "name": "王彥章",
        "plate": "RFQ-9982",
        "phone": "0932045133",
        "vehicle": "Ford Kuga 休旅2.0 Turbo",
        "color": "白色",
        "bank_code": "822",
        "account": "635540088287"
    },
    {
        "name": "李玉綸",
        "plate": "RFR-3157",
        "phone":[
            "0975621975",
            "0933619628"
        ],
        "vehicle": "RAV4",
        "color": "白色",
        "bank_code": "822",
        "account": "635540088287"
    },
    {
        "name": "侯尚謙/侯冠廷",
        "plate": "RFW-3116",
        "phone": "0955898650",
        "vehicle": "BENZ VITO",
        "color": "黑色",
        "bank_code": "822",
        "account": "794540161511"
    },
    {
        "name": "郭偉壯",
        "plate": "RFU-1381",
        "phone": "0921-512-772",
        "vehicle": "賓士Vito Tourer",
        "color": "黑色",
        "bank_code": "822",
        "account": "266540239599"
    },
    {
        "name": "林啟聖Jason",
        "plate": "RDM-1931",
        "phone": "0935875796",
        "vehicle": "VolkswagonT6",
        "color": "黑色",
        "bank_code": "822",
        "account": "026540123942"
    },
    {
        "name": "黃順福",
        "plate": "RDY-8256",
        "phone": "0916827102",
        "vehicle": "granvia 8座",
        "color": "黑色"
    },
    {
        "name": "簡伶珊",
        "plate": "RFY-9733",
        "phone": "0926307065",
        "vehicle": "Toyota Cross",
        "color": "灰色",
        "bank_code": "822",
        "account": "266540114928"
    },
    {
        "name": "呂小七（青陽）",
        "plate": "RDM-0166",
        "phone":[
            "0985676447",
            "0933061999"
        ],
        "vehicle": "賓士 v250d 八座",
        "color": "銀色",
        "bank_code": "822",
        "account": "314540665536"
    },
    {
        "name": "康致銘",
        "plate": "RCE-9555",
        "phone": "0972807549",
        "vehicle": "Odyssey 7人座",
        "color": "銀色",
        "bank_code": "822",
        "account": "141540302606"
    },
    {
        "name": "羅宏文",
        "plate": "ROO-0000",
        "phone": "0926171881",
        "vehicle": "Granvia八座",
        "color": "白色"
    },
    {
        "name": "彭緒宏",
        "plate": "ROO-0000",
        "phone": "0916472399",
        "vehicle": "CAMRY",
        "color": "黑色"
    },
    {
        "name": "杜致緯",
        "plate": "RFB-9397",
        "phone": "0900614468",
        "vehicle": "VW T6.1",
        "color": "銀色",
        "bank_code": "700",
        "account": "00012981085730"
    },
    {
        "name": "李宗頤",
        "plate": "RFT-0107",
        "phone": "0916267185",
        "vehicle": "STARIA",
        "color": "黑色",
        "bank_code": "822",
        "account": "163540294080"
    },
    {
        "name": "林佩誼",
        "plate": "RDV-7037",
        "phone": "0908873139",
        "vehicle": "福斯九人座",
        "color": "黑色"
    },
    {
        "name": "游坤南",
        "plate": "RFR-0567",
        "phone": "0967172266",
        "vehicle": "Toyota RAV4",
        "color": "黑色",
        "bank_code": "822",
        "account": "082540609536"
    },
    {
        "name": "杜肯西",
        "plate": "RFR-8785",
        "phone": "0909651097",
        "vehicle": "Yaris Crossover",
        "color": "白色",
        "bank_code": "017",
        "account": "20713637599"
    },
    {
        "name": "高文富",
        "plate": "REA-7331",
        "phone": "0907735537",
        "vehicle": "Tesla Model Y",
        "color": "白色",
        "bank_code": "822",
        "account": "026540567074"
    },
    {
        "name": "許耀太",
        "plate": "RFT-1802",
        "phone": "0965827763",
        "vehicle": "THonda CRV",
        "bank_code": "822",
        "account": "303533739707"
    }
]




# =====================================================
# ⚠️ 特殊註記資料
#
# 注意：
# 這裡不是黑名單
# 可以正常派單
# 只是搜尋到時提醒
# =====================================================

special_notes = [

    {
        "name": "余家婕",
        "plate": "RFR-9671",
        "phone": "0917235666",
        "vehicle": "RAV4",
        "color": "灰色",
        "note": "無理由退單"
    },
    {
        "name": "賴志憲 Jackson",
        "plate": "RFF-9903",
        "phone": "0982062587",
        "vehicle": "Benz 航空椅",
        "color": "黑色",
        "note": "標報時又退單，請留意"
    },
    {
        "name": "謝華軒",
        "plate": "REB-6956",
        "phone": "0988-083-095",
        "vehicle": "Tesla Model Y",
        "color": "黑色",
        "bank_code": "822",
        "account": "185540243598",
        "note": "長租車，請留意保單"
    },
    {
        "name": "羅暐晟",
        "plate": "RFK-5392",
        "phone": "0909480959",
        "vehicle": "大G",
        "color": "黑色",
        "bank_code": "822",
        "account": "864975077773",
        "note": "長租車，請留意保單"
    },
    {
        "name": "賴炳男",
        "plate": "RFV-1153",
        "phone": "0924118168",
        "vehicle": "v250d",
        "color": "米白色",
        "bank_code": "822",
        "account": "255540455229",
        "note": "長租車，請留意保單"
    }
]


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
    phone = normalize_phone(item.get("phone", []))
    plate = item.get("plate", "")
    vehicle = item.get("vehicle", "未提供")
    color = item.get("color", "未提供")
    bank_code = item.get("bank_code", "")
    account = item.get("account", "")

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
    phone = normalize_phone(item.get("phone", []))
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

    normalized_phone = normalize_phone(phone)

        if normalized_phone in normalized_numbers:
            return "blacklist", item, "電話"


    # -----------------------------
    # 黑名單
    # -----------------------------

    for item in blocked_list:

        phone = normalize_phone(
            item.get("phone", "")
        )

        if not phone:
            continue

        if phone in normalized_numbers:
            return "blacklist", item, "電話"


    # -----------------------------
    # 特殊註記
    # -----------------------------

    for item in special_notes:

        phone = normalize_phone(
            item.get("phone", "")
        )

        if not phone:
            continue

        if phone in normalized_numbers:
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
    return "LINE bot is running", 200


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
