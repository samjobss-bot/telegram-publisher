#!/usr/bin/env python3
"""
Sam Jobs - Telegram publisher.

Watches the PUBLIC feed of the blog. When a new published post appears, it sends:
  1) to the official channel, then
  2) to the matching region / type channels (decided from the post labels and city).

Drafts are never visible in the public feed, so nothing is sent before you publish.
Needs only the standard library. The bot token comes from the TELEGRAM_BOT_TOKEN env var.
Set DRY_RUN=1 to print the messages instead of sending them.
"""
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BLOG = "https://samjobss.blogspot.com"
FEED_URL = BLOG + "/feeds/posts/default?alt=json&max-results=25"
STATE_FILE = os.environ.get("STATE_FILE", "sent.json")
# keep only the characters a Telegram token can contain (drops hidden RTL marks, spaces, new lines)
TOKEN = re.sub(r"[^0-9A-Za-z:_-]", "", os.environ.get("TELEGRAM_BOT_TOKEN", ""))
DRY_RUN = os.environ.get("DRY_RUN") == "1"

OFFICIAL = "@samjobs_team"

CHANNELS = {
    "riyadh": "@samjobs_alriyadh",
    "jeddah": "@samjobs_jeddah",
    "makkah": "@samjobs_makkah",
    "madinah": "@samjobs_almadinah",
    "qassim": "@samjobs_alqassim",
    "dammam": "@samjobs_aldammam",
    "military": "@samjobs_alaskaryah",
    "remote": "@samjobs_online",
}

# channel key -> words searched in the post labels and in the city field
ROUTES = [
    ("military", ["عسكري"]),
    ("riyadh", ["الرياض"]),
    ("jeddah", ["جدة"]),
    ("makkah", ["مكة"]),
    ("madinah", ["المدينة المنورة", "المدينة"]),
    ("qassim", ["القصيم", "بريدة", "عنيزة"]),
    ("dammam", ["الدمام", "المنطقة الشرقية", "الخبر", "الظهران", "الأحساء", "الجبيل", "القطيف"]),
    ("remote", ["عن بعد"]),
]

# names of the fields in the "job details" list of the article template
FIELDS = {
    "title": ["المسمى الوظيفي", "المسمى", "الوظيفة", "البرنامج"],
    "org": ["الجهة", "جهة العمل", "الشركة"],
    "city": ["المدينة / المنطقة", "المدينة/المنطقة", "المدينة", "المنطقة", "الموقع"],
    "period": ["فترة التقديم", "موعد التقديم", "التقديم"],
}

END_LABEL = re.compile(r"^انتهاء\s*-\s*\d{4}-\d{2}-\d{2}$")
PLACEHOLDERS = ("...", "…", "اسم الجهة", "المسمى الوظيفي", "المدينة", "ضع ")


# military / security entities: a post that mentions any of them also goes to the military channel
MILITARY_NAMES = [
    "وزارة الدفاع",
    "القوات البرية الملكية السعودية",
    "قوات الدفاع الجوي الملكي السعودي",
    "القوات الجوية الملكية السعودية",
    "القوات البحرية الملكية السعودية",
    "قوة الصواريخ الاستراتيجية",
    "الخدمات الصحية لوزارة الدفاع",
    "وزارة الحرس الوطني",
    "رئاسة بقوات الحرس الوطني",
    "وزارة الداخلية",
    "الأمن العام",
    "شرطة",
    "المرور",
    "أمن الطرق",
    "دوريات الأمن",
    "قوات الطوارئ الخاصة",
    "حرس الحدود",
    "المديرية العامة للسجون",
    "المديرية العامة للدفاع المدني",
    "قوات الأمن الخاصة",
    "الإدارة العامة لحماية المنشآت",
    "الجوازات",
    "رئاسة الاستخبارات العامة",
    "رئاسة أمن الدولة",
    "المباحث العامة",
    "طيران الأمن",
    "رئاسة الحرس الملكي",
    "الهيئة الملكية للجبيل وينبع",
    "الإدارة العسكرية",
    "أمن المنشآت الصناعية",
    "الكليات والمعاهد العسكرية",
    "كلية الملك عبد العزيز الحربية",
    "كلية الملك فهد الأمنية",
    "كلية الملك فهد الحربية",
    "كلية الدفاع الجوي",
    "كلية الأمير سلطان بن عبد العزيز العسكرية",
    "كلية الملك خالد العسكرية",
    "كلية قوى الأمن الداخلي",
    "معهد التدريب العسكري المهني",
    "معهد حرس الحدود",
    "قوات الطوارئ",
    "كلية الملك فيصل الجوية",
    "الإدارة العامة للخدمات الطبية للقوات المسلحة المستشفيات العسكرية",
    "مركز الحرب الجوية",
    "القوات الخاصة الأمنية",
    "الخدمات الطبية للقوات المسلحة",
    "المستشفيات العسكرية",
]


