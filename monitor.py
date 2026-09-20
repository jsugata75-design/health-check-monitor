#!/usr/bin/env python3
"""health-check-monitor — ヘルチェック Web予約 空き枠監視＋自動予約変更

既存予約（人間ドックC / 大宮センター 2027-01-19）を、
「セデーション・鎮静剤」付きで 2026-12-31 までの平日枠へ前倒しする。

流れ（すべて ASP.NET WebForms の POST 連鎖。ブラウザ不要）:
  wpgLogin.aspx  --ログイン-->  Default.aspx
  --予約状況を確認する--> wpgWBP01010  --予約変更--> wpgWBP02141 (STEP3 オプション)
  --セデーション✓+次に進む--> wpgWBP02151 (STEP4 施設・予約日: 2週間カレンダー)
  --前週×N で 9月まで遡りながら ○ を収集
  [AUTO_RESERVE] ○クリック → 時間ラジオ → STEP5 料金確認 → STEP6 お客様情報(既定値のまま)
                 → STEP7 予約内容確認 → 「この内容で予約する」 → STEP8 完了

認証情報は Keychain (service=health-check-web, account=ログインID) から読む。
このスクリプトはパスワードをファイルに書かない。
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import html
import http.cookiejar
import json
import os
import re
import smtplib
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.mime.text import MIMEText
from email.utils import formatdate
from pathlib import Path

BASE = "https://web.health-check.jp/webreserve/"
URL_LOGIN = BASE + "wpgLogin.aspx"
URL_TOP = BASE + "Default.aspx"

HERE = Path(__file__).resolve().parent
STATE_PATH = HERE / "state.json"
HISTORY_PATH = HERE / "history.jsonl"
LOCK_PATH = HERE / ".lock"
SNAP_DIR = HERE / "snapshots"

# ---------------------------------------------------------------- 設定
# ログインID（画面右上の ID）。公開リポジトリに置くため既定値はコードに書かない。
# 優先順: 環境変数 HC_LOGIN_ID → login_id.txt（gitignore 済み）
LOGIN_ID = os.environ.get("HC_LOGIN_ID", "").strip() or (
    (HERE / "login_id.txt").read_text().strip() if (HERE / "login_id.txt").exists() else "")
KEYCHAIN_SERVICE = os.environ.get("HC_KEYCHAIN_SERVICE", "health-check-web")

# 対象センター（優先順）。cdofc は STEP4 の <a cdofc="..."> 属性値
CENTERS = [
    ("001011", "大宮センター"),
    ("001007", "池袋センター"),
    ("001003", "新宿西口センター"),
    ("001012", "渋谷アクシュ"),
    ("001008", "日本橋センター"),
]
CENTER_PRIORITY = {c: i for i, (c, _) in enumerate(CENTERS)}
# 表示用（対象外センターも名前で出す）。2026-09-14 に STEP4 から採取
CENTER_NAME = {
    "001001": "横浜東口センター", "001002": "横浜西口センター", "001003": "新宿西口センター",
    "001004": "レディース横浜", "001005": "ファーストプレイス横浜", "001006": "レディース新宿",
    "001007": "池袋センター", "001008": "日本橋センター", "001009": "川崎センター",
    "001010": "横濱ゲートタワー", "001011": "大宮センター", "001012": "渋谷アクシュ",
}
CENTER_NAME.update(dict(CENTERS))

DEADLINE = dt.date.fromisoformat(os.environ.get("HC_DEADLINE", "2026-12-31"))
EXCLUDE_DATES = {
    dt.date.fromisoformat(x.strip())
    for x in os.environ.get("HC_EXCLUDE", "2026-10-31,2026-11-11,2026-12-02").split(",")
    if x.strip()
}
ALLOW_WEEKDAYS = {int(x) for x in os.environ.get("HC_WEEKDAYS", "0,1,2,3,4").split(",")}  # 月=0
ALLOW_AMPM = {x.strip().upper() for x in os.environ.get("HC_AMPM", "AM,PM").split(",")}
MIN_LEAD_DAYS = int(os.environ.get("HC_MIN_LEAD_DAYS", "2"))  # 受診前日・当日はWeb変更不可
AUTO_RESERVE = os.environ.get("AUTO_RESERVE", "1") == "1"
SEDATION_LABEL = "セデーション・鎮静剤"
TIME_PREF = os.environ.get("HC_TIME_PREF", "earliest")  # earliest | latest

UA = os.environ.get("MONITOR_UA", "health-check-monitor/1.0 (personal use)")
REQ_INTERVAL = float(os.environ.get("HC_REQ_INTERVAL", "1.0"))


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- HTML helpers

def unesc(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def attr(tag: str, name: str) -> str | None:
    m = re.search(r'\b%s\s*=\s*"([^"]*)"' % re.escape(name), tag, re.I)
    if not m:
        m = re.search(r"\b%s\s*=\s*'([^']*)'" % re.escape(name), tag, re.I)
    return html.unescape(m.group(1)) if m else None


def form_fields(page: str) -> dict[str, str]:
    """フォームの現在値（hidden/text/select/チェック済みradio,checkbox）を集める。
    submit ボタンは含めない（押すものだけ呼び出し側で足す）。"""
    fields: dict[str, str] = {}
    for tag in re.findall(r"<input\b[^>]*>", page, re.I):
        name = attr(tag, "name")
        if not name:
            continue
        typ = (attr(tag, "type") or "text").lower()
        if typ in ("submit", "button", "image", "reset", "file"):
            continue
        if typ in ("checkbox", "radio"):
            if re.search(r"\bchecked\b", tag, re.I):
                fields[name] = attr(tag, "value") or "on"
            continue
        fields[name] = attr(tag, "value") or ""
    for m in re.finditer(r"<select\b([^>]*)>(.*?)</select>", page, re.I | re.S):
        name = attr(m.group(1), "name")
        if not name:
            continue
        val = ""
        for opt in re.findall(r"<option\b[^>]*>", m.group(2), re.I):
            if re.search(r"\bselected\b", opt, re.I):
                val = attr(opt, "value") or ""
                break
        fields[name] = val
    for m in re.finditer(r"<textarea\b([^>]*)>(.*?)</textarea>", page, re.I | re.S):
        name = attr(m.group(1), "name")
        if name:
            fields[name] = html.unescape(m.group(2))
    return fields


def form_action(page: str, current_url: str) -> str:
    m = re.search(r"<form\b[^>]*>", page, re.I)
    act = attr(m.group(0), "action") if m else None
    return urllib.parse.urljoin(current_url, act) if act else current_url


def find_postback_link(page: str, text: str) -> str | None:
    """指定テキストを含む <a href="javascript:__doPostBack('X','')"> の X を返す"""
    for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", page, re.I | re.S):
        if text in unesc(m.group(2)):
            h = attr(m.group(1), "href") or ""
            mm = re.search(r"__doPostBack\('([^']+)'", h)
            if mm:
                return mm.group(1)
    return None


def is_logged_in(page: str) -> bool:
    return "ログアウト" in page and "fldStrPasswordWebKarte" not in page


def step_of(page: str) -> str:
    m = re.search(r"wpgWBP0(\d{4})", page)
    return m.group(0) if m else "?"


# ---------------------------------------------------------------- HTTP session

class Site:
    def __init__(self, snapshot: bool = False):
        self.cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))
        self.op.addheaders = [("User-Agent", UA), ("Accept-Language", "ja,en;q=0.8")]
        self.url = ""
        self.page = ""
        self.snapshot = snapshot
        self.n = 0

    def _save(self, tag: str) -> None:
        if not self.snapshot:
            return
        SNAP_DIR.mkdir(exist_ok=True)
        self.n += 1
        (SNAP_DIR / f"{self.n:02d}_{tag}.html").write_text(self.page, encoding="utf-8")

    def get(self, url: str, tag: str = "get") -> str:
        time.sleep(REQ_INTERVAL)
        with self.op.open(url, timeout=60) as r:
            self.url = r.geturl()
            self.page = r.read().decode("utf-8", "replace")
        self._save(tag)
        return self.page

    def post(self, data: dict[str, str], tag: str = "post", url: str | None = None) -> str:
        time.sleep(REQ_INTERVAL)
        url = url or form_action(self.page, self.url)
        body = urllib.parse.urlencode(data, encoding="utf-8").encode()
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": self.url,
            "Origin": "https://web.health-check.jp",
        })
        with self.op.open(req, timeout=60) as r:
            self.url = r.geturl()
            self.page = r.read().decode("utf-8", "replace")
        self._save(tag)
        return self.page

    def submit(self, button: str | None = None, extra: dict[str, str] | None = None,
               event_target: str | None = None, tag: str = "post") -> str:
        """現在ページのフォームを、ボタン押下 or __doPostBack 相当で送る"""
        data = form_fields(self.page)
        if extra:
            data.update(extra)
        if event_target:
            data["__EVENTTARGET"] = event_target
            data["__EVENTARGUMENT"] = ""
        if button:
            val = None
            for t in re.findall(r"<input\b[^>]*>", self.page, re.I):
                if attr(t, "name") == button:
                    val = attr(t, "value") or ""
                    break
            if val is None:
                raise RuntimeError(f"ボタンが見つからない: {button} (page={step_of(self.page)})")
            data[button] = val
        return self.post(data, tag=tag)


# ---------------------------------------------------------------- 認証情報

def keychain_password() -> str | None:
    pw = os.environ.get("HC_PASSWORD", "").strip()
    if pw:
        return pw
    try:
        return subprocess.check_output(
            ["security", "find-generic-password", "-a", LOGIN_ID, "-s", KEYCHAIN_SERVICE, "-w"],
            stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return None


def _gmail_password(addr: str) -> str | None:
    pw = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    if pw:
        return pw
    try:
        return subprocess.check_output(
            ["security", "find-generic-password", "-a", addr,
             "-s", os.environ.get("KEYCHAIN_SERVICE", "wonder-gym-mail"), "-w"],
            stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return None


def setup_password() -> int:
    """macOS のダイアログでパスワードを聞いて Keychain に保存する（画面にもログにも出さない）"""
    script = (
        'display dialog "ヘルチェック Web予約 のパスワードを入力してください\n（ID: %s）\n\nKeychain (service=%s) に保存します。" '
        'default answer "" with hidden answer with title "health-check-monitor" buttons {"キャンセル", "保存"} default button "保存"\n'
        'return text returned of result' % (LOGIN_ID, KEYCHAIN_SERVICE))
    r = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, timeout=300)
    if r.returncode != 0:
        log("キャンセルされました")
        return 1
    pw = r.stdout.decode("utf-8").rstrip("\n")
    if not pw:
        log("空のパスワードは保存しません")
        return 1
    r = subprocess.run(["/usr/bin/osascript", "-e", script.replace("を入力してください", "をもう一度入力してください（確認）")],
                       capture_output=True, timeout=300)
    if r.returncode != 0 or r.stdout.decode("utf-8").rstrip("\n") != pw:
        log("2回の入力が一致しません。保存しませんでした")
        return 1
    r2 = subprocess.run(["security", "add-generic-password", "-a", LOGIN_ID, "-s", KEYCHAIN_SERVICE,
                         "-w", pw, "-U"], capture_output=True)
    if r2.returncode != 0:
        log(f"Keychain 保存失敗: {r2.stderr.decode(errors='replace').strip()}")
        return 1
    log(f"Keychain に保存しました (account={LOGIN_ID}, service={KEYCHAIN_SERVICE})")
    return 0


# ---------------------------------------------------------------- 通知

def notify_mail(subject: str, body: str) -> str:
    addr = os.environ.get("MAIL_FROM", "").strip()
    to = [x.strip() for x in os.environ.get("MAIL_TO", addr).split(",") if x.strip()]
    if not addr or not to:
        return "mail: MAIL_FROM/MAIL_TO 未設定のためスキップ"
    pw = _gmail_password(addr)
    if not pw:
        return "mail: 認証情報なしのためスキップ"
    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = addr
    msg["To"] = ", ".join(to)
    msg["Date"] = formatdate(localtime=True)
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
            s.starttls()
            s.login(addr, pw)
            s.sendmail(addr, to, msg.as_string())
        return f"mail: OK -> {', '.join(to)}"
    except Exception as e:
        return f"mail: 送信失敗: {e}"


def notify_ntfy(title: str, body: str, urgent: bool) -> str:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return "ntfy: 未設定"
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
        headers={"Title": title.encode("utf-8").decode("latin-1", "ignore") or "health-check",
                 "Priority": "urgent" if urgent else "default", "Tags": "hospital",
                 # 通知をタップ → そのまま予約サイトのログイン画面へ
                 "Click": URL_LOGIN,
                 "Actions": f"view, Open reserve site, {URL_LOGIN}, clear=true"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return f"ntfy: OK ({r.status})"
    except Exception as e:
        return f"ntfy: 失敗: {e}"


def notify(title: str, body: str, *, urgent: bool = True, dry_run: bool = False) -> None:
    if dry_run:
        log(f"[dry-run] 通知抑止: {title}\n{body}")
        return
    for r in (notify_ntfy(title, body, urgent), notify_mail(title, body)):
        log(r)


# ---------------------------------------------------------------- state

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(st: dict) -> None:
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def append_history(rec: dict) -> None:
    rec = {"ts": dt.datetime.now().isoformat(timespec="seconds"), **rec}
    with HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- サイト操作

class AuthError(RuntimeError):
    """ID/パスワードの問題。ネットワーク要因とは区別する"""


def login(site: Site) -> None:
    site.get(URL_LOGIN, "login")
    if is_logged_in(site.page):
        return
    if not LOGIN_ID:
        raise AuthError("ログインID未設定: HC_LOGIN_ID か login_id.txt を用意してください")
    pw = keychain_password()
    if not pw:
        raise AuthError("パスワード未登録: python3 monitor.py --setup で登録してください")
    site.submit("cmdLoginWebKarte", {"fldIdWebKarte": LOGIN_ID, "fldStrPasswordWebKarte": pw}, tag="after_login")
    if not is_logged_in(site.page):
        m = re.search(r'class="[^"]*(?:error|msg)[^"]*"[^>]*>(.*?)<', site.page, re.I | re.S)
        raise AuthError("ログイン失敗" + (f": {unesc(m.group(1))}" if m else ""))


def current_reservation(site: Site) -> dict:
    """Default → 予約状況を確認する → wpgWBP01010 の予約内容を返す"""
    if "cmdWebReserve" not in site.page:
        site.get(URL_TOP, "top")
    site.submit("ctl00$ContentPlaceHolder1$cmdWebReserve", tag="01010")
    p = site.page
    info = {}
    for key, span in (("Web予約番号", "lblNcWebReserve"), ("受診施設", "lblNmOfc"),
                      ("ご予約日", "lblDtSchedule"), ("開始時刻", "lblTmStart")):
        m = re.search(r'id="ctl00_ContentPlaceHolder1_%s"[^>]*>(.*?)</span>' % span, p, re.S)
        if m:
            info[key] = re.sub(r"\s+", " ", unesc(m.group(1)))
    m = re.search(r"申込内容／当日のお支払い\s*</th>\s*<td[^>]*>(.*?)</td>", p, re.S)
    if m:
        info["申込内容"] = re.sub(r"\s+", " ", unesc(m.group(1)))
    m = re.search(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日", info.get("ご予約日", ""))
    if m:
        info["date"] = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    return info


def open_calendar(site: Site) -> None:
    """wpgWBP01010 → 予約変更 → セデーション✓ → STEP4 カレンダー"""
    site.submit("ctl00$ContentPlaceHolder1$cmdReserveChange", tag="02141")
    if "wpgWBP02141" not in site.url:
        raise RuntimeError(f"予約変更ページに到達できない: {site.url}")
    cb = None
    for m in re.finditer(r"<tr\b[^>]*>(.*?)</tr>", site.page, re.S | re.I):
        row = m.group(1)
        if SEDATION_LABEL in row:
            t = re.search(r'<input\b[^>]*type="checkbox"[^>]*>', row, re.I)
            if t:
                cb = attr(t.group(0), "name")
                break
    if not cb:
        raise RuntimeError("セデーションのチェックボックスが見つからない")
    site.submit("ctl00$ContentPlaceHolder1$cmdNext", {cb: "on"}, tag="02151")
    if "wpgWBP02151" not in site.url or SEDATION_LABEL not in site.page:
        raise RuntimeError(f"STEP4 に到達できない/セデーション未反映: {site.url}")


def parse_calendar(page: str) -> list[dict]:
    """STEP4 の全セル → [{date, ampm, cdofc, mark, target}]"""
    cells = []
    for m in re.finditer(r"<a\b([^>]*\blinktype=\"select_schedule\"[^>]*)>(.*?)</a>", page, re.S | re.I):
        tag, inner = m.group(1), m.group(2)
        d = attr(tag, "dtschedule")
        if not d or len(d) != 8:
            continue
        href = attr(tag, "href") or ""
        mm = re.search(r"__doPostBack\('([^']+)'", href)
        cells.append({
            "date": dt.date(int(d[:4]), int(d[4:6]), int(d[6:])),
            "ampm": (attr(tag, "ampm") or "").upper(),
            "cdofc": attr(tag, "cdofc") or "",
            "mark": unesc(inner),
            "target": mm.group(1) if mm else None,
        })
    return cells


def slot_ok(c: dict, today: dt.date) -> bool:
    d = c["date"]
    return (c["cdofc"] in CENTER_PRIORITY and c["ampm"] in ALLOW_AMPM
            and today + dt.timedelta(days=MIN_LEAD_DAYS) <= d <= DEADLINE
            and d.weekday() in ALLOW_WEEKDAYS and d not in EXCLUDE_DATES)


def rank(c: dict) -> tuple:
    return (CENTER_PRIORITY[c["cdofc"]], c["date"], 0 if c["ampm"] == "AM" else 1)


def scan_weeks(site: Site, today: dt.date, stop_at: dt.date | None = None,
               on_page=None) -> tuple[list[dict], int]:
    """STEP4 を「前週」で遡り、全ページの ○ を集める。
    stop_at を渡すとその日付を含むページで止まる（そのページが site.page に残る）。
    on_page(cells) を渡すと各ページで呼ぶ。True を返したら走査をそこで打ち切る
    （そのページで予約を試みる用。○は数十秒で消えるので後戻りしない）。
    戻り値: (○セル一覧, 見たページ数)"""
    opens: list[dict] = []
    pages = 0
    seen_first: set = set()
    while True:
        cells = parse_calendar(site.page)
        pages += 1
        if not cells:
            raise RuntimeError("カレンダーが読めない")
        dates = sorted({c["date"] for c in cells})
        first, last = dates[0], dates[-1]
        if first in seen_first:
            break  # 遷移していない
        seen_first.add(first)
        for c in cells:
            c["page"] = pages - 1  # STEP4 の初期ページから「前週」を何回押した位置か
        opens.extend(c for c in cells if c["mark"] == "○")
        if on_page and on_page(cells):
            break
        if stop_at and first <= stop_at <= last:
            break
        if first <= today:
            break
        prev = find_postback_link(site.page, "前週")
        if not prev:
            break
        site.submit(event_target=prev, tag=f"week_{first:%m%d}")
    return opens, pages


def reserve(site: Site, slot: dict, dry_run: bool) -> dict:
    """slot の ○ をクリック → 時間選択 → STEP5 → 6 → 7 → 完了。
    site.page は slot の日付を含む STEP4 ページであること。"""
    cells = parse_calendar(site.page)
    cur = next((c for c in cells if c["date"] == slot["date"] and c["cdofc"] == slot["cdofc"]
                and c["ampm"] == slot["ampm"]), None)
    if not cur or cur["mark"] != "○" or not cur["target"]:
        return {"ok": False, "reason": f"再確認時に○ではなかった (mark={cur and cur['mark']})"}
    site.submit(event_target=cur["target"], tag="pick_day")
    radios = []
    for t in re.findall(r"<input\b[^>]*type=\"radio\"[^>]*>", site.page, re.I):
        name, val, rid = attr(t, "name"), attr(t, "value"), attr(t, "id")
        if not name or "reserve_time" not in name:
            continue
        lab = ""
        if rid:
            lm = re.search(r'<label\b[^>]*for="%s"[^>]*>(.*?)</label>' % re.escape(rid), site.page, re.S | re.I)
            if lm:
                lab = unesc(lm.group(1))
        if not lab:
            i = site.page.find(t)
            lab = unesc(site.page[i + len(t): i + len(t) + 200]).split("\n")[0][:10]
        radios.append((name, val, lab))
    if not radios:
        return {"ok": False, "reason": "時間の選択肢が出なかった（直前に埋まった可能性）"}
    def tkey(r):
        m = re.search(r"(\d{1,2}):(\d{2})", r[2])
        return (int(m.group(1)) * 60 + int(m.group(2))) if m else 9999
    radios.sort(key=tkey, reverse=(TIME_PREF == "latest"))
    name, val, lab = radios[0]
    log(f"時間候補: {[r[2] for r in radios]} → {lab}")
    if dry_run:
        return {"ok": False, "reason": "dry-run（ここで停止）", "time": lab, "times": [r[2] for r in radios]}
    site.submit("ctl00$ContentPlaceHolder1$cmdNext", {name: val}, tag="step5")
    if "wpgWBP02161" not in site.url:
        return {"ok": False, "reason": f"STEP5 に進めない: {site.url}"}
    if SEDATION_LABEL not in site.page:
        return {"ok": False, "reason": "STEP5 にセデーションが載っていない"}
    site.submit("ctl00$ContentPlaceHolder1$cmdNext", tag="step6")
    if "wpgWBP02171" not in site.url:
        return {"ok": False, "reason": f"STEP6 に進めない: {site.url}"}
    site.submit("ctl00$ContentPlaceHolder1$cmdNext", tag="step7")
    if "wpgWBP02181" not in site.url:
        return {"ok": False, "reason": f"STEP7 に進めない: {site.url}"}
    p = site.page
    name_ok = CENTER_NAME[slot["cdofc"]].replace("センター", "") in p
    date_ok = re.search(r"%d年\s*%d月\s*%d日" % (slot["date"].year, slot["date"].month, slot["date"].day), p)
    if not (name_ok and date_ok and SEDATION_LABEL in p):
        return {"ok": False, "reason": f"STEP7 の内容が想定と違う (center={name_ok}, date={bool(date_ok)})"}
    site.submit("ctl00$ContentPlaceHolder1$cmdNext", tag="step8")
    final_url = site.url
    m = re.search(r"Web予約番号.*?<td[^>]*>(.*?)</td>", site.page, re.S)
    reserve_no = unesc(m.group(1)) if m else None
    # 完了画面の文言に頼らず、予約状況ページを引き直して実際に変わったかで判定する
    try:
        site.get(URL_TOP, "verify_top")
        cur = current_reservation(site)
    except Exception as e:
        return {"ok": False, "reason": f"完了後の確認に失敗（予約は変わっている可能性あり）: {e}",
                "time": lab, "reserve_no": reserve_no, "final_url": final_url}
    done = (cur.get("date") == slot["date"].isoformat()
            and CENTER_NAME[slot["cdofc"]] in cur.get("受診施設", "")
            and SEDATION_LABEL in cur.get("申込内容", ""))
    return {"ok": done,
            "reason": "" if done else f"予約状況ページが変わっていない: {cur}",
            "time": cur.get("開始時刻") or lab, "reserve_no": cur.get("Web予約番号") or reserve_no,
            "final_url": final_url, "after": cur}


def nav_text(slots: list[dict]) -> str:
    """通知に載せる手動手順（短く）"""
    c = slots[0]
    n = c.get("page") or 0
    back = f"「前週」を{n}回 → " if n else ""
    return "\n".join([
        f"手動なら → {URL_LOGIN}",
        "予約変更 → 鎮静剤にチェック → 次へ",
        f"{back}{fmt_short(c)} の○ → 時間を選ぶ → 次へ×3 → 予約する",
    ])


def fmt_short(c: dict) -> str:
    """通知用の短い表記: 渋谷アクシュ 11/20(金) AM"""
    wd = "月火水木金土日"[c["date"].weekday()]
    return f"{CENTER_NAME.get(c['cdofc'], c['cdofc'])} {c['date'].month}/{c['date'].day}({wd}) {c['ampm']}"


def fmt_slot(c: dict) -> str:
    wd = "月火水木金土日"[c["date"].weekday()]
    return f"{CENTER_NAME.get(c['cdofc'], c['cdofc'])} {c['date']:%Y-%m-%d}({wd}) {c['ampm']}"


# ---------------------------------------------------------------- main

def run(args) -> int:
    today = dt.date.today()
    st = load_state()
    if st.get("done") and not args.force:
        log(f"完了済み（{st['done'].get('slot')}）。何もしない")
        return 0
    if today > DEADLINE:
        log("期限超過。何もしない")
        return 0

    site = Site(snapshot=args.snapshot)
    try:
        login(site)
    except AuthError as e:
        log(f"ログインエラー: {e}")
        n = st.get("login_fail", 0) + 1
        st["login_fail"] = n
        if n in (1, 6, 36):  # 初回・約1時間後・約6時間後に知らせる
            notify("【ヘルチェック監視】ログインできません", f"{e}\n\n連続失敗 {n} 回。パスワード登録/変更を確認してください。", dry_run=args.dry_run)
        save_state(st)
        return 1
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        # ネットワーク要因（スリープ復帰直後の DNS 失敗、サーバ遅延など）。認証失敗とは別枠で数える
        n = st.get("net_fail", 0) + 1
        st["net_fail"] = n
        log(f"通信エラー（{n}回目）: {e}")
        if n == 18:  # 起きている間ずっと失敗が続く場合だけ知らせる（10分×18=約3時間）
            notify("【ヘルチェック監視】サイトに繋がりません", f"{e}\n\n連続 {n} 回。回線やサイト側の状況を確認してください。", urgent=False, dry_run=args.dry_run)
        save_state(st)
        return 1
    st["login_fail"] = 0
    st["net_fail"] = 0

    cur = current_reservation(site)
    log(f"現在の予約: {cur}")
    st["current"] = cur
    cur_date = dt.date.fromisoformat(cur["date"]) if cur.get("date") else None
    if cur_date and cur_date <= DEADLINE and any(n in cur.get("受診施設", "") for n in CENTER_NAME.values()) \
            and SEDATION_LABEL in cur.get("申込内容", ""):
        log("既に目標を満たす予約になっている → 完了扱い")
        st["done"] = {"slot": f"{cur.get('受診施設')} {cur.get('ご予約日')}", "ts": dt.datetime.now().isoformat(timespec="seconds"), "by": "already"}
        save_state(st)
        return 0

    open_calendar(site)
    auto = AUTO_RESERVE and not args.no_reserve
    attempt: dict = {}   # ページ上で即予約を試みた結果（1回だけ）

    def on_page(cells: list[dict]) -> bool:
        # 2026-09-18: 全ページ走査→フローやり直しで43秒かかり、その間に○が×になった。
        # 以後は「○を見つけたページでその場で押す」。同一ページ内だけ優先順位で選ぶ。
        if not auto or attempt.get("res"):
            return False
        here = sorted((c for c in cells if c["mark"] == "○" and slot_ok(c, today)), key=rank)
        if not here:
            return False
        append_history({"event": "found", "slots": [fmt_slot(c) for c in here]})
        for best in here:  # 同ページ内は優先順に、消えていたら次へ
            log(f"自動予約（その場で）: {fmt_slot(best)}  同ページ候補={[fmt_slot(c) for c in here]}")
            res = reserve(site, best, dry_run=args.dry_run)
            append_history({"event": "reserve", "slot": fmt_slot(best), **{k: v for k, v in res.items() if k not in ("final_url", "after")}})
            still_calendar = bool(parse_calendar(site.page))
            if not res.get("ok") and still_calendar and not args.dry_run:
                # ○が消えていた/時間候補なし → まだ STEP4 にいるので次の候補・次のページへ
                log(f"取れず（{res.get('reason')}）→ 続行")
                attempt["missed"] = attempt.get("missed", []) + [fmt_slot(best)]
                continue
            attempt.update({"slot": best, "res": res, "cands": here})
            return True
        return False

    opens, pages = scan_weeks(site, today, on_page=on_page)
    cands = sorted((c for c in opens if slot_ok(c, today)), key=rank)
    others = [c for c in opens if not slot_ok(c, today)]
    log(f"{pages}ページ走査 / ○ 合計 {len(opens)} / 条件合致 {len(cands)}")
    for c in cands:
        log(f"  候補: {fmt_slot(c)}")
    for c in others:
        log(f"  対象外○: {fmt_slot(c)}")
    st["last_check"] = dt.datetime.now().isoformat(timespec="seconds")
    st["last_open"] = [fmt_slot(c) for c in opens]

    if attempt.get("res"):
        best, res = attempt["slot"], attempt["res"]
        if res.get("ok"):
            st["done"] = {"slot": fmt_slot(best), "time": res.get("time"), "reserve_no": res.get("reserve_no"),
                          "ts": dt.datetime.now().isoformat(timespec="seconds"), "by": "auto"}
            save_state(st)
            notify("【ヘルチェック】予約変更 完了 " + fmt_short(best),
                   f"{fmt_short(best)} {res.get('time') or ''}\n人間ドックC＋鎮静剤\n元の {cur.get('ご予約日', '')} は置き換わりました。\n確認 → {URL_LOGIN}",
                   dry_run=args.dry_run)
            return 0
        log(f"予約失敗: {res}")
        save_state(st)
        if not args.dry_run:
            key = fmt_slot(best) + "|fail"
            if st.get("notified_key") != key:
                notify("【ヘルチェック】空きあり・自動予約に失敗 " + fmt_short(best),
                       "空き: " + " / ".join(fmt_short(c) for c in cands) + f"\n自動予約は失敗（{res.get('reason')}）\n\n" + nav_text(cands),
                       dry_run=args.dry_run)
                st["notified_key"] = key
                save_state(st)
        return 2

    if not cands:
        save_state(st)
        return 0

    # ここに来るのは: 自動予約OFF、または ○を押した直後に消えていた（missed）
    best = cands[0]
    missed = attempt.get("missed", [])
    if missed:
        log(f"○は出たが押す前に消えた: {missed}")
        key = "missed|" + ",".join(missed)
        if st.get("notified_key") != key and not args.dry_run:
            notify("【ヘルチェック】空き出現→取れず " + fmt_short(best),
                   "空き: " + " / ".join(fmt_short(c) for c in cands) + "\n押す前に埋まりました。まだ空いていれば手動で。\n\n" + nav_text(cands),
                   urgent=False, dry_run=args.dry_run)
            st["notified_key"] = key
        save_state(st)
        return 2
    append_history({"event": "found", "slots": [fmt_slot(c) for c in cands]})
    key = fmt_slot(best)
    if st.get("notified_key") != key:
        body = "空き: " + " / ".join(fmt_short(c) for c in cands) + "\n（自動予約OFF）\n\n" + nav_text(cands)
        notify("【ヘルチェック】空きあり " + fmt_short(best), body, dry_run=args.dry_run)
        st["notified_key"] = key
    save_state(st)
    return 0


def test_reserve(args) -> int:
    c, d, ampm = args.test_reserve.split(":")
    slot = {"cdofc": c, "date": dt.date(int(d[:4]), int(d[4:6]), int(d[6:])), "ampm": ampm.upper()}
    if not args.dry_run:
        log("--test-reserve は --dry-run 必須（本予約は run() 経由のみ）")
        return 1
    site = Site(snapshot=args.snapshot)
    login(site)
    log(f"現在の予約: {current_reservation(site)}")
    open_calendar(site)
    scan_weeks(site, dt.date.today(), stop_at=slot["date"])
    res = reserve(site, slot, dry_run=True)
    log(f"結果: {res}")
    return 0 if res.get("times") else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="通知を送らず、予約は時間選択の手前で止める")
    ap.add_argument("--no-reserve", action="store_true", help="この実行では自動予約しない（通知のみ）")
    ap.add_argument("--snapshot", action="store_true", help="各ページの HTML を snapshots/ に保存")
    ap.add_argument("--force", action="store_true", help="done 済みでも走らせる")
    ap.add_argument("--test-mail", action="store_true")
    ap.add_argument("--setup", action="store_true", help="パスワードをダイアログで聞いて Keychain に保存")
    ap.add_argument("--test-reserve", metavar="CDOFC:YYYYMMDD:AMPM",
                    help="指定枠の○を押して時間候補まで確認する（--dry-run と併用で予約はしない）")
    args = ap.parse_args()
    if args.test_reserve:
        return test_reserve(args)
    if args.setup:
        return setup_password()
    if args.test_mail:
        sample = [{"cdofc": "001012", "date": dt.date(2026, 11, 20), "ampm": "AM", "page": 5}]
        notify("【ヘルチェック】テスト 渋谷アクシュ 11/20(金) AM",
               "空き: 渋谷アクシュ 11/20(金) AM\n（テスト通知。本番はこの形式）\n\n" + nav_text(sample), urgent=False)
        return 0
    with LOCK_PATH.open("w") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log("前回実行がまだ動いている。スキップ")
            return 0
        # HC_LOOP_SECONDS>0 なら、その秒数のあいだ巡回を繰り返す
        # （GitHub Actions は cron が5分刻みなので、1ジョブ内で回して実質1〜2分間隔にする）
        budget = float(os.environ.get("HC_LOOP_SECONDS", "0") or 0)
        pause = float(os.environ.get("HC_LOOP_INTERVAL", "20") or 0)
        started = time.monotonic()
        rc = 0
        while True:
            try:
                rc = run(args)
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                log(f"通信エラー（巡回中）: {e!r}")
                rc = 1
            except Exception as e:
                log(f"ERROR: {e!r}")
                rc = 1
            if budget <= 0 or load_state().get("done"):
                return rc
            elapsed = time.monotonic() - started
            if elapsed + pause + 60 > budget:  # 次の1周が収まらないなら終える
                return rc
            time.sleep(pause)


if __name__ == "__main__":
    sys.exit(main())