def norm(text):
    """Arabic text for matching: no diacritics / tatweel, unified alef, teh marbuta and yeh."""
    text = re.sub(r"[\u064B-\u0652\u0640]", "", text or "")
    text = re.sub("[أإآ]", "ا", text)
    return text.replace("ة", "ه").replace("ى", "ي")


_MILITARY_NORM = [(norm(n), n) for n in MILITARY_NAMES]


def military_match(text):
    """Returns the first military entity found in the text, or ''."""
    t = norm(text)
    for n, original in _MILITARY_NORM:
        if n and n in t:
            return original
    return ""


def log(*a):
    print(*a, file=sys.stderr)


def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "samjobs-telegram-bot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_posts():
    data = http_json(FEED_URL)
    return (data.get("feed") or {}).get("entry") or []


def html_to_lines(content):
    t = re.sub(r"(?i)</(li|p|div|h\d|tr)>|<br\s*/?>", "\n", content or "")
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t).replace("\xa0", " ")
    return [ln.strip() for ln in t.split("\n") if ln.strip()]


def clean_value(v):
    v = v.strip(" \t:-•*")
    if not v or v in PLACEHOLDERS or any(v.startswith(p) for p in ("ضع ", "...")):
        return ""
    return v


def parse_details(content):
    out = {}
    for line in html_to_lines(content):
        m = re.match(r"^[•\-*\s]*([^:：]{2,40})[:：]\s*(.+)$", line)
        if not m:
            continue
        key, value = m.group(1).strip(), clean_value(m.group(2))
        if not value:
            continue
        for field, names in FIELDS.items():
            if field not in out and key in names:
                out[field] = value
                break
    return out


def first_image(content):
    """The first image of the post (the employer logo) at a decent size, or ''."""
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', content or "", re.I)
    if not m:
        return ""
    url = html.unescape(m.group(1)).strip()
    if not url.startswith("http"):
        return ""
    url = url.replace("http://", "https://", 1)
    return re.sub(r"/s\d{2,4}(-c)?/", "/s800/", url)


def parse_entry(entry):
    link = ""
    for l in entry.get("link", []):
        if l.get("rel") == "alternate":
            link = l.get("href", "")
    link = link.split("?")[0].replace("http://", "https://")
    labels = [c.get("term", "") for c in entry.get("category", []) if c.get("term")]
    labels = [x for x in labels if not END_LABEL.match(x)]
    content = (entry.get("content") or {}).get("$t") or (entry.get("summary") or {}).get("$t") or ""
    details = parse_details(content)
    title = (entry.get("title") or {}).get("$t", "").strip()
    page_text = " ".join([title] + labels + html_to_lines(content))
    return {
        "military": military_match(page_text),
        "image": first_image(content),
        "id": (entry.get("id") or {}).get("$t", link),
        "title": details.get("title") or title,
        "org": details.get("org", ""),
        "city": details.get("city", ""),
        "period": details.get("period", ""),
        "labels": labels,
        "link": link,
    }


def route(post):
    """Returns the list of target channels: official first, then matching ones."""
    haystack = " ".join(post["labels"] + [post["city"]])
    targets = [OFFICIAL]
    if post.get("military"):
        targets.append(CHANNELS["military"])
    for key, words in ROUTES:
        if any(w in haystack for w in words):
            ch = CHANNELS[key]
            if ch not in targets:
                targets.append(ch)
    return targets


YEAR_RE = re.compile(r"\s*(?<![0-9٠-٩/\-])(?:19|20|١٩|٢٠)[0-9٠-٩]{2}(?![0-9٠-٩/\-])(?:هـ|م(?![ء-ي]))?")


def strip_year(text):
    """'من 1 أكتوبر 2026 إلى 30 أكتوبر 2026' -> 'من 1 أكتوبر إلى 30 أكتوبر' (Telegram only)."""
    return re.sub(r"\s{2,}", " ", YEAR_RE.sub("", text)).strip()


def build_message(post):
    esc = html.escape
    lines = ["💼 <b>%s</b>" % esc(post["title"])]
    if post["org"]:
        lines.append("🏢 %s" % esc(post["org"]))
    if post["city"]:
        lines.append("📍 %s" % esc(post["city"]))
    if post["period"]:
        lines.append("📅 التقديم: %s" % esc(strip_year(post["period"])))
    lines.append("")
    lines.append('🔗 للتقديم: <a href="%s">اضغط هنا</a>' % html.escape(post["link"], quote=True))
    return "\n".join(lines)


def tg(method, params):
    if DRY_RUN:
        print("---- %s %s ----\n%s\n" % (method, params.get("chat_id"), params.get("caption") or params.get("text")))
        return True
    url = "https://api.telegram.org/bot%s/%s" % (TOKEN, method)
    body = urllib.parse.urlencode(params).encode("utf-8")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=30) as r:
                return json.loads(r.read().decode("utf-8")).get("ok", False)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "ignore")
            try:
                info = json.loads(raw)
            except ValueError:
                info = {}
            if e.code == 429:
                wait = int((info.get("parameters") or {}).get("retry_after", 5)) + 1
                log("rate limited, waiting", wait)
                time.sleep(wait)
                continue
            log("telegram error", method, params.get("chat_id"), e.code, info.get("description", raw[:200]))
            return False
        except Exception as e:  # network problem
            log("network error", params.get("chat_id"), e)
            time.sleep(3)
    return False


def send(chat, text, image=""):
    """Photo with the text as its caption; plain text if there is no image or the photo fails."""
    if image and len(text) <= 1000:
        if tg("sendPhoto", {"chat_id": chat, "photo": image, "caption": text, "parse_mode": "HTML"}):
            return True
        log("photo failed, sending text only:", chat)
    return tg("sendMessage", {
        "chat_id": chat,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": json.dumps({"is_disabled": True}),
        "disable_web_page_preview": "true",
    })


def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f).get("sent", [])


def save_state(sent):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"sent": sent[-500:]}, f, ensure_ascii=False, indent=1)


def main():
    if not TOKEN and not DRY_RUN:
        log("TELEGRAM_BOT_TOKEN is missing")
        return 1
    posts = [parse_entry(e) for e in fetch_posts()]
    sent = load_state()

    if sent is None:  # first run: remember what already exists, send nothing
        save_state([p["id"] for p in posts])
        log("first run: %d existing posts recorded, nothing sent" % len(posts))
        return 0

    new = [p for p in reversed(posts) if p["id"] not in sent]
    log("new posts: %d" % len(new))
    changed = False
    for p in new:
        text = build_message(p)
        targets = route(p)
        if p["military"]:
            log("military entity found: %s | %s" % (p["military"], p["title"]))
        ok_official = send(targets[0], text, p["image"])
        if not ok_official:
            log("official channel failed, will retry next run:", p["title"])
            continue
        for ch in targets[1:]:
            time.sleep(1.5)
            if not send(ch, text, p["image"]):
                log("failed:", ch, p["title"])
        sent.append(p["id"])
        changed = True
        time.sleep(1.5)
    if changed:
        save_state(sent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
