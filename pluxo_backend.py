"""
PLUXO API + Telegram admin bot. Stock and balances are shared with the Pluxo HTML app.

Set TELEGRAM_BOT_TOKEN and OWNER_TELEGRAM_ID in .env (never put tokens in HTML).
Only one host/process may poll Telegram per bot token. Extra Gunicorn workers: PLUXO_TELEGRAM_POLL=never.

Railway: use requirements.txt + railway.json (or Procfile). Gunicorn listens on $PORT with
--workers 1. Copy env.example → project variables; mount a volume on /app/data for persistent
state.json (otherwise data resets on redeploy).
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import random
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from flask import Flask, Response, abort, jsonify, request, send_file
from flask_cors import CORS
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
SHOP_PRODUCTS_JSON = ROOT_DIR / "shop_products.json"


def resolve_index_html() -> Path | None:
    """Find the main Pluxo HTML file next to this script (handles odd names like 'index (27).html')."""
    root = ROOT_DIR
    for name in ("index.html", "index (27).html"):
        p = root / name
        if p.is_file():
            return p
    for p in sorted(root.glob("index*.html")):
        if p.is_file():
            return p
    for p in sorted(root.glob("*.html")):
        if p.is_file():
            return p
    return None
STATE_PATH = DATA_DIR / "state.json"
SHOP_STOCK_DOCUMENT_PATH = DATA_DIR / "shop_stock.txt"
WEBHOOK_SECRET = os.environ.get("PLUXO_WEBHOOK_SECRET", "pluxo_secret_2024")
AUTH_SECRET_KEY = os.environ.get("AUTH_SECRET_KEY", WEBHOOK_SECRET + "-pluxo-auth-v2")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
# GOATYS.CC Telegram folder (add-list); override via env on deploy if the link rotates.
_GOATYS_TG_CHAT_LIST_RAW = os.environ.get(
    "GOATYS_TELEGRAM_CHAT_LIST_URL",
    "https://t.me/addlist/um9UwF7A0Qk2MWVh",
).strip()
GOATYS_TELEGRAM_CHAT_LIST_URL = _GOATYS_TG_CHAT_LIST_RAW or "https://t.me/addlist/um9UwF7A0Qk2MWVh"
OWNER_RAW = os.environ.get("OWNER_TELEGRAM_ID", "").strip()
# HTTP timeouts for python-telegram-bot (default PTB timeouts are ~5s; slow networks hit TimedOut).
def _telegram_http_timeouts() -> tuple[float, float]:
    """Returns (socket timeouts, pool_timeout)."""
    try:
        sec = float(os.environ.get("TELEGRAM_HTTP_TIMEOUT", "45").strip())
    except ValueError:
        sec = 45.0
    sec = max(15.0, min(120.0, sec))
    pool = max(10.0, min(45.0, sec))
    return sec, pool
# If "1"/"true"/"yes": do not start Telegram polling (API only). Use when Railway or another PC runs the bot.
_DISABLE = os.environ.get("DISABLE_TELEGRAM_BOT", "").strip().lower()
TELEGRAM_BOT_DISABLED = _DISABLE in ("1", "true", "yes", "on")

try:
    STOCK_BATCH_MAX = int(os.environ.get("STOCK_BATCH_MAX", "5000").strip())
except ValueError:
    STOCK_BATCH_MAX = 5000
STOCK_BATCH_MAX = max(1, min(10000, STOCK_BATCH_MAX))

# Telegram multi-message /stock … /done batch upload (context.user_data + state.json).
STOCK_UPLOAD_SESSION_KEY = "pluxo_stock_batch"
STOCK_UPLOAD_SESSION_TTL_SEC = 6 * 3600  # drop abandoned uploads after 6h

# Shop bases (internal id → customer-facing label on site / Telegram).
STOCK_BASE_CATALOG: tuple[dict[str, str], ...] = (
    {"id": "BLACKJACK_BASE", "label": "BLACKJACK🃏"},
    {"id": "MONEYJR_BASE", "label": "MoneyJR Base"},
    {"id": "UHQ_USA_BASE", "label": "UHQ USA 🔥"},
    {"id": "FOREIGN_RICH_FUCKERS_BASE", "label": "FOREIGN Rich Fuckers👲"},
)
VALID_STOCK_BASES: frozenset[str] = frozenset(b["id"] for b in STOCK_BASE_CATALOG)
STOCK_BASE_LABEL_BY_ID: dict[str, str] = {b["id"]: b["label"] for b in STOCK_BASE_CATALOG}
DEFAULT_STOCK_BASE_ID = "BLACKJACK_BASE"

# Older rows may still carry labels like "2026_US_Base"; sales roll up here for reporting.
SOLD_STOCK_FALLBACK_BUCKET = "UNASSIGNED"

# Legacy custom admin bases (deprecated — catalog bases only).
ADMIN_STOCK_BASE_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,31}$")


def stock_base_label(base_id: str) -> str:
    b = str(base_id or "").strip().upper()
    return STOCK_BASE_LABEL_BY_ID.get(b, str(base_id or "").strip() or b)


def default_stock_base_id() -> str:
    return DEFAULT_STOCK_BASE_ID
_STOCK_COUNTRY_ALIASES = {"UK": "GB"}

FRONTEND_COUNTRY_PRESETS: dict[str, dict[str, str]] = {
    "US": {"flag": "🇺🇸", "flagClass": "fi-us", "code": "US", "name": "United States"},
    "CA": {"flag": "🇨🇦", "flagClass": "fi-ca", "code": "CA", "name": "Canada"},
    "GB": {"flag": "🇬🇧", "flagClass": "fi-gb", "code": "GB", "name": "United Kingdom"},
    "UK": {"flag": "🇬🇧", "flagClass": "fi-gb", "code": "GB", "name": "United Kingdom"},
    "AU": {"flag": "🇦🇺", "flagClass": "fi-au", "code": "AU", "name": "Australia"},
    "DE": {"flag": "🇩🇪", "flagClass": "fi-de", "code": "DE", "name": "Germany"},
    "FR": {"flag": "🇫🇷", "flagClass": "fi-fr", "code": "FR", "name": "France"},
    "IT": {"flag": "🇮🇹", "flagClass": "fi-it", "code": "IT", "name": "Italy"},
    "ES": {"flag": "🇪🇸", "flagClass": "fi-es", "code": "ES", "name": "Spain"},
    "NL": {"flag": "🇳🇱", "flagClass": "fi-nl", "code": "NL", "name": "Netherlands"},
    "BE": {"flag": "🇧🇪", "flagClass": "fi-be", "code": "BE", "name": "Belgium"},
    "AT": {"flag": "🇦🇹", "flagClass": "fi-at", "code": "AT", "name": "Austria"},
    "CH": {"flag": "🇨🇭", "flagClass": "fi-ch", "code": "CH", "name": "Switzerland"},
    "IE": {"flag": "🇮🇪", "flagClass": "fi-ie", "code": "IE", "name": "Ireland"},
    "PT": {"flag": "🇵🇹", "flagClass": "fi-pt", "code": "PT", "name": "Portugal"},
    "PL": {"flag": "🇵🇱", "flagClass": "fi-pl", "code": "PL", "name": "Poland"},
    "SE": {"flag": "🇸🇪", "flagClass": "fi-se", "code": "SE", "name": "Sweden"},
    "NO": {"flag": "🇳🇴", "flagClass": "fi-no", "code": "NO", "name": "Norway"},
    "DK": {"flag": "🇩🇰", "flagClass": "fi-dk", "code": "DK", "name": "Denmark"},
    "FI": {"flag": "🇫🇮", "flagClass": "fi-fi", "code": "FI", "name": "Finland"},
    "NZ": {"flag": "🇳🇿", "flagClass": "fi-nz", "code": "NZ", "name": "New Zealand"},
    "JP": {"flag": "🇯🇵", "flagClass": "fi-jp", "code": "JP", "name": "Japan"},
    "BR": {"flag": "🇧🇷", "flagClass": "fi-br", "code": "BR", "name": "Brazil"},
    "MX": {"flag": "🇲🇽", "flagClass": "fi-mx", "code": "MX", "name": "Mexico"},
    "IN": {"flag": "🇮🇳", "flagClass": "fi-in", "code": "IN", "name": "India"},
}


def _site_owner_username_norm() -> str | None:
    raw = (
        os.environ.get("PLUXO_SITE_OWNER_USERNAME")
        or os.environ.get("SITE_OWNER_USERNAME")
        or ""
    ).strip()
    return norm_user(raw) if raw else None


def can_skip_custom_stock_base(username: str) -> bool:
    """Site owner may pick any catalog base on web upload without registering a custom base."""
    so = _site_owner_username_norm()
    return bool(so and norm_user(username) == so)


def normalize_stock_upload_country(code: str) -> str:
    c_orig = (code or "").strip().upper()
    c = _STOCK_COUNTRY_ALIASES.get(c_orig, c_orig)
    if c == "UK":
        c = "GB"
    return c if c in FRONTEND_COUNTRY_PRESETS else "US"


def all_known_stock_bases_unlocked() -> set[str]:
    """Call with state_lock held. Only GOATYS catalog bases (no legacy MONEY_BASE / TONY_BASE)."""
    return set(VALID_STOCK_BASES)

try:
    TOPUP_SUBMIT_MAX_HOURLY = int(os.environ.get("TOPUP_SUBMIT_MAX_HOURLY", "8").strip())
except ValueError:
    TOPUP_SUBMIT_MAX_HOURLY = 8
TOPUP_SUBMIT_MAX_HOURLY = max(1, min(50, TOPUP_SUBMIT_MAX_HOURLY))

state_lock = threading.Lock()
state: dict[str, Any] = {}

_topup_submit_lock = threading.Lock()
_topup_submit_timestamps: dict[str, list[float]] = {}

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})


def _default_state() -> dict[str, Any]:
    oid: int | None = None
    if OWNER_RAW.isdigit():
        oid = int(OWNER_RAW)
    admins: list[int] = []
    if oid is not None:
        admins.append(oid)
    extra = os.environ.get("ADMIN_TELEGRAM_IDS", "")
    for part in extra.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            i = int(part)
            if i not in admins:
                admins.append(i)
    return {
        "users": {},
        "stock": [],
        "next_product_id": 1,
        "owner_telegram_id": oid,
        "admin_telegram_ids": admins,
        "site_admin_usernames": [],
        "dice": {"bets": [], "history": []},
        "blackjack": {"matches": [], "history": []},
        "action_logs": [],
        "purchase_log": [],
        "lockdown": False,
        "crypto_topups": {},
        "support_tickets": {},
        "leads": [],
        "next_lead_id": 1,
        "sold_stock_daily": {},
        "sold_stock_recent": [],
        "admin_stock_bases": {},
        "telegram_stock_sessions": {},
    }


def load_state() -> None:
    global state
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if STATE_PATH.exists():
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
    else:
        state = _default_state()
        save_state()
        return
    # Merge env owner into loaded state
    if OWNER_RAW.isdigit():
        oid = int(OWNER_RAW)
        state["owner_telegram_id"] = oid
        lst = state.setdefault("admin_telegram_ids", [])
        if oid not in lst:
            lst.append(oid)

    state.setdefault("site_admin_usernames", [])

    # Env site admins for the web panel
    sad = os.environ.get("SITE_ADMIN_USERNAMES", "")
    if sad.strip():
        lst = state["site_admin_usernames"]
        for part in sad.replace(";", ",").split(","):
            u = norm_user(part)
            if u and u not in lst:
                lst.append(u)
        save_state()

    state.setdefault("action_logs", [])
    state.setdefault("purchase_log", [])
    state.setdefault("lockdown", False)
    state.setdefault("crypto_topups", {})
    state.setdefault("support_tickets", {})
    state.setdefault("leads", [])
    state.setdefault("next_lead_id", 1)
    state.setdefault("sold_stock_daily", {})
    state.setdefault("sold_stock_recent", [])
    state.setdefault("admin_stock_bases", {})
    state.setdefault("telegram_stock_sessions", {})
    # Drop legacy custom bases; shop uses STOCK_BASE_CATALOG only.
    if state.get("admin_stock_bases"):
        state["admin_stock_bases"] = {}
    # Remove legacy MONEY_BASE / TONY_BASE rows from shop inventory.
    stock = state.get("stock") or []
    cleaned = [
        s
        for s in stock
        if isinstance(s, dict) and str(s.get("base") or "") in VALID_STOCK_BASES
    ]
    if len(cleaned) != len(stock):
        state["stock"] = cleaned
        save_state()


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today_utc_date_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _sold_stock_bucket_for_base_unlocked(base_val: str | None) -> str:
    """Call only with state_lock held (or single-threaded test)."""
    b = str(base_val or "").strip()
    if not b:
        return SOLD_STOCK_FALLBACK_BUCKET
    if b in VALID_STOCK_BASES:
        return b
    if b in all_known_stock_bases_unlocked():
        return b
    if ADMIN_STOCK_BASE_NAME_RE.match(b):
        return b
    return SOLD_STOCK_FALLBACK_BUCKET


def _record_sold_stock_unlocked(
    base_val: str | None, price: float, username: str, product_id: int | None
) -> None:
    """Track card sales by base for Telegram /soldstock (UTC day buckets)."""
    day = _today_utc_date_str()
    b = _sold_stock_bucket_for_base_unlocked(base_val)
    daily = state.setdefault("sold_stock_daily", {})
    day_bucket = daily.setdefault(day, {})
    rec = day_bucket.setdefault(b, {"count": 0, "revenue": 0.0})
    rec["count"] = int(rec["count"]) + 1
    rec["revenue"] = round(float(rec["revenue"]) + float(price), 2)
    recent = state.setdefault("sold_stock_recent", [])
    recent.insert(
        0,
        {
            "t": _utc_now_z(),
            "day": day,
            "base": b,
            "price": round(float(price), 2),
            "buyer": norm_user(username),
            "product_id": int(product_id) if product_id is not None else None,
        },
    )
    if len(recent) > 500:
        del recent[500:]
    if len(daily) > 120:
        for old in sorted(daily.keys())[:-120]:
            del daily[old]


def _action_log_unlocked(line: str, uid: int | None = None) -> None:
    """Append one log row while holding ``state_lock``."""
    logs = state.setdefault("action_logs", [])
    row: dict[str, Any] = {"t": _utc_now_z(), "line": line[:800]}
    if uid is not None:
        row["uid"] = uid
    logs.insert(0, row)
    if len(logs) > 400:
        del logs[400:]


def save_state() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    tmp.replace(STATE_PATH)


def _refresh_state_keys_from_disk(*keys: str) -> None:
    """Merge selected keys from state.json into RAM (web worker vs Telegram worker)."""
    if not keys or not STATE_PATH.exists():
        return
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            disk = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[state] refresh keys {keys!r}: {exc!r}", flush=True)
        return
    if not isinstance(disk, dict):
        return
    for k in keys:
        if k not in disk:
            continue
        if k == "crypto_topups":
            ram = state.get("crypto_topups")
            dsk = disk.get("crypto_topups")
            if not isinstance(ram, dict):
                ram = {}
            if not isinstance(dsk, dict):
                dsk = {}
            state["crypto_topups"] = {**ram, **dsk}
        elif k == "users":
            ram = state.get("users")
            dsk = disk.get("users")
            if not isinstance(ram, dict):
                ram = {}
            if not isinstance(dsk, dict):
                dsk = {}
            merged_users = dict(ram)
            for uname, row in dsk.items():
                if isinstance(row, dict) and isinstance(merged_users.get(uname), dict):
                    merged_users[uname] = {**merged_users[uname], **row}
                else:
                    merged_users[uname] = row
            state["users"] = merged_users
        else:
            state[k] = disk[k]


def _reload_crypto_topups_from_disk() -> None:
    """Load crypto_topups (+ users) from state.json (web Gunicorn worker vs Telegram poll leader)."""
    with state_lock:
        if not STATE_PATH.exists():
            return
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                disk = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[topup] reload from disk: {exc!r}", flush=True)
            return
        if not isinstance(disk, dict):
            return
        dsk_top = disk.get("crypto_topups")
        if isinstance(dsk_top, dict):
            state["crypto_topups"] = dict(dsk_top)
        _refresh_state_keys_from_disk("users")


def require_secret() -> None:
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
        abort(403)


def norm_user(name: str) -> str:
    return (name or "").strip().lower()


def _auth_serializer():
    from itsdangerous import URLSafeTimedSerializer

    return URLSafeTimedSerializer(AUTH_SECRET_KEY, salt="pluxo-user-v1")


def make_auth_token(username: str) -> str:
    return _auth_serializer().dumps({"u": norm_user(username)})


def verify_auth_token(token: str) -> str | None:
    try:
        d = _auth_serializer().loads(token.strip(), max_age=86400 * 14)
        u = norm_user(d.get("u", ""))
        return u or None
    except Exception:
        return None


def request_auth_username() -> str | None:
    auth = request.headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        return None
    return verify_auth_token(auth[7:])


def is_site_web_admin(username: str) -> bool:
    u = norm_user(username)
    return u in [norm_user(x) for x in state.get("site_admin_usernames", [])]


def require_secret_or_site_admin_web() -> None:
    if request.headers.get("X-Webhook-Secret") == WEBHOOK_SECRET:
        return
    au = request_auth_username()
    if au and is_site_web_admin(au):
        return
    abort(403)


def can_read_balance(path_username: str) -> bool:
    if request.headers.get("X-Webhook-Secret") == WEBHOOK_SECRET:
        return True
    au = request_auth_username()
    return au == norm_user(path_username)


def can_checkout(username: str) -> bool:
    if request.headers.get("X-Webhook-Secret") == WEBHOOK_SECRET:
        return True
    au = request_auth_username()
    return au == norm_user(username)


def _valid_topup_confirmation_url(url: str, method: str) -> bool:
    _ = method
    u = (url or "").strip()
    if len(u) < 24 or len(u) > 480:
        return False
    p = urlparse(u)
    if p.scheme != "https":
        return False
    if not p.netloc:
        return False
    path_l = (p.path or "").lower()
    if "/tx/" not in path_l and "/transaction/" not in path_l and "/transactions/" not in path_l:
        return False
    nl = p.netloc.lower()
    if nl.startswith("127.") or nl.startswith("localhost"):
        return False
    return True


def _topup_rate_key(ip: str, username: str) -> str:
    return f"{ip}|{norm_user(username)}"


def _topup_rate_allow(ip: str, username: str) -> bool:
    now = time.time()
    window = 3600.0
    key = _topup_rate_key(ip, username)
    with _topup_submit_lock:
        lst = _topup_submit_timestamps.setdefault(key, [])
        lst[:] = [t for t in lst if now - t < window]
        if len(lst) >= TOPUP_SUBMIT_MAX_HOURLY:
            return False
        lst.append(now)
    return True


TICKET_ID_RE = re.compile(r"^tk[a-f0-9]{12}$")


def norm_ticket_id(s: str) -> str | None:
    v = (s or "").strip().lower()
    return v if TICKET_ID_RE.fullmatch(v) else None


def _new_support_ticket_id() -> str:
    return "tk" + uuid.uuid4().hex[:12]


def _clip_text(s: str, n: int) -> str:
    t = str(s or "").strip().replace("\r\n", "\n")
    return t[:n]


def support_ticket_broadcast_new(tid: str, mu: str, subject: str, reason: str) -> None:
    """Notify admins on Telegram when a site user opens a ticket (HTTP API only)."""
    if not TELEGRAM_BOT_TOKEN:
        return
    subj_esc = html.escape(_clip_text(subject, 120))
    reason_esc = html.escape(_clip_text(reason, 80))
    txt = (
        "🎫 <b>New support ticket</b>\n\n"
        f"<b>ID:</b> <code>{html.escape(tid)}</code>\n"
        f"<b>Site user:</b> <code>{html.escape(norm_user(mu))}</code>\n"
        f"<b>Subject:</b> {subj_esc}\n"
        f"<b>Reason:</b> {reason_esc}\n\n"
        "<b>Commands</b>\n"
        f"• View: <code>/ticket {tid}</code>\n"
        f"• Reply: <code>/treply {tid} Your message...</code>\n"
        f"• Close: <code>/tresolve {tid}</code>"
    )
    for cid in _telegram_notification_targets():
        _telegram_api_send_message(cid, txt, None)


def _telegram_notification_targets() -> list[int]:
    with state_lock:
        out: set[int] = set()
        oid = state.get("owner_telegram_id")
        if oid is not None:
            try:
                out.add(int(oid))
            except (TypeError, ValueError):
                pass
        for a in state.get("admin_telegram_ids", []):
            try:
                out.add(int(a))
            except (TypeError, ValueError):
                continue
    if not out and OWNER_RAW.isdigit():
        out.add(int(OWNER_RAW))
    return sorted(out)


def _telegram_api_send_message(
    chat_id: int, text: str, reply_markup: dict[str, Any] | None
) -> dict[str, Any] | None:
    if not TELEGRAM_BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as e:
        print(f"[topup] telegram send failed: {e!r}", flush=True)
        return None
    if not body.get("ok"):
        print(f"[topup] telegram api error: {body!r}", flush=True)
        return None
    return body.get("result") or {}


def _broadcast_crypto_topup_pending(
    pid: str,
    site_user: str,
    amount: float,
    method: str,
    conf_url: str,
    client_ip: str,
) -> list[dict[str, int]]:
    mu = norm_user(site_user)
    safe_url = html.escape(conf_url, quote=True)
    link_preview = html.escape(conf_url[:200] + ("…" if len(conf_url) > 200 else ""))
    txt = (
        "🔔 <b>New top-up to verify</b>\n\n"
        f"<b>ID:</b> <code>{html.escape(pid)}</code>\n"
        f"<b>Site user:</b> <code>{html.escape(mu)}</code>\n"
        f"<b>Amount:</b> ${amount:.2f} USD\n"
        f"<b>Method:</b> {html.escape(method.upper())}\n"
        f"<b>IP:</b> <code>{html.escape((client_ip or '')[:64])}</code>\n"
        f'<b>Tx link:</b> <a href="{safe_url}">Open explorer</a>\n'
        f"<code>{link_preview}</code>\n\n"
        "<i>If funds received, tap Accept.</i>"
    )
    kb = {
        "inline_keyboard": [
            [
                {"text": "✅ Accept top-up", "callback_data": f"tua:{pid}"},
                {"text": "❌ Reject", "callback_data": f"tur:{pid}"},
            ]
        ]
    }
    msgs: list[dict[str, int]] = []
    for cid in _telegram_notification_targets():
        res = _telegram_api_send_message(cid, txt, kb)
        if res and "message_id" in res:
            msgs.append({"chat_id": int(cid), "message_id": int(res["message_id"])})
    return msgs


USER_RE_VALID = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")
REFERRAL_CODE_RE = re.compile(r"^[A-Z0-9]{6,12}$")
REFERRAL_DEPOSIT_BONUS_PCT = 10.0  # one-time +10% on first deposit for referred signups


def _normalize_referral_code(raw: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (raw or "").strip().upper())[:12]


def _referral_owner_for_code_unlocked(code: str) -> str | None:
    """Return username that owns this referral code (state_lock held)."""
    c = _normalize_referral_code(code)
    if not c or not REFERRAL_CODE_RE.fullmatch(c):
        return None
    for uname, row in state.get("users", {}).items():
        if not isinstance(row, dict):
            continue
        if str(row.get("referral_code") or "").upper() == c:
            return norm_user(uname)
    return None


def _referral_code_taken_unlocked(code: str, except_user: str | None = None) -> bool:
    c = _normalize_referral_code(code)
    owner = _referral_owner_for_code_unlocked(c)
    if not owner:
        return False
    if except_user and norm_user(except_user) == owner:
        return False
    return True


def ensure_user_referral_code_unlocked(username: str) -> str:
    """Assign a unique referral code if missing (state_lock held)."""
    rec = get_balance_record(username)
    existing = _normalize_referral_code(str(rec.get("referral_code") or ""))
    if existing and REFERRAL_CODE_RE.fullmatch(existing):
        rec["referral_code"] = existing
        return existing
    u = norm_user(username)
    prefix = re.sub(r"[^A-Z0-9]", "", u.upper())[:4] or "GOAT"
    for _ in range(64):
        suffix = secrets.token_hex(3).upper()[:6]
        cand = (prefix + suffix)[:12]
        if len(cand) < 6:
            cand = (cand + secrets.token_hex(2).upper())[:8]
        if not _referral_code_taken_unlocked(cand):
            rec["referral_code"] = cand
            return cand
    cand = secrets.token_hex(5).upper()[:10]
    rec["referral_code"] = cand
    return cand


def _referral_public_row(username: str, rec: dict[str, Any]) -> dict[str, Any]:
    code = ensure_user_referral_code_unlocked(username)
    referred_by = norm_user(str(rec.get("referred_by") or "")) or None
    bonus_used = bool(rec.get("referral_deposit_bonus_used"))
    pending = bool(referred_by and not bonus_used)
    return {
        "referral_code": code,
        "referred_by": referred_by,
        "referral_deposit_bonus_used": bonus_used,
        "referral_deposit_bonus_pending": pending,
        "referral_deposit_bonus_pct": REFERRAL_DEPOSIT_BONUS_PCT,
    }


def _count_referrals_unlocked(referrer: str) -> int:
    ref = norm_user(referrer)
    n = 0
    for uname, row in state.get("users", {}).items():
        if not isinstance(row, dict):
            continue
        if norm_user(str(row.get("referred_by") or "")) == ref:
            n += 1
    return n


def credit_user_deposit_unlocked(
    username: str,
    amount: float,
    *,
    log_line: str,
    uid: int | None = None,
) -> dict[str, Any]:
    """Credit balance + totalRecharge; apply one-time referral deposit bonus if eligible."""
    u = norm_user(username)
    amt = round(max(0.0, float(amount)), 2)
    rec = get_balance_record(u)
    ensure_user_referral_code_unlocked(u)
    bonus = 0.0
    rec["balance"] = round(float(rec["balance"]) + amt, 2)
    rec["totalRecharge"] = round(float(rec.get("totalRecharge", 0)) + amt, 2)
    referrer = norm_user(str(rec.get("referred_by") or ""))
    if (
        amt > 0
        and referrer
        and referrer != u
        and not rec.get("referral_deposit_bonus_used")
    ):
        bonus = round(amt * (REFERRAL_DEPOSIT_BONUS_PCT / 100.0), 2)
        if bonus > 0:
            rec["balance"] = round(float(rec["balance"]) + bonus, 2)
            rec["referral_deposit_bonus_used"] = True
            _action_log_unlocked(
                f"referral bonus {u} +${bonus:.2f} ({REFERRAL_DEPOSIT_BONUS_PCT:.0f}% of ${amt:.2f}) via {referrer}",
                uid=uid,
            )
    _action_log_unlocked(log_line, uid=uid)
    return {
        "username": u,
        "credited": amt,
        "referral_bonus": bonus,
        "balance": float(rec["balance"]),
        "totalRecharge": float(rec.get("totalRecharge", 0)),
    }


def get_balance_record(username: str) -> dict[str, Any]:
    """Return account row while holding state_lock (must be called inside lock).
    Migrates legacy {balance,totalRecharge} dicts.
    """
    u = norm_user(username)
    users = state.setdefault("users", {})
    if u not in users:
        users[u] = {"balance": 0.0, "totalRecharge": 0.0, "pwd_hash": None, "email": ""}
        return users[u]
    row = users[u]
    if not isinstance(row, dict):
        row = {"balance": 0.0, "totalRecharge": 0.0, "pwd_hash": None, "email": ""}
        users[u] = row
    row.setdefault("balance", float(row.get("balance", 0) or 0))
    row.setdefault("totalRecharge", float(row.get("totalRecharge", 0) or 0))
    row.setdefault("pwd_hash", row.get("pwd_hash"))
    row.setdefault("email", row.get("email") or "")
    row.setdefault("referred_by", norm_user(str(row.get("referred_by") or "")) or "")
    row.setdefault("referral_deposit_bonus_used", bool(row.get("referral_deposit_bonus_used")))
    if row.get("referral_code"):
        row["referral_code"] = _normalize_referral_code(str(row["referral_code"]))
    return row


def extract_bin(card_blob: str) -> str:
    m = re.search(r"\d{6,19}", card_blob.replace(" ", ""))
    if m:
        return m.group()[:6]
    return "000000"


def _frontend_country(code: str) -> dict[str, Any]:
    c = (code or "").strip().upper()
    c = _STOCK_COUNTRY_ALIASES.get(c, c)
    if c == "UK":
        c = "GB"
    row = FRONTEND_COUNTRY_PRESETS.get(c)
    if row:
        return dict(row)
    return dict(FRONTEND_COUNTRY_PRESETS["US"])


def _stock_pipe_layout(parts: list[str]) -> str:
    """mm_yy_split: PAN|MM|YY|CVV|Name|… — slash_exp: PAN|MM/YY|CVV|Name|…"""
    if len(parts) < 4:
        return "slash_exp"
    p1, p2, p3 = parts[1], parts[2], parts[3]
    if (
        p1.isdigit()
        and len(p1) <= 2
        and p2.isdigit()
        and len(p2) <= 2
        and p3.isdigit()
        and 3 <= len(p3) <= 4
        and "/" not in p1
    ):
        return "mm_yy_split"
    return "slash_exp"


def _stock_has_email_slot(parts: list[str]) -> bool:
    """PAN|MM|YY|CVV|…|ZIP|Phone|Email|Brand|… (email at index 10)."""
    if _stock_pipe_layout(parts) != "mm_yy_split" or len(parts) < 12:
        return False
    return "@" in (parts[10] or "")


def _stock_field_indices(parts: list[str]) -> dict[str, Any]:
    """Return field index map; includes 'layout' key for parsers."""
    if _stock_pipe_layout(parts) == "mm_yy_split" and _stock_has_email_slot(parts):
        # PAN|MM|YY|CVV|Name|Street|City|State|ZIP|Phone|Email|Brand|Type|Level|Issuer|Country|CC|IP?
        return {
            "layout": "mm_yy_email",
            "name": 4,
            "street": 5,
            "city": 6,
            "state": 7,
            "zip": 8,
            "phone": 9,
            "email": 10,
            "brand": 11,
            "card_type": 12,
            "level": 13,
            "issuer": 14,
            "country_name": 15,
            "country_code": 16,
        }
    if _stock_pipe_layout(parts) == "mm_yy_split":
        # PAN|MM|YY|CVV|Name|Street|City|State|ZIP|Country|Phone|Brand|Type|Level|Issuer|…
        return {
            "layout": "mm_yy_legacy",
            "name": 4,
            "street": 5,
            "city": 6,
            "state": 7,
            "zip": 8,
            "country": 9,
            "phone": 10,
            "extras": 11,
        }
    return {
        "layout": "slash_exp",
        "name": 3,
        "street": 4,
        "city": 5,
        "state": 6,
        "zip": 7,
        "country": 8,
        "phone": 9,
        "extras": 10,
    }


def _stock_part(parts: list[str], idx: int) -> str:
    if 0 <= idx < len(parts):
        return parts[idx].strip()
    return ""


def build_stock_row_from_line(
    card_raw: str,
    product_id: int,
    price_val: float,
    base_label: str,
    *,
    country_override: str | None = None,
) -> dict[str, Any]:
    """Build a shop row from pipe-delimited stock.

    Primary format (mm_yy_email):
      PAN|MM|YY|CVV|Name|Street|City|State|ZIP|Phone|Email|Brand|Type|Level|Issuer|Country|CC|IP?

    Legacy (mm_yy_legacy):
      PAN|MM|YY|CVV|Name|Street|City|State|ZIP|Country|Phone|Brand|Type|Level|Issuer|…

    Also supports PAN|MM/YY|CVV|Name|… (slash_exp).
    """

    def _nz(s: str | None) -> bool:
        return bool(s is not None and str(s).strip())

    _CARD_BRANDS = frozenset({"VISA", "MASTERCARD", "AMEX", "DISCOVER", "MC"})

    line = card_raw.strip()
    bin6 = extract_bin(line)
    brand = _brand_from_bin(bin6)
    known_bases = all_known_stock_bases_unlocked()
    safe_base = base_label if base_label in known_bases else str(base_label or SOLD_STOCK_FALLBACK_BUCKET)
    row: dict[str, Any] = {
        "id": product_id,
        "bin": bin6,
        "brand": brand,
        "bank": "",
        "base": safe_base,
        "refundable": True,
        "price": round(float(price_val), 2),
        "full_info": line,
        "has_name": False,
        "has_address": False,
        "has_zip": False,
        "has_phone": False,
        "has_mail": False,
        "state": "",
        "zip": "",
        "card_type": "",
        "level": "",
        "issuer": "",
    }
    co = normalize_stock_upload_country(country_override) if country_override else None
    if "|" not in line or line.count("|") < 8:
        if co:
            row["country"] = _frontend_country(co)
        return row
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 9:
        if co:
            row["country"] = _frontend_country(co)
        return row
    cc = parts[0]
    if cc:
        bin6 = extract_bin(cc)
        row["bin"] = bin6
        row["brand"] = _brand_from_bin(bin6)
    idx = _stock_field_indices(parts)
    layout = str(idx.get("layout") or "slash_exp")

    row["has_name"] = _nz(_stock_part(parts, idx["name"]))
    row["has_address"] = _nz(_stock_part(parts, idx["street"])) or _nz(
        _stock_part(parts, idx["city"])
    )
    row["has_zip"] = _nz(_stock_part(parts, idx["zip"]))
    row["state"] = _stock_part(parts, idx["state"])
    row["zip"] = _stock_part(parts, idx["zip"])

    if layout == "mm_yy_email":
        row["has_phone"] = _nz(_stock_part(parts, idx["phone"]))
        row["has_mail"] = _nz(_stock_part(parts, idx["email"]))
        cc_raw = _stock_part(parts, idx["country_code"]) or _stock_part(
            parts, idx["country_name"]
        )
        row["country"] = _frontend_country(co) if co else _frontend_country(cc_raw)
        b0 = _stock_part(parts, idx["brand"]).upper()
        if b0 in _CARD_BRANDS:
            row["brand"] = "MASTERCARD" if b0 == "MC" else b0
        row["card_type"] = _stock_part(parts, idx["card_type"])
        row["level"] = _stock_part(parts, idx["level"])
        iss = _stock_part(parts, idx["issuer"])
        row["bank"] = iss
        row["issuer"] = iss
        return row

    row["has_phone"] = _nz(_stock_part(parts, idx["phone"]))
    row["country"] = _frontend_country(co) if co else _frontend_country(
        _stock_part(parts, idx["country"])
    )
    ex = int(idx["extras"])
    tail = parts[ex:] if ex < len(parts) else []
    row["has_mail"] = bool(
        tail and (any(_nz(x) for x in tail) or any("@" in x for x in tail))
    )
    if tail:
        b0 = tail[0].upper()
        if b0 in _CARD_BRANDS:
            row["brand"] = "MASTERCARD" if b0 == "MC" else b0
        if len(tail) > 1:
            row["card_type"] = tail[1]
        if len(tail) > 2:
            row["level"] = tail[2]
        if len(tail) > 3:
            row["bank"] = tail[3]
            row["issuer"] = tail[3]
    return row


def _stock_batch_load_persisted(uid: int) -> dict[str, Any] | None:
    """Load in-progress /stock batch from state.json (survives worker swap / restart)."""
    with state_lock:
        sessions = state.get("telegram_stock_sessions")
        if not isinstance(sessions, dict):
            return None
        raw = sessions.get(str(uid))
        if not isinstance(raw, dict):
            return None
        started = float(raw.get("started") or 0)
        if started and (time.time() - started) > STOCK_UPLOAD_SESSION_TTL_SEC:
            sessions.pop(str(uid), None)
            save_state()
            return None
        chunks = raw.get("chunks")
        if not isinstance(chunks, list):
            chunks = []
        return {
            "price": round(float(raw.get("price") or 0), 2),
            "base": str(raw.get("base") or DEFAULT_STOCK_BASE_ID),
            "country": str(raw.get("country") or "US"),
            "chunks": [str(c) for c in chunks if str(c).strip()],
            "started": started or time.time(),
            "card_count": int(raw.get("card_count") or 0),
        }


def _stock_batch_save_persisted(uid: int, sess: dict[str, Any]) -> None:
    with state_lock:
        sessions = state.setdefault("telegram_stock_sessions", {})
        if not isinstance(sessions, dict):
            sessions = {}
            state["telegram_stock_sessions"] = sessions
        sessions[str(uid)] = {
            "price": round(float(sess.get("price") or 0), 2),
            "base": str(sess.get("base") or DEFAULT_STOCK_BASE_ID),
            "country": str(sess.get("country") or "US"),
            "chunks": list(sess.get("chunks") or []),
            "started": float(sess.get("started") or time.time()),
            "card_count": int(sess.get("card_count") or 0),
        }
        save_state()


def _stock_batch_delete_persisted(uid: int) -> None:
    with state_lock:
        sessions = state.get("telegram_stock_sessions")
        if isinstance(sessions, dict) and str(uid) in sessions:
            del sessions[str(uid)]
            save_state()


def _stock_batch_session(
    context: Any, telegram_uid: int | None = None
) -> dict[str, Any] | None:
    sess = context.user_data.get(STOCK_UPLOAD_SESSION_KEY)
    if isinstance(sess, dict) and isinstance(sess.get("chunks"), list):
        return sess
    if telegram_uid is None:
        return None
    persisted = _stock_batch_load_persisted(telegram_uid)
    if persisted:
        context.user_data[STOCK_UPLOAD_SESSION_KEY] = persisted
        return persisted
    return None


def _stock_batch_clear(context: Any, telegram_uid: int | None = None) -> None:
    context.user_data.pop(STOCK_UPLOAD_SESSION_KEY, None)
    if telegram_uid is not None:
        _stock_batch_delete_persisted(telegram_uid)


def _stock_batch_merged_blob(sess: dict[str, Any]) -> str:
    chunks = sess.get("chunks") or []
    if not isinstance(chunks, list):
        return ""
    return "\n".join(str(c) for c in chunks if str(c).strip())


def _stock_batch_start(
    context: Any,
    *,
    telegram_uid: int,
    price: float,
    base_sel: str,
    country_code: str,
) -> None:
    sess = {
        "price": round(float(price), 2),
        "base": base_sel,
        "country": country_code,
        "chunks": [],
        "started": time.time(),
        "card_count": 0,
    }
    context.user_data[STOCK_UPLOAD_SESSION_KEY] = sess
    _stock_batch_save_persisted(telegram_uid, sess)


def _stock_batch_append(context: Any, text: str, telegram_uid: int) -> int:
    """Append a message chunk; return total parsed card line count."""
    sess = _stock_batch_session(context, telegram_uid)
    if not sess:
        return 0
    chunks = sess.setdefault("chunks", [])
    chunks.append((text or "").strip())
    cards = parse_stock_cards_bulk(_stock_batch_merged_blob(sess))
    sess["card_count"] = len(cards)
    _stock_batch_save_persisted(telegram_uid, sess)
    return len(cards)


def _commit_stock_cards_unlocked(
    cards: list[str],
    price: float,
    base_sel: str,
    country_code: str,
    *,
    log_line: str,
    uid: int | None = None,
) -> int:
    """Append cards; caller must hold state_lock."""
    if not cards:
        return 0
    stock = state.setdefault("stock", [])
    nid = int(state.get("next_product_id", 1))
    added = 0
    for card in cards:
        row = build_stock_row_from_line(
            card, nid, price, base_sel, country_override=country_code
        )
        stock.append(row)
        nid += 1
        added += 1
    state["next_product_id"] = nid
    _action_log_unlocked(log_line, uid=uid)
    append_cards_to_shop_stock_document(base_sel, cards)
    save_state()
    return added


def commit_stock_cards(
    cards: list[str],
    price: float,
    base_sel: str,
    country_code: str,
    *,
    log_line: str,
    uid: int | None = None,
) -> int:
    """Append parsed card lines to state stock; returns count added."""
    if not cards:
        return 0
    with state_lock:
        return _commit_stock_cards_unlocked(
            cards, price, base_sel, country_code, log_line=log_line, uid=uid
        )


# Records in /stock bulk paste: start with 6–19 digit PAN then '|'
_PAN_RECORD_START = re.compile(r"(?:^|[\s\r\n]+)(\d{6,19}\|)")


def _split_stock_bulk_segments(blob: str) -> list[str]:
    """First-level split: ;; groups, or one record per line, or one fat line."""
    blob = blob.replace("\r\n", "\n").strip()
    if not blob:
        return []
    if ";;" in blob:
        return [p.strip() for p in re.split(r"\s*;;\s*", blob) if p.strip()]
    lines = [ln.strip() for ln in blob.split("\n") if ln.strip()]
    if len(lines) > 1:
        return lines
    return [blob.strip()]


def _explode_segment_into_pan_records(segment: str) -> list[str]:
    """One line may contain multiple records: ...| 123456|PAN|… (whitespace before next PAN)."""
    segment = segment.strip()
    if not segment:
        return []
    ms = list(_PAN_RECORD_START.finditer(segment))
    if len(ms) <= 1:
        return [segment]
    out: list[str] = []
    for i, m in enumerate(ms):
        start = m.start(1)
        end = ms[i + 1].start(1) if i + 1 < len(ms) else len(segment)
        piece = segment[start:end].strip()
        if piece:
            out.append(piece)
    return out


def parse_stock_cards_bulk(blob: str) -> list[str]:
    """Flatten bulk /stock payload into individual card lines."""
    rows: list[str] = []
    for seg in _split_stock_bulk_segments(blob):
        rows.extend(_explode_segment_into_pan_records(seg))
    return rows


def _document_default_base_name() -> str:
    raw = (
        os.environ.get("PLUXO_DOCUMENT_DEFAULT_BASE", DEFAULT_STOCK_BASE_ID) or ""
    ).strip().upper()
    return raw if raw in VALID_STOCK_BASES else DEFAULT_STOCK_BASE_ID


def _document_line_card_payload(line: str) -> str | None:
    """If line is a card row in shop_stock.txt, return PAN|… payload; else None."""
    s = line.strip()
    if not s or "|" not in s:
        return None
    m = re.match(r"^([A-Z][A-Z0-9_]{2,31})\s+(\d{6,19}\|.*)$", s)
    if m:
        return m.group(2).strip()
    if re.match(r"^\d{6,19}\|", s):
        return s
    return None


def remove_sold_lines_from_shop_stock_document(sold_full_infos: list[str]) -> None:
    """Remove one file line per sold card (FIFO) matching full_info / PAN|… payload."""
    if not sold_full_infos or not SHOP_STOCK_DOCUMENT_PATH.is_file():
        return
    try:
        raw = SHOP_STOCK_DOCUMENT_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    ends_nl = raw.endswith("\n")
    lines = raw.splitlines()
    for sold in sold_full_infos:
        target = (sold or "").strip()
        if not target:
            continue
        idx: int | None = None
        for i, ln in enumerate(lines):
            pay = _document_line_card_payload(ln)
            if pay == target:
                idx = i
                break
        if idx is not None:
            del lines[idx]
    out = "\n".join(lines)
    if ends_nl or lines:
        out += "\n"
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SHOP_STOCK_DOCUMENT_PATH.write_text(out, encoding="utf-8")
    except OSError:
        pass


def append_cards_to_shop_stock_document(base_label: str, card_lines: list[str]) -> None:
    """If shop_stock.txt exists, append Telegram/web bulk lines so checkout removal stays in sync."""
    if not card_lines or not SHOP_STOCK_DOCUMENT_PATH.is_file():
        return
    b = (base_label or "").strip().upper()
    if not b:
        return
    try:
        parts: list[str] = []
        for card in card_lines:
            c = (card or "").strip()
            if c:
                parts.append(f"{b}\t{c}\n")
        if parts:
            with SHOP_STOCK_DOCUMENT_PATH.open("a", encoding="utf-8") as f:
                f.write("".join(parts))
    except OSError:
        pass


def parse_stock_inventory_document(
    blob: str,
    known_bases: set[str],
) -> tuple[list[tuple[str, str]], float | None, str | None]:
    """Parse admin stock document: section headers, inline BASE + card, PRICE line.

    Returns (entries (base, card_line), optional price from a PRICE line, error or None).
    """
    doc_price: float | None = None
    default_base = _document_default_base_name()
    current_base: str | None = default_base if default_base in known_bases else None
    if current_base is None:
        for cand in (b["id"] for b in STOCK_BASE_CATALOG):
            if cand in known_bases:
                current_base = cand
                break
        if current_base is None and known_bases:
            current_base = next(iter(sorted(known_bases)), None)

    entries: list[tuple[str, str]] = []

    for line in blob.replace("\r\n", "\n").split("\n"):
        s = line.rstrip()
        if not s.strip():
            continue
        st = s.strip()
        mpr = re.match(r"^PRICE\s*[:\s]?\s*(\d+(?:\.\d{1,2})?)\s*$", st, re.I)
        if mpr:
            try:
                doc_price = float(mpr.group(1))
            except ValueError:
                pass
            continue
        if "|" not in st:
            if ADMIN_STOCK_BASE_NAME_RE.fullmatch(st):
                ub = st.upper()
                if ub not in known_bases:
                    return [], None, f"Unknown base in document: {ub}"
                current_base = ub
            continue
        m = re.match(r"^([A-Z][A-Z0-9_]{2,31})\s+(\d{6,19}\|.*)$", st)
        if m:
            b = m.group(1).upper()
            card = m.group(2).strip()
            if b not in known_bases:
                return [], None, f"Unknown base: {b}"
            entries.append((b, card))
            continue
        if re.match(r"^\d{6,19}\|", st):
            if not current_base:
                return (
                    [],
                    None,
                    "Card line appears before a valid base (add a header like BLACKJACK_BASE, "
                    "or set PLUXO_DOCUMENT_DEFAULT_BASE).",
                )
            entries.append((current_base, st))
            continue
        return [], None, f"Unrecognized document line: {st[:120]}"

    if not entries:
        return [], doc_price, "No card lines found in document."
    return entries, doc_price, None


# --- Flask: Pluxo site (same origin as API) ---


def _send_index() -> Any:
    path = resolve_index_html()
    if not path:
        return (
            f"<!DOCTYPE html><html><body style='font-family:system-ui;padding:24px'>"
            f"<p>No <code>*.html</code> found in:</p><pre>{ROOT_DIR}</pre>"
            f"<p>Put <code>index (27).html</code> or <code>index.html</code> next to <code>pluxo_backend.py</code>.</p>"
            "</body></html>",
            404,
            {"Content-Type": "text/html; charset=utf-8"},
        )
    # Response(bytes) avoids rare send_file issues with spaces/parentheses on Windows paths.
    data = path.read_bytes()
    return Response(
        data,
        mimetype="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@app.before_request
def _serve_pluxo_home_before_routing():
    """Guarantee / and /index.html return the site (runs before URL matching)."""
    if request.method not in ("GET", "HEAD"):
        return None
    if request.path not in ("/", "/index.html"):
        return None
    return _send_index()


@app.route("/", methods=["GET", "HEAD"])
def root():
    return _send_index()


@app.route("/index.html", methods=["GET", "HEAD"])
def index_html_alias():
    return _send_index()


@app.get("/pluxo-ok")
def pluxo_ok():
    """Visit this to confirm you are hitting THIS app (not some other server on :5000)."""
    idx = resolve_index_html()
    return jsonify(
        {
            "pluxo": True,
            "folder": str(ROOT_DIR),
            "index_html": str(idx) if idx else None,
        }
    )


@app.get("/shop_products.json")
def shop_products_static():
    """Fallback file the HTML may fetch when the API has no products."""
    if SHOP_PRODUCTS_JSON.is_file():
        return send_file(SHOP_PRODUCTS_JSON, mimetype="application/json", max_age=0)
    return jsonify([])


# --- Flask: products (public) ---


@app.get("/api/stock-bases")
def api_stock_bases():
    """Catalog bases for shop filters and admin uploads."""
    return jsonify([{"id": b["id"], "label": b["label"]} for b in STOCK_BASE_CATALOG])


@app.get("/api/products")
def api_products():
    with state_lock:
        rows = []
        for row in state.get("stock", []):
            if not isinstance(row, dict):
                continue
            out = dict(row)
            out["base_label"] = stock_base_label(str(out.get("base") or ""))
            rows.append(out)
        body = jsonify(rows)
    body.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    body.headers["Pragma"] = "no-cache"
    return body


@app.get("/api/leads")
def api_leads_public():
    """Website Leads tab: first/last name, city, state, price; sensitive raw/PAN never exposed."""
    with state_lock:
        rows = list(state.get("leads", []))
    out: list[dict[str, Any]] = []
    for x in rows:
        try:
            lid = int(x.get("id", 0))
        except (TypeError, ValueError):
            continue
        price_out: float | None = None
        if "price" in x:
            try:
                price_out = round(float(x.get("price", 0) or 0), 2)
            except (TypeError, ValueError):
                price_out = None
        out.append(
            {
                "id": lid,
                "firstName": str(x.get("firstName") or ""),
                "lastName": str(x.get("lastName") or ""),
                "city": str(x.get("city") or ""),
                "state": str(x.get("state") or ""),
                "country": str(x.get("country") or ""),
                "price": price_out,
                "created": str(x.get("created") or ""),
            }
        )
    out.sort(key=lambda r: r["id"], reverse=True)
    return jsonify({"leads": out})


# --- Flask: website accounts (stored in state.json alongside Telegram-managed balances) ---


def _web_auth_stock_fields_unlocked(username: str) -> dict[str, Any]:
    u = norm_user(username)
    admin_flag = is_site_web_admin(u)
    bmap = state.get("admin_stock_bases") or {}
    stock_base = bmap.get(u)
    skip = can_skip_custom_stock_base(u)
    needs_stock_base = bool(admin_flag) and not stock_base and not skip
    return {
        "stock_base": stock_base,
        "needs_stock_base": needs_stock_base,
        "can_use_system_stock_bases": skip,
    }


@app.post("/api/signup")
def api_signup():
    data = request.get_json(force=True, silent=True) or {}
    username = norm_user(data.get("username", ""))
    password = data.get("password", "")
    email = (data.get("email") or "").strip()
    ref_code = _normalize_referral_code(
        str(data.get("referral_code") or data.get("ref") or "")
    )
    if not USER_RE_VALID.match(username):
        return jsonify({"ok": False, "error": "Username 3-32 chars, letters/numbers/.-/_"}), 400
    if not password or len(str(password)) < 6:
        return jsonify({"ok": False, "error": "Password at least 6 characters"}), 400
    with state_lock:
        rec = get_balance_record(username)
        if rec.get("pwd_hash"):
            return jsonify({"ok": False, "error": "Username already registered"}), 400
        referrer: str | None = None
        if ref_code:
            if not REFERRAL_CODE_RE.fullmatch(ref_code):
                return jsonify({"ok": False, "error": "Invalid referral code format"}), 400
            referrer = _referral_owner_for_code_unlocked(ref_code)
            if not referrer:
                return jsonify({"ok": False, "error": "Referral code not found"}), 400
            if norm_user(referrer) == username:
                return jsonify(
                    {"ok": False, "error": "You cannot sign up with your own referral code"}
                ), 400
        rec["pwd_hash"] = generate_password_hash(str(password))
        if email:
            rec["email"] = email[:200]
        if referrer and not rec.get("referred_by"):
            rec["referred_by"] = referrer
        ref_fields = _referral_public_row(username, rec)
        admin_flag = is_site_web_admin(username)
        bal = float(rec["balance"])
        tr = float(rec.get("totalRecharge", 0))
        stock_fields = _web_auth_stock_fields_unlocked(username)
        save_state()
    token = make_auth_token(username)
    return jsonify(
        {
            "ok": True,
            "success": True,
            "token": token,
            "username": username,
            "balance": bal,
            "totalRecharge": tr,
            "is_site_admin": admin_flag,
            **stock_fields,
            **ref_fields,
            "referrals_count": 0,
        }
    )


@app.post("/api/auth/login")
def api_auth_login():
    data = request.get_json(force=True, silent=True) or {}
    username = norm_user(data.get("username", ""))
    password = data.get("password", "")
    if not USER_RE_VALID.match(username):
        return jsonify({"success": False, "error": "Bad username"}), 400
    if not password:
        return jsonify({"success": False, "error": "Missing password"}), 400
    with state_lock:
        rec = get_balance_record(username)
        ph = rec.get("pwd_hash")
        if not ph or not check_password_hash(ph, str(password)):
            return jsonify({"success": False, "error": "Invalid credentials"}), 401
        ref_fields = _referral_public_row(username, rec)
        ref_count = _count_referrals_unlocked(username)
        admin_flag = is_site_web_admin(username)
        bal = float(rec["balance"])
        tr = float(rec.get("totalRecharge", 0))
        stock_fields = _web_auth_stock_fields_unlocked(username)
        save_state()
    token = make_auth_token(username)
    return jsonify(
        {
            "success": True,
            "token": token,
            "username": username,
            "balance": bal,
            "totalRecharge": tr,
            "is_site_admin": admin_flag,
            **stock_fields,
            **ref_fields,
            "referrals_count": ref_count,
        }
    )


@app.get("/api/auth/me")
def api_auth_me():
    au = request_auth_username()
    if not au:
        return jsonify({"success": False, "error": "Not logged in"}), 401
    with state_lock:
        rec = get_balance_record(au)
        ref_fields = _referral_public_row(au, rec)
        ref_count = _count_referrals_unlocked(au)
        admin_flag = is_site_web_admin(au)
        stock_fields = _web_auth_stock_fields_unlocked(au)
        save_state()
        return jsonify(
            {
                "success": True,
                "username": au,
                "balance": float(rec["balance"]),
                "totalRecharge": float(rec.get("totalRecharge", 0)),
                "is_site_admin": admin_flag,
                "has_password": bool(rec.get("pwd_hash")),
                **stock_fields,
                **ref_fields,
                "referrals_count": ref_count,
            }
        )


@app.get("/api/referral/validate/<code>")
def api_referral_validate(code: str):
    """Public check for signup form."""
    c = _normalize_referral_code(code)
    if not c or not REFERRAL_CODE_RE.fullmatch(c):
        return jsonify({"ok": False, "valid": False, "error": "Invalid code format"}), 400
    with state_lock:
        owner = _referral_owner_for_code_unlocked(c)
    if not owner:
        return jsonify({"ok": True, "valid": False, "error": "Code not found"})
    return jsonify(
        {
            "ok": True,
            "valid": True,
            "referrer": owner,
            "bonus_pct": REFERRAL_DEPOSIT_BONUS_PCT,
        }
    )


@app.get("/api/referral/me")
def api_referral_me():
    au = request_auth_username()
    if not au:
        return jsonify({"ok": False, "error": "Not logged in"}), 401
    with state_lock:
        rec = get_balance_record(au)
        ref_fields = _referral_public_row(au, rec)
        ref_count = _count_referrals_unlocked(au)
        referred_users = [
            norm_user(uname)
            for uname, row in state.get("users", {}).items()
            if isinstance(row, dict)
            and norm_user(str(row.get("referred_by") or "")) == norm_user(au)
        ]
        referred_users.sort()
        save_state()
    return jsonify(
        {
            "ok": True,
            "username": au,
            **ref_fields,
            "referrals_count": ref_count,
            "referred_usernames": referred_users[:200],
            "share_message": (
                f"Join GOATYS with my code {ref_fields['referral_code']} "
                f"— get {REFERRAL_DEPOSIT_BONUS_PCT:.0f}% extra on your first deposit!"
            ),
        }
    )


@app.post("/api/admin/promote-site-admin")
def api_admin_promote_site_admin():
    au = request_auth_username()
    if not au or not is_site_web_admin(au):
        abort(403)
    data = request.get_json(force=True, silent=True) or {}
    target = norm_user(data.get("username", ""))
    if not USER_RE_VALID.match(target):
        return jsonify({"ok": False, "error": "Invalid username"}), 400
    if target == norm_user(au):
        return jsonify({"ok": False, "error": "Use another admin to change your own role."}), 400
    with state_lock:
        lst = state.setdefault("site_admin_usernames", [])
        if target in [norm_user(x) for x in lst]:
            return jsonify({"ok": True, "already_admin": True})
        lst.append(target)
        get_balance_record(target)
        save_state()
    return jsonify({"ok": True})


@app.post("/api/admin/set-stock-base")
def api_admin_set_stock_base():
    au = request_auth_username()
    if not au or not is_site_web_admin(au):
        abort(403)
    data = request.get_json(force=True, silent=True) or {}
    raw = (data.get("base_key") or data.get("base") or "").strip().upper()
    if raw not in VALID_STOCK_BASES:
        labels = ", ".join(b["label"] for b in STOCK_BASE_CATALOG)
        return jsonify(
            {
                "ok": False,
                "error": f"Pick a shop base id: {labels} (e.g. BLACKJACK_BASE).",
            }
        ), 400
    u = norm_user(au)
    with state_lock:
        state.setdefault("admin_stock_bases", {})[u] = raw
        save_state()
    return jsonify({"ok": True, "stock_base": raw})


@app.post("/api/admin/stock-bulk")
def api_admin_stock_bulk():
    au = request_auth_username()
    if not au or not is_site_web_admin(au):
        abort(403)
    data = request.get_json(force=True, silent=True) or {}
    bulk = (data.get("bulk") or data.get("paste") or "").strip()
    if not bulk:
        return jsonify({"ok": False, "error": "Paste card lines in bulk"}), 400
    try:
        price = round(float(data.get("price", 0) or 0), 2)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid price"}), 400
    if price <= 0:
        return jsonify({"ok": False, "error": "Price must be positive"}), 400
    country = normalize_stock_upload_country(str(data.get("country") or "US"))
    sys_base = (data.get("system_base") or "").strip().upper()
    u = norm_user(au)
    with state_lock:
        bmap = state.setdefault("admin_stock_bases", {})
        base_sel = bmap.get(u)
        if not base_sel and can_skip_custom_stock_base(au):
            base_sel = sys_base if sys_base in VALID_STOCK_BASES else default_stock_base_id()
        if not base_sel:
            return jsonify(
                {
                    "ok": False,
                    "error": "Register your stock base name first (welcome prompt or Admin portal).",
                }
            ), 400
        if base_sel not in all_known_stock_bases_unlocked():
            return jsonify({"ok": False, "error": "Invalid stock base"}), 400
        cards = parse_stock_cards_bulk(bulk)
        if not cards:
            return jsonify({"ok": False, "error": "No card lines found"}), 400
        if len(cards) > STOCK_BATCH_MAX:
            return jsonify(
                {"ok": False, "error": f"Maximum {STOCK_BATCH_MAX} lines per request"}
            ), 400
        added = _commit_stock_cards_unlocked(
            cards,
            price,
            base_sel,
            country,
            log_line=f"web stock-bulk +{len(cards)} @ ${price:.2f} base={base_sel} country={country}",
            uid=None,
        )
    return jsonify({"ok": True, "added": added, "base": base_sel, "country": country})


@app.post("/api/admin/stock-document/import")
def api_admin_stock_document_import():
    """Replace shop stock from a text document; saves data/shop_stock.txt; sales remove lines from that file."""
    au = request_auth_username()
    if not au or not is_site_web_admin(au):
        abort(403)

    document = ""
    up = request.files.get("file")
    if up and getattr(up, "filename", None):
        try:
            document = up.read().decode("utf-8", errors="replace")
        except Exception:
            return jsonify({"ok": False, "error": "Could not read uploaded file"}), 400
    if not document.strip():
        data = request.get_json(force=True, silent=True) or {}
        document = (data.get("document") or data.get("bulk") or data.get("text") or "").strip()
    if not document:
        return jsonify({"ok": False, "error": "Provide a .txt file or JSON { document: \"…\" }"}), 400

    payload = request.get_json(force=True, silent=True) if request.is_json else {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        env_doc = float(os.environ.get("PLUXO_DOCUMENT_DEFAULT_PRICE", "10").strip())
    except ValueError:
        env_doc = 10.0

    raw_price = payload.get("price")
    if raw_price is None or str(raw_price).strip() == "":
        raw_price = request.form.get("price")
    try:
        req_price = round(float(raw_price), 2) if raw_price is not None and str(raw_price).strip() != "" else None
    except (TypeError, ValueError):
        req_price = None

    country = normalize_stock_upload_country(
        str(payload.get("country") or request.form.get("country") or "US")
    )

    with state_lock:
        known = set(all_known_stock_bases_unlocked())
        entries, doc_price, perr = parse_stock_inventory_document(document, known)
        if perr:
            return jsonify({"ok": False, "error": perr}), 400
        if len(entries) > STOCK_BATCH_MAX:
            return jsonify(
                {"ok": False, "error": f"Maximum {STOCK_BATCH_MAX} lines per import (split the file)"}
            ), 400

        price = req_price if req_price is not None and req_price > 0 else None
        if price is None and doc_price is not None and doc_price > 0:
            price = round(float(doc_price), 2)
        if price is None and env_doc > 0:
            price = round(float(env_doc), 2)
        if price is None or price <= 0:
            return jsonify(
                {
                    "ok": False,
                    "error": "Set price in the form, use a PRICE 12.50 line in the document, or PLUXO_DOCUMENT_DEFAULT_PRICE in .env",
                }
            ), 400

        stock = state.setdefault("stock", [])
        stock.clear()
        nid = int(state.get("next_product_id", 1))
        for base_sel, card in entries:
            row = build_stock_row_from_line(
                card, nid, price, base_sel, country_override=country
            )
            stock.append(row)
            nid += 1
        state["next_product_id"] = nid
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            SHOP_STOCK_DOCUMENT_PATH.write_text(
                document.replace("\r\n", "\n"), encoding="utf-8"
            )
        except OSError as e:
            return jsonify({"ok": False, "error": f"Could not save shop_stock.txt: {e}"}), 500
        _action_log_unlocked(
            f"stock-document import {len(entries)} lines @ ${price:.2f} country={country}",
            uid=None,
        )
        save_state()

    return jsonify({"ok": True, "imported": len(entries), "price": price, "country": country})


@app.get("/api/admin/accounts")
def api_admin_accounts_list():
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
        au = request_auth_username()
        if not au or not is_site_web_admin(au):
            abort(403)
    with state_lock:
        rows = []
        for key, raw in sorted(state.get("users", {}).items()):
            if not isinstance(raw, dict):
                continue
            rows.append(
                {
                    "username": key,
                    "balance": float(raw.get("balance", 0) or 0),
                    "totalRecharge": float(raw.get("totalRecharge", 0) or 0),
                    "has_password": bool(raw.get("pwd_hash")),
                }
            )
        return jsonify({"success": True, "users": rows})


# --- Flask: register ---


@app.post("/api/register")
def api_register():
    require_secret()
    data = request.get_json(force=True, silent=True) or {}
    username = norm_user(data.get("username", ""))
    if not username:
        return jsonify({"ok": False, "error": "username required"}), 400
    with state_lock:
        get_balance_record(username)
        save_state()
    return jsonify({"ok": True, "success": True})


# --- Flask: balance ---


@app.get("/api/balance/<username>")
def api_balance_get(username: str):
    u = norm_user(username)
    if not can_read_balance(u):
        abort(403)
    with state_lock:
        rec = get_balance_record(u)
        return jsonify(
            {
                "success": True,
                "balance": float(rec["balance"]),
                "totalRecharge": float(rec.get("totalRecharge", 0)),
            }
        )


@app.post("/api/topup/submit")
def api_topup_submit():
    """Logged-in user submits crypto payment proof link; staff approves in Telegram."""
    data = request.get_json(force=True, silent=True) or {}
    site_u = norm_user(str(data.get("username", "")))
    auth_u = request_auth_username()
    if not auth_u or auth_u != site_u:
        return jsonify({"ok": False, "error": "Login required"}), 401
    try:
        amount = float(data.get("amount_usd") or data.get("amount") or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid amount"}), 400
    if amount < 10 or amount > 50_000:
        return jsonify({"ok": False, "error": "Amount must be between 10 and 50000 USD"}), 400
    method = norm_user(str(data.get("method", "")))
    if method not in ("btc", "ltc"):
        return jsonify({"ok": False, "error": "Unsupported method"}), 400
    conf_url = str(data.get("confirmation_url") or "").strip()
    if not _valid_topup_confirmation_url(conf_url, method):
        return jsonify(
            {
                "ok": False,
                "error": "Paste a valid https block explorer transaction link (e.g. mempool.space/tx/...).",
            }
        ), 400
    client_ip = (
        (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        or (request.remote_addr or "")
    ) or "0.0.0.0"
    if not _topup_rate_allow(client_ip, site_u):
        return jsonify(
            {
                "ok": False,
                "error": (
                    f"Too many top-up submissions. Maximum {TOPUP_SUBMIT_MAX_HOURLY} per hour — "
                    "try again later."
                ),
            }
        ), 429
    pid = uuid.uuid4().hex[:16]
    row = {
        "id": pid,
        "site_username": site_u,
        "amount_usd": round(amount, 2),
        "method": method,
        "confirmation_url": conf_url[:480],
        "client_ip": client_ip[:80],
        "status": "pending",
        "created_at": _utc_now_z(),
        "resolved_at": None,
        "resolved_by_tg": None,
        "notify_messages": [],
    }
    with state_lock:
        state.setdefault("crypto_topups", {})[pid] = row
        save_state()
    msgs = _broadcast_crypto_topup_pending(pid, site_u, amount, method, conf_url, client_ip)
    with state_lock:
        st = state.get("crypto_topups", {}).get(pid)
        if st:
            st["notify_messages"] = msgs
            save_state()
    return jsonify({"ok": True, "id": pid, "message": "Submitted — admins will verify on Telegram."})


def _ticket_row_pub(row: dict[str, Any]) -> dict[str, Any]:
    """JSON shape for logged-in ticket owner."""
    msgs = []
    for m in row.get("messages") or []:
        msgs.append(
            {
                "sender": str(m.get("sender") or ""),
                "text": str(m.get("text") or ""),
                "time": str(m.get("time") or ""),
                "is_admin": bool(m.get("is_admin")),
            }
        )
    return {
        "id": row.get("id"),
        "site_username": row.get("site_username"),
        "subject": row.get("subject"),
        "reason": row.get("reason"),
        "status": row.get("status"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "messages": msgs,
    }


@app.post("/api/tickets")
def api_support_ticket_create():
    au = request_auth_username()
    if not au:
        return jsonify({"ok": False, "error": "login required"}), 401
    data = request.get_json(force=True, silent=True) or {}
    subj = _clip_text(str(data.get("subject", "")), 200)
    reason = _clip_text(str(data.get("reason", "")), 100)
    desc = _clip_text(str(data.get("description", "")), 12000)
    if len(subj) < 3 or len(reason) < 2 or len(desc) < 5:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "Enter a subject (3+ chars), reason, and description (details).",
                }
            ),
            400,
        )
    tid = _new_support_ticket_id()
    now = _utc_now_z()
    row = {
        "id": tid,
        "site_username": au,
        "subject": subj,
        "reason": reason,
        "status": "progressing",
        "created_at": now,
        "updated_at": now,
        "messages": [{"sender": au, "text": desc, "time": now, "is_admin": False}],
    }
    with state_lock:
        state.setdefault("support_tickets", {})[tid] = row
        save_state()
    try:
        support_ticket_broadcast_new(tid, au, subj, reason)
    except Exception as exc:
        print(f"[ticket] telegram notify failed: {exc!r}", flush=True)
    return jsonify({"ok": True, "ticket": _ticket_row_pub(row)})


@app.get("/api/tickets")
def api_support_ticket_list_mine():
    au = request_auth_username()
    if not au:
        return jsonify({"ok": False, "error": "login required"}), 401
    out: list[dict[str, Any]] = []
    with state_lock:
        for _, row in (state.get("support_tickets") or {}).items():
            if norm_user(str(row.get("site_username"))) == au:
                out.append(_ticket_row_pub(row))
    out.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return jsonify({"ok": True, "tickets": out})


@app.get("/api/tickets/<tid>")
def api_support_ticket_one(tid: str):
    au = request_auth_username()
    if not au:
        return jsonify({"ok": False, "error": "login required"}), 401
    key = norm_ticket_id(tid)
    if not key:
        return jsonify({"ok": False, "error": "bad ticket id"}), 400
    with state_lock:
        row = state.get("support_tickets", {}).get(key)
    if not row:
        return jsonify({"ok": False, "error": "not found"}), 404
    if norm_user(str(row.get("site_username"))) != au and not is_site_web_admin(au):
        return jsonify({"ok": False, "error": "forbidden"}), 403
    return jsonify({"ok": True, "ticket": _ticket_row_pub(row)})


@app.post("/api/tickets/<tid>/reply")
def api_support_ticket_reply_user(tid: str):
    au = request_auth_username()
    if not au:
        return jsonify({"ok": False, "error": "login required"}), 401
    key = norm_ticket_id(tid)
    if not key:
        return jsonify({"ok": False, "error": "bad ticket id"}), 400
    data = request.get_json(force=True, silent=True) or {}
    text = _clip_text(str(data.get("text", "")), 12000)
    if len(text) < 1:
        return jsonify({"ok": False, "error": "message required"}), 400
    now = _utc_now_z()
    with state_lock:
        row = state.get("support_tickets", {}).get(key)
        if not row:
            return jsonify({"ok": False, "error": "not found"}), 404
        if norm_user(str(row.get("site_username"))) != au:
            return jsonify({"ok": False, "error": "forbidden"}), 403
        if row.get("status") != "progressing":
            return jsonify({"ok": False, "error": "ticket is closed"}), 400
        lst = row.setdefault("messages", [])
        lst.append({"sender": au, "text": text, "time": now, "is_admin": False})
        row["updated_at"] = now
        save_state()
        snap = dict(row)
    return jsonify({"ok": True, "ticket": _ticket_row_pub(snap)})


@app.get("/api/tickets/admin/all")
def api_support_ticket_admin_list():
    au = request_auth_username()
    if not au:
        return jsonify({"ok": False, "error": "login required"}), 401
    if not is_site_web_admin(au):
        return jsonify({"ok": False, "error": "site admin required"}), 403
    out: list[dict[str, Any]] = []
    with state_lock:
        for _, row in (state.get("support_tickets") or {}).items():
            out.append(_ticket_row_pub(row))
    out.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return jsonify({"ok": True, "tickets": out})


@app.post("/api/tickets/admin/resolve")
def api_support_ticket_admin_resolve():
    au = request_auth_username()
    if not au:
        return jsonify({"ok": False, "error": "login required"}), 401
    if not is_site_web_admin(au):
        return jsonify({"ok": False, "error": "site admin required"}), 403
    data = request.get_json(force=True, silent=True) or {}
    key = norm_ticket_id(str(data.get("ticket_id", data.get("id", ""))))
    if not key:
        return jsonify({"ok": False, "error": "bad ticket id"}), 400
    now = _utc_now_z()
    with state_lock:
        row = state.get("support_tickets", {}).get(key)
        if not row:
            return jsonify({"ok": False, "error": "not found"}), 404
        if row.get("status") != "progressing":
            return jsonify({"ok": False, "error": "already closed"}), 400
        row["status"] = "resolved"
        row["updated_at"] = now
        row.setdefault("messages", []).append(
            {
                "sender": au,
                "text": "Ticket marked as resolved via admin panel.",
                "time": now,
                "is_admin": True,
            }
        )
        save_state()
        snap = dict(row)
    _action_log_unlocked(f"[ticket resolve web] id={key} by {au}")
    return jsonify({"ok": True, "ticket": _ticket_row_pub(snap)})


@app.post("/api/balance/update")
def api_balance_update():
    require_secret()
    data = request.get_json(force=True, silent=True) or {}
    username = norm_user(data.get("username", ""))
    action = data.get("action", "")
    amount = float(data.get("amount", 0) or 0)
    if not username or action not in ("add", "subtract"):
        return jsonify({"success": False, "error": "bad request"}), 400
    with state_lock:
        rec = get_balance_record(username)
        bonus = 0.0
        if action == "subtract":
            if rec["balance"] < amount:
                return jsonify({"success": False, "error": "insufficient"}), 400
            rec["balance"] = round(rec["balance"] - amount, 2)
            nb = rec["balance"]
        else:
            out = credit_user_deposit_unlocked(
                username,
                amount,
                log_line=f"api balance/add {username} +${amount:.2f}",
            )
            nb = out["balance"]
            bonus = float(out.get("referral_bonus") or 0)
        save_state()
    return jsonify({"success": True, "newBalance": nb, "referral_bonus": bonus})


# --- Flask: checkout ---


@app.post("/api/purchase/checkout")
def api_checkout():
    data = request.get_json(force=True, silent=True) or {}
    username = norm_user(data.get("username", ""))
    items = data.get("items") or []
    if not username or not isinstance(items, list) or not items:
        return jsonify({"error": "invalid payload"}), 400

    if not can_checkout(username):
        abort(403)

    with state_lock:
        if state.get("lockdown"):
            return jsonify({"error": "system locked by admin — use /lockdown (owner Telegram)"}), 403
        rec = get_balance_record(username)
        stock = state.setdefault("stock", [])
        leads_lst = state.setdefault("leads", [])
        total = 0.0
        resolved_stock: list[dict[str, Any]] = []
        resolved_leads: list[dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                return jsonify({"error": "invalid item"}), 400
            lead_id_raw = it.get("leadId")
            if lead_id_raw is not None and lead_id_raw != "":
                try:
                    lid_chk = int(lead_id_raw)
                except (TypeError, ValueError):
                    return jsonify({"error": "invalid leadId"}), 400
                price_req = round(float(it.get("price", 0) or 0), 2)
                ld = next((l for l in leads_lst if int(l.get("id", -1)) == lid_chk), None)
                if not ld:
                    return jsonify({"error": f"lead {lid_chk} not available"}), 400
                lp = None
                if "price" in ld:
                    try:
                        lp = round(float(ld.get("price", 0) or 0), 2)
                    except (TypeError, ValueError):
                        lp = None
                if lp is None:
                    return jsonify({"error": f"lead {lid_chk} has no price"}), 400
                if abs(lp - price_req) > 0.009:
                    return jsonify({"error": "lead price mismatch"}), 400
                total += lp
                resolved_leads.append(ld)
                continue
            pid = it.get("productId")
            price = float(it.get("price", 0) or 0)
            row = next((s for s in stock if s.get("id") == pid), None)
            if not row:
                return jsonify({"error": f"product {pid} not found"}), 400
            if abs(float(row.get("price", 0)) - price) > 0.009:
                return jsonify({"error": "price mismatch"}), 400
            total += price
            resolved_stock.append(row)
        if rec["balance"] < total - 0.001:
            return jsonify({"error": "insufficient balance"}), 400
        bought: list[dict[str, Any]] = []
        sold_for_doc: list[str] = []
        for row in resolved_stock:
            stock[:] = [s for s in stock if s.get("id") != row.get("id")]
            pr = float(row.get("price", 0))
            fi = str(row.get("full_info") or "").strip()
            if fi:
                sold_for_doc.append(fi)
            bought.append(
                {
                    "kind": "stock",
                    "bin": row.get("bin"),
                    "base": row.get("base"),
                    "price": pr,
                    "refundable": row.get("refundable", True),
                    "full_info": row.get("full_info", ""),
                }
            )
            _record_sold_stock_unlocked(
                str(row.get("base") or ""),
                pr,
                username,
                row.get("id"),
            )
        if sold_for_doc:
            remove_sold_lines_from_shop_stock_document(sold_for_doc)
        for ld in resolved_leads:
            lid_rm = int(ld["id"])
            leads_lst[:] = [l for l in leads_lst if int(l.get("id", -2)) != lid_rm]
            fn = str(ld.get("firstName") or "").strip()
            ln = str(ld.get("lastName") or "").strip()
            nm = f"{fn} {ln}".strip() or fn or "—"
            pr = round(float(ld.get("price", 0) or 0), 2)
            raw = str(ld.get("raw") or "").strip()
            if raw:
                info = f"Lead purchase (ref #{lid_rm})\n\n{raw}"
            else:
                info = (
                    f"Lead purchase (ref #{lid_rm})\n"
                    f"Name: {nm}\n"
                    f"City: {str(ld.get('city') or '').strip()}\n"
                    f"State: {str(ld.get('state') or '').strip()}\n"
                ).strip()
            bought.append(
                {
                    "kind": "lead",
                    "bin": "LEAD",
                    "base": "—",
                    "price": pr,
                    "refundable": False,
                    "full_info": info,
                }
            )
        rec["balance"] = round(rec["balance"] - total, 2)
        nb = rec["balance"]
        plog = state.setdefault("purchase_log", [])
        plog.insert(
            0,
            {
                "t": _utc_now_z(),
                "username": username,
                "amount": round(total, 2),
                "item": f"{len(bought)} item(s)",
                "source": "checkout",
            },
        )
        if len(plog) > 500:
            del plog[500:]
        _action_log_unlocked(f"checkout {username} ${total:.2f} ({len(bought)} item(s))")
        save_state()

    return jsonify({"newBalance": nb, "items": bought})


# --- Dice ---


def _dice_roll() -> int:
    return random.randint(1, 6)


def _settle_balances_dice(
    creator: str, opponent: str, amount: float, cr: int, opr: int
) -> tuple[str, float, float]:
    """Returns winner username or 'tie', creator final balance, opponent final balance."""
    c, o = norm_user(creator), norm_user(opponent)
    rec_c = get_balance_record(c)
    rec_o = get_balance_record(o)
    amt = float(amount)
    if cr == opr:
        # Tie: each nets -amt/2 (refund half stake)
        rec_c["balance"] = round(rec_c["balance"] + amt / 2, 2)
        rec_o["balance"] = round(rec_o["balance"] + amt / 2, 2)
        return "tie", rec_c["balance"], rec_o["balance"]
    winner = c if cr > opr else o
    rec_w = get_balance_record(winner)
    # Winner recovers both stakes (+2*amt on top of current after both paid)
    rec_w["balance"] = round(rec_w["balance"] + 2 * amt, 2)
    return (winner if winner == c else o), rec_c["balance"], rec_o["balance"]


@app.post("/api/games/dice/create")
def dice_create():
    require_secret()
    data = request.get_json(force=True, silent=True) or {}
    creator = norm_user(data.get("creator", ""))
    creator_name = data.get("creatorName") or creator
    amount = float(data.get("amount", 0) or 0)
    if not creator or amount <= 0:
        return jsonify({"error": "invalid"}), 400
    with state_lock:
        rec = get_balance_record(creator)
        if rec["balance"] < amount:
            return jsonify({"error": "Insufficient balance"}), 400
        rec["balance"] = round(rec["balance"] - amount, 2)
        nb = rec["balance"]
        bet_id = str(uuid.uuid4())[:12]
        bet = {
            "id": bet_id,
            "creator": creator,
            "creatorName": creator_name,
            "amount": amount,
            "status": "waiting",
            "opponent": None,
            "opponentName": None,
        }
        state["dice"]["bets"].append(bet)
        save_state()
    return jsonify({"newBalance": nb, "bet": bet})


@app.get("/api/games/dice/bets")
def dice_bets():
    require_secret()
    with state_lock:
        return jsonify({"bets": list(state["dice"]["bets"])})


@app.get("/api/games/dice/history")
def dice_history():
    require_secret()
    with state_lock:
        return jsonify({"history": list(state["dice"]["history"])})


@app.post("/api/games/dice/accept")
def dice_accept():
    require_secret()
    data = request.get_json(force=True, silent=True) or {}
    bet_id = data.get("betId", "")
    opponent = norm_user(data.get("opponent", ""))
    opponent_name = data.get("opponentName") or opponent
    with state_lock:
        bets = state["dice"]["bets"]
        bet = next((b for b in bets if b.get("id") == bet_id), None)
        if not bet or bet.get("status") != "waiting":
            return jsonify({"error": "Bet not found"}), 400
        if bet["creator"] == opponent:
            return jsonify({"error": "cannot join own bet"}), 400
        amt = float(bet["amount"])
        rec_o = get_balance_record(opponent)
        if rec_o["balance"] < amt:
            return jsonify({"error": "Insufficient balance"}), 400
        rec_o["balance"] = round(rec_o["balance"] - amt, 2)
        cr, opr = _dice_roll(), _dice_roll()
        winner, bc, bo = _settle_balances_dice(bet["creator"], opponent, amt, cr, opr)
        hist_id = bet_id
        creator_display = bet.get("creatorName") or bet["creator"]
        wname = (
            "Tie"
            if winner == "tie"
            else (creator_display if norm_user(winner) == bet["creator"] else opponent_name)
        )
        hist = {
            "id": hist_id,
            "creator": bet["creator"],
            "creatorName": creator_display,
            "opponent": opponent,
            "opponentName": opponent_name,
            "amount": amt,
            "creatorRoll": cr,
            "opponentRoll": opr,
            "winner": winner,
            "winnerName": wname,
            "status": "completed",
            "completedAt": __import__("datetime").datetime.utcnow().isoformat() + "Z",
            "creatorBalanceAfter": bc,
            "opponentBalanceAfter": bo,
        }
        state["dice"]["history"].insert(0, hist)
        bets[:] = [b for b in bets if b.get("id") != bet_id]
        save_state()
        viewer = opponent
        vb = get_balance_record(viewer)["balance"]
        wkey = "tie" if winner == "tie" else norm_user(winner)
        result = {
            "id": hist_id,
            "creator": bet["creator"],
            "opponent": opponent,
            "amount": amt,
            "creatorRoll": cr,
            "opponentRoll": opr,
            "winner": wkey,
        }
        return jsonify({"result": result, "viewerBalance": vb})


@app.post("/api/games/dice/cancel")
def dice_cancel():
    require_secret()
    data = request.get_json(force=True, silent=True) or {}
    bet_id = data.get("betId", "")
    username = norm_user(data.get("username", ""))
    with state_lock:
        bets = state["dice"]["bets"]
        bet = next((b for b in bets if b.get("id") == bet_id), None)
        if not bet or bet.get("creator") != username:
            return jsonify({"error": "cannot cancel"}), 400
        if bet.get("status") != "waiting":
            return jsonify({"error": "not waiting"}), 400
        amt = float(bet["amount"])
        rec = get_balance_record(username)
        rec["balance"] = round(rec["balance"] + amt, 2)
        nb = rec["balance"]
        bets[:] = [b for b in bets if b.get("id") != bet_id]
        save_state()
    return jsonify({"newBalance": nb, "amount": amt})


# --- Blackjack ---


def _bj_score() -> int:
    return random.randint(17, 21)


def _settle_bj_balances(creator: str, opponent: str, amount: float, cs: int, os: int) -> tuple[str, float, float]:
    c, o = norm_user(creator), norm_user(opponent)
    amt = float(amount)
    rec_c = get_balance_record(c)
    rec_o = get_balance_record(o)
    if cs == os:
        rec_c["balance"] = round(rec_c["balance"] + amt / 2, 2)
        rec_o["balance"] = round(rec_o["balance"] + amt / 2, 2)
        return "tie", rec_c["balance"], rec_o["balance"]
    winner = c if cs > os else o
    rec_w = get_balance_record(winner)
    rec_w["balance"] = round(rec_w["balance"] + 2 * amt, 2)
    return (winner if winner == c else o), rec_c["balance"], rec_o["balance"]


@app.post("/api/games/blackjack/create")
def bj_create():
    require_secret()
    data = request.get_json(force=True, silent=True) or {}
    creator = norm_user(data.get("creator", ""))
    creator_name = data.get("creatorName") or creator
    amount = float(data.get("amount", 0) or 0)
    if not creator or amount <= 0:
        return jsonify({"error": "invalid"}), 400
    with state_lock:
        rec = get_balance_record(creator)
        if rec["balance"] < amount:
            return jsonify({"error": "Insufficient balance"}), 400
        rec["balance"] = round(rec["balance"] - amount, 2)
        nb = rec["balance"]
        mid = str(uuid.uuid4())[:12]
        m = {
            "id": mid,
            "creator": creator,
            "creatorName": creator_name,
            "amount": amount,
            "status": "waiting",
            "opponent": None,
            "opponentName": None,
        }
        state["blackjack"]["matches"].append(m)
        save_state()
    return jsonify({"newBalance": nb, "match": m})


@app.get("/api/games/blackjack/matches")
def bj_matches():
    require_secret()
    with state_lock:
        return jsonify({"matches": list(state["blackjack"]["matches"])})


@app.get("/api/games/blackjack/history")
def bj_history():
    require_secret()
    with state_lock:
        return jsonify({"history": list(state["blackjack"]["history"])})


@app.post("/api/games/blackjack/join")
def bj_join():
    require_secret()
    data = request.get_json(force=True, silent=True) or {}
    mid = data.get("matchId", "")
    opponent = norm_user(data.get("opponent", ""))
    opponent_name = data.get("opponentName") or opponent
    with state_lock:
        matches = state["blackjack"]["matches"]
        m = next((x for x in matches if x.get("id") == mid), None)
        if not m or m.get("status") != "waiting":
            return jsonify({"error": "Match not available"}), 400
        if m["creator"] == opponent:
            return jsonify({"error": "cannot join own"}), 400
        amt = float(m["amount"])
        rec_o = get_balance_record(opponent)
        if rec_o["balance"] < amt:
            return jsonify({"error": "Insufficient balance"}), 400
        rec_o["balance"] = round(rec_o["balance"] - amt, 2)
        cs, os_ = _bj_score(), _bj_score()
        winner, bc, bo = _settle_bj_balances(m["creator"], opponent, amt, cs, os_)
        creator_display = m.get("creatorName") or m["creator"]
        wname = (
            "Tie"
            if winner == "tie"
            else (creator_display if norm_user(winner) == m["creator"] else opponent_name)
        )
        hist = {
            "id": mid,
            "creator": m["creator"],
            "creatorName": creator_display,
            "opponent": opponent,
            "opponentName": opponent_name,
            "amount": amt,
            "creatorScore": cs,
            "opponentScore": os_,
            "winner": winner,
            "winnerName": wname,
            "status": "completed",
            "completedAt": __import__("datetime").datetime.utcnow().isoformat() + "Z",
            "creatorBalanceAfter": bc,
            "opponentBalanceAfter": bo,
        }
        state["blackjack"]["history"].insert(0, hist)
        matches[:] = [x for x in matches if x.get("id") != mid]
        save_state()
        vb = get_balance_record(opponent)["balance"]
        wkey = "tie" if winner == "tie" else norm_user(winner)
        result = {
            "id": mid,
            "creator": m["creator"],
            "opponent": opponent,
            "amount": amt,
            "creatorScore": cs,
            "opponentScore": os_,
            "winner": wkey,
        }
        return jsonify({"result": result, "viewerBalance": vb})


@app.post("/api/games/blackjack/cancel")
def bj_cancel():
    require_secret()
    data = request.get_json(force=True, silent=True) or {}
    mid = data.get("matchId", "")
    username = norm_user(data.get("username", ""))
    with state_lock:
        matches = state["blackjack"]["matches"]
        m = next((x for x in matches if x.get("id") == mid), None)
        if not m or m.get("creator") != username:
            return jsonify({"error": "cannot cancel"}), 400
        amt = float(m["amount"])
        rec = get_balance_record(username)
        rec["balance"] = round(rec["balance"] + amt, 2)
        nb = rec["balance"]
        matches[:] = [x for x in matches if x.get("id") != mid]
        save_state()
    return jsonify({"newBalance": nb, "amount": amt})


# --- Telegram bot ---


def _owner_id_from_env_or_state() -> int | None:
    """Prefer state file; fall back to OWNER_TELEGRAM_ID env (needed on fresh Railway deploys)."""
    oid = state.get("owner_telegram_id")
    if oid is not None:
        try:
            return int(oid)
        except (TypeError, ValueError):
            pass
    if OWNER_RAW.isdigit():
        return int(OWNER_RAW)
    return None


def _env_admin_id_set() -> set[int]:
    out: set[int] = set()
    raw = os.environ.get("ADMIN_TELEGRAM_IDS", "")
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


def _is_owner(uid: int) -> bool:
    oid = _owner_id_from_env_or_state()
    return oid is not None and int(uid) == oid


def _is_staff(uid: int) -> bool:
    uid = int(uid)
    if _is_owner(uid):
        return True
    if uid in _env_admin_id_set():
        return True
    return uid in [int(x) for x in state.get("admin_telegram_ids", [])]


TG_AUTH_FAIL = (
    "Not authorized. On Railway add Variables: OWNER_TELEGRAM_ID = your id from /myid "
    "(and TELEGRAM_BOT_TOKEN). Redeploy if you just changed variables."
)


try:
    LEADS_MAX_ROWS = int(os.environ.get("LEADS_MAX_ROWS", "2000").strip())
except ValueError:
    LEADS_MAX_ROWS = 2000
LEADS_MAX_ROWS = max(50, min(20000, LEADS_MAX_ROWS))

try:
    LEADS_BATCH_TELEGRAM_MAX = int(os.environ.get("LEADS_BATCH_TELEGRAM_MAX", "500").strip())
except ValueError:
    LEADS_BATCH_TELEGRAM_MAX = 500
LEADS_BATCH_TELEGRAM_MAX = max(1, min(2000, LEADS_BATCH_TELEGRAM_MAX))


def _strip_lead_csv_quotes(s: str) -> str:
    """Trim field padding and remove one pair of outer ``"…"`` from pasted pipe segments."""
    s = str(s or "").strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1].strip()
    return s


def _is_us_state_abbr(s: str) -> bool:
    t = _strip_lead_csv_quotes(s).strip().upper()
    return len(t) == 2 and t.isalpha()


def _country_token_to_display(s: str) -> str:
    u = _strip_lead_csv_quotes(s).upper().replace(".", "").replace(" ", "")
    if u in ("US", "USA", "UNITEDSTATES"):
        return "US"
    return ""


def _sniff_country_from_tail(parts: list[str], state_index: int) -> str:
    """Pick ``US`` from trailing ``UNITED STATES`` etc. (after state/zip/phone/email)."""
    for j in range(len(parts) - 1, state_index, -1):
        raw = _strip_lead_csv_quotes(parts[j])
        if not raw:
            continue
        if "@" in raw:
            continue
        dig = raw.replace("+", "").replace("-", "").replace(" ", "").replace("(", "").replace(")", "")
        if dig.isdigit():
            continue
        c = _country_token_to_display(raw)
        if c:
            return c
    return ""


def _city_from_street_comma_blob(blob: str) -> str:
    """``21 Denise Ct, Matteson`` → ``Matteson`` (last comma segment)."""
    b = _strip_lead_csv_quotes(blob)
    if "," not in b:
        return b.strip()
    return b.rsplit(",", 1)[-1].strip()


def parse_lead_paste(blob: str) -> dict[str, str] | None:
    """Parse pasted lead lines after optional ``[n] BIN … |`` head.

    Supports:

    - **Standard (8+ pipes):** ``PAN|mm|yy|cvv|Full name|Street|City|State|…``
    - **Split name (9+ pipes):** ``PAN|mm|yy|cvv|First|Last|Street|City|State|…``
      (detected when index 7 is not a 2-letter state but index 8 is).
    """
    blob = blob.strip()
    if not blob:
        return None
    if " | " in blob:
        _, _, blob = blob.partition(" | ")
    parts = [p.strip() for p in blob.split("|")]
    if len(parts) < 8:
        return None

    country = ""
    blob6 = _strip_lead_csv_quotes(parts[6]) if len(parts) > 6 else ""
    blob5 = _strip_lead_csv_quotes(parts[5]) if len(parts) > 5 else ""

    if (
        len(parts) >= 9
        and not _is_us_state_abbr(parts[7])
        and _is_us_state_abbr(parts[8])
    ):
        first = _strip_lead_csv_quotes(parts[4])
        last = _strip_lead_csv_quotes(parts[5])
        city = _strip_lead_csv_quotes(parts[7])
        region = _strip_lead_csv_quotes(parts[8])
        country = _sniff_country_from_tail(parts, 8)
    elif (
        len(parts) == 8
        and _is_us_state_abbr(parts[7])
        and "," in blob6
        and any(ch.isdigit() for ch in blob6)
        and not any(ch.isdigit() for ch in blob5)
    ):
        # First | Last | "street, city" | ST  (no separate zip segment)
        first = _strip_lead_csv_quotes(parts[4])
        last = _strip_lead_csv_quotes(parts[5])
        city = _city_from_street_comma_blob(parts[6])
        region = _strip_lead_csv_quotes(parts[7])
        country = _sniff_country_from_tail(parts, 7)
    else:
        full_name = _strip_lead_csv_quotes(parts[4])
        city = _strip_lead_csv_quotes(parts[6])
        region = _strip_lead_csv_quotes(parts[7])
        tokens = full_name.split()
        first = tokens[0] if tokens else ""
        last = " ".join(tokens[1:]) if len(tokens) > 1 else ""
        country = _sniff_country_from_tail(parts, 7)

    if not first and not last and not city and not region:
        return None
    out: dict[str, str] = {
        "firstName": first,
        "lastName": last,
        "city": city,
        "state": region,
    }
    if country:
        out["country"] = country
    return out


def parse_leads_price_prefix(body: str) -> tuple[float, str] | None:
    """First token price (``$1`` / ``1.50``); rest is one or concatenated paste lines."""
    body = body.strip()
    if not body:
        return None
    m = re.match(r"^\s*\$?\s*(\d+(?:\.\d+)?)\s+(.+)\s*$", body, flags=re.DOTALL | re.IGNORECASE)
    if not m:
        return None
    try:
        amt = round(float(m.group(1)), 2)
    except ValueError:
        return None
    if amt < 0 or amt > 1_000_000:
        return None
    return amt, m.group(2).strip()


def split_concatenated_lead_chunks(leads_blob: str) -> list[str]:
    """
    Turns glued lines like ``[1] BIN …970[2] BIN …971`` into separate strings
    by splitting at each ``[digits] BIN`` head.
    """
    s = leads_blob.strip()
    if not s:
        return []
    pat = re.compile(r"\[\s*\d+\s*\]\s+BIN\s+", re.IGNORECASE)
    idxs = [m.start() for m in pat.finditer(s)]
    if not idxs:
        return [s]
    chunks: list[str] = []
    for i, start in enumerate(idxs):
        end = idxs[i + 1] if i + 1 < len(idxs) else len(s)
        chunk = s[start:end].strip()
        if chunk:
            chunks.append(chunk)
    return chunks


def _brand_from_bin(bin6: str) -> str:
    if not bin6:
        return "VISA"
    if bin6[0] == "4":
        return "VISA"
    if bin6[0] == "5":
        return "MASTERCARD"
    if bin6.startswith("34") or bin6.startswith("37"):
        return "AMEX"
    return "VISA"


async def tg_start(update, context) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    uid = user.id
    display = user.first_name or user.username or "there"
    display = html.escape(str(display))
    if _is_owner(uid):
        role = "OWNER"
    elif _is_staff(uid):
        role = "ADMIN"
    else:
        role = "not linked yet (use server OWNER_TELEGRAM_ID)"
    text = (
        "🔐 <b>PLUXO Admin Bot</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 Welcome, {display}\n"
        f"🆔 Your ID: <code>{uid}</code>\n"
        f"👑 Role: {role}\n\n"
        "<b>💰 Balance</b>\n"
        "/balance &lt;user&gt; · /setbalance · /addbalance · /removebalance\n"
        "/users · /allbalances\n\n"
        "<b>🛒 Purchases</b>\n"
        "/addpurchase &lt;user&gt; &lt;item&gt; &lt;amt&gt;\n"
        "/purchases &lt;user&gt; · /recentpurchases\n\n"
        "<b>👥 Telegram admins</b> (OWNER)\n"
        "/addadmin /removeadmin · /admins\n\n"
        "<b>🌐 Website panel admins</b> (OWNER)\n"
        "/addsiteadmin /removesiteadmin · /listsiteadmins\n\n"
        "<b>🔒 System</b> (OWNER: /lockdown)\n"
        "/status · /logs\n\n"
        "<b>📦 Shop stock</b>\n"
        "/stock — pick base, <code>/stockcountry US</code>, then "
        "<code>/stock &lt;price&gt;</code> → paste lines (many messages OK) → <code>/done</code>\n"
        "Formats: <code>PAN|MM/YY|CVV|…</code> or <code>PAN|MM|YY|CVV|…</code>\n"
        "<code>/stock BLACKJACK_BASE &lt;price&gt; &lt;bulk&gt;</code> (one message) · /done · /cancelstock\n"
        "/stockbase · /soldstock · /mystock · /viewallstock · /allkeys · /redeem\n"
        "<b>📍 Website leads</b> — <code>/leads &lt;price&gt;</code> then paste (glue many\n"
        "<code>[n] BIN …</code> blocks). Site shows name, city, state, price.\n\n"
        "<b>🎫 Support tickets</b>\n"
        "/ticket · /ticket &lt;id&gt; · /treply · /tresolve\n\n"
        "<b>📣 Telegram</b>\n"
        f'<a href="{html.escape(GOATYS_TELEGRAM_CHAT_LIST_URL, quote=True)}">'
        "GOATYS.CC — add chats folder</a>\n\n"
        "<b>/myid</b> · <b>/help</b>\n\n"
        "<i>Put your bot token in server .env (TELEGRAM_BOT_TOKEN) — "
        "@BotFather on Telegram generates it.</i>"
    )
    await msg.reply_text(text, parse_mode="HTML")


async def tg_help(update, context) -> None:
    msg = update.effective_message
    if not msg:
        return
    await msg.reply_text(
        "<b>PLUXO — quick reference</b>\n\n"
        "<b>Balance</b>\n"
        "/balance /setbalance /addbalance /removebalance\n"
        "/users /allbalances\n\n"
        "<b>Purchases</b>\n"
        "/addpurchase &lt;user&gt; &lt;item&gt; &lt;amt&gt;\n"
        "/purchases &lt;user&gt; /recentpurchases\n\n"
        "<b>Shop</b>\n"
        "/stockcountry — US, DE, FR, … for next batch\n"
        "/stock &lt;price&gt; → paste (many msgs) → /done · /cancelstock\n"
        "/stock BASE &lt;price&gt; &lt;bulk&gt; — one-shot upload\n"
        "/stockbase /soldstock /mystock /viewallstock /allkeys /stats /redeem · "
        "<b>/leads &lt;$price&gt; paste…</b> → site Leads\n\n"
        "<b>TG admins</b> (OWNER): /addadmin /removeadmin /admins\n"
        "<b>Site admins</b> (OWNER): /addsiteadmin /removesiteadmin /listsiteadmins\n"
        "<b>System:</b> /status /logs · /lockdown (OWNER)\n"
        "<b>Support:</b> /ticket · /ticket &lt;id&gt; · /treply · /tresolve\n"
        '<b>Telegram folder:</b> <a href="'
        f'{html.escape(GOATYS_TELEGRAM_CHAT_LIST_URL, quote=True)}">GOATYS.CC chats</a>\n'
        "<b>You:</b> /myid · /help · /start",
        parse_mode="HTML",
    )


async def tg_myid(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    uid = update.effective_user.id
    await msg.reply_text(f"Your Telegram user id: <code>{uid}</code>", parse_mode="HTML")


async def tg_balance(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if not _is_staff(update.effective_user.id):
        await msg.reply_text(TG_AUTH_FAIL)
        return
    if not context.args:
        await msg.reply_text("Usage: /balance <user>")
        return
    u = norm_user(context.args[0])
    with state_lock:
        rec = get_balance_record(u)
        b = rec["balance"]
    await msg.reply_text(f"{u}: ${b:.2f}")


async def tg_setbalance(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if not _is_staff(update.effective_user.id):
        await msg.reply_text(TG_AUTH_FAIL)
        return
    if len(context.args) < 2:
        await msg.reply_text("Usage: /setbalance <user> <amount>")
        return
    u = norm_user(context.args[0])
    amt = float(context.args[1])
    with state_lock:
        rec = get_balance_record(u)
        rec["balance"] = round(amt, 2)
        _action_log_unlocked(f"/setbalance {u} -> ${amt:.2f}", uid=update.effective_user.id)
        save_state()
    await msg.reply_text(f"{u} balance set to ${amt:.2f}")


async def tg_addbalance(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if not _is_staff(update.effective_user.id):
        await msg.reply_text(TG_AUTH_FAIL)
        return
    if len(context.args) < 2:
        await msg.reply_text("Usage: /addbalance <user> <amount>")
        return
    u = norm_user(context.args[0])
    amt = float(context.args[1])
    bonus = 0.0
    with state_lock:
        out = credit_user_deposit_unlocked(
            u,
            amt,
            log_line=f"/addbalance {u} +${amt:.2f}",
            uid=update.effective_user.id,
        )
        nb = out["balance"]
        bonus = float(out.get("referral_bonus") or 0)
        save_state()
    extra = f" (+${bonus:.2f} referral bonus)" if bonus > 0 else ""
    await msg.reply_text(f"{u}: added ${amt:.2f}{extra} -> ${nb:.2f}")


async def tg_removebalance(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if not _is_staff(update.effective_user.id):
        await msg.reply_text(TG_AUTH_FAIL)
        return
    if len(context.args) < 2:
        await msg.reply_text("Usage: /removebalance <user> <amount>")
        return
    u = norm_user(context.args[0])
    amt = float(context.args[1])
    with state_lock:
        rec = get_balance_record(u)
        rec["balance"] = round(max(0.0, rec["balance"] - amt), 2)
        nb = rec["balance"]
        _action_log_unlocked(f"/removebalance {u} -${amt:.2f} -> ${nb:.2f}", uid=update.effective_user.id)
        save_state()
    await msg.reply_text(f"{u}: removed ${amt:.2f} -> ${nb:.2f}")


async def tg_users(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if not _is_staff(update.effective_user.id):
        await msg.reply_text(TG_AUTH_FAIL)
        return
    with state_lock:
        names = sorted(state.get("users", {}).keys())
    if not names:
        await msg.reply_text("No users yet.")
        return
    chunk = names[:80]
    await msg.reply_text("Users:\n" + "\n".join(chunk))


async def tg_stock_base_callback(update, context) -> None:
    q = update.callback_query
    if not q or not q.from_user:
        return
    if not _is_staff(q.from_user.id):
        await q.answer("Not allowed", show_alert=True)
        return
    data = (q.data or "").strip()
    if not data.startswith("stockbase:"):
        return
    b = data.split(":", 1)[1].strip().upper()
    with state_lock:
        ok = b in all_known_stock_bases_unlocked()
    if not ok:
        await q.answer("Unknown base", show_alert=True)
        return
    context.user_data["stock_upload_base"] = b
    await q.answer(f"Base set: {b}")
    await q.edit_message_text(
        f"✅ Upload base: <b>{html.escape(b)}</b>\n\n"
        "Bulk (Telegram splits long paste — use batch mode):\n"
        "<code>/stock &lt;price&gt;</code> → paste lines → <code>/done</code>\n\n"
        "One message:\n"
        f"<code>/stock {html.escape(b)} &lt;price&gt; &lt;bulk&gt;</code>",
        parse_mode="HTML",
    )


async def tg_stockbase(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if not _is_staff(update.effective_user.id):
        await msg.reply_text(TG_AUTH_FAIL)
        return
    if not context.args:
        cur = context.user_data.get("stock_upload_base")
        await msg.reply_text(
            f"Current upload base: <code>{html.escape(str(cur or '—'))}</code>\n"
            "Set: <code>/stockbase BLACKJACK_BASE</code> (or MONEYJR_BASE, UHQ_USA_BASE, "
            "FOREIGN_RICH_FUCKERS_BASE).",
            parse_mode="HTML",
        )
        return
    b = context.args[0].strip().upper()
    with state_lock:
        ok = b in all_known_stock_bases_unlocked()
    if not ok:
        await msg.reply_text(
            "Unknown base. Use BLACKJACK_BASE, MONEYJR_BASE, UHQ_USA_BASE, or FOREIGN_RICH_FUCKERS_BASE."
        )
        return
    context.user_data["stock_upload_base"] = b
    await msg.reply_text(f"Upload base set to <b>{html.escape(b)}</b>.", parse_mode="HTML")


async def tg_soldstock(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if not _is_staff(update.effective_user.id):
        await msg.reply_text(TG_AUTH_FAIL)
        return
    day = _today_utc_date_str()
    if context.args:
        cand = context.args[0].strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}$", cand):
            day = cand
    with state_lock:
        day_data = dict(state.get("sold_stock_daily", {}).get(day, {}))
    chunks: list[str] = [
        f"📊 <b>Sold stock</b> · <code>{html.escape(day)}</code> UTC\n",
    ]
    keys_ordered: list[str] = []
    for pref in (b["id"] for b in STOCK_BASE_CATALOG):
        if pref in day_data and pref not in keys_ordered:
            keys_ordered.append(pref)
    for bname in sorted(day_data.keys(), key=str):
        if bname not in keys_ordered and bname != SOLD_STOCK_FALLBACK_BUCKET:
            keys_ordered.append(bname)
    for bname in keys_ordered:
        rec = day_data.get(bname) or {"count": 0, "revenue": 0.0}
        c = int(rec.get("count", 0) or 0)
        rev = float(rec.get("revenue", 0) or 0)
        disp = stock_base_label(bname)
        chunks.append(
            f"\n<b>{html.escape(disp)}</b> <code>{html.escape(bname)}</code>\n"
            f"CARDS SOLD: {c}\n"
            f"Profit to pay out: <code>${rev:.2f}</code>\n"
        )
    un = day_data.get(SOLD_STOCK_FALLBACK_BUCKET) or {"count": 0, "revenue": 0.0}
    uc = int(un.get("count", 0) or 0)
    if uc:
        urev = float(un.get("revenue", 0) or 0)
        chunks.append(
            f"\n<b>{html.escape(SOLD_STOCK_FALLBACK_BUCKET)}</b> <i>(legacy / unknown base on card)</i>\n"
            f"CARDS SOLD: {uc}\n"
            f"Profit to pay out: <code>${urev:.2f}</code>\n"
        )
    await msg.reply_text("".join(chunks), parse_mode="HTML")


async def tg_stockcountry(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if not _is_staff(update.effective_user.id):
        await msg.reply_text(TG_AUTH_FAIL)
        return
    if not context.args:
        sample = ", ".join(sorted(FRONTEND_COUNTRY_PRESETS.keys())[:12])
        await msg.reply_text(
            "<b>Country for the next /stock batch</b>\n"
            "Example: <code>/stockcountry FR</code> (France) or <code>/stockcountry DE</code>.\n\n"
            f"Some codes: <code>{html.escape(sample)}</code>, …\n"
            "Default if not set: <code>US</code>.",
            parse_mode="HTML",
        )
        return
    c = normalize_stock_upload_country(context.args[0])
    context.user_data["stock_upload_country"] = c
    disp = _frontend_country(c)
    await msg.reply_text(
        f"✅ Stock upload country: <b>{html.escape(disp['name'])}</b> (<code>{html.escape(c)}</code>)\n"
        "Applies to card lines when you run <code>/stock</code> next.",
        parse_mode="HTML",
    )


def _build_stock_base_kb() -> Any:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for b in STOCK_BASE_CATALOG:
        row.append(
            InlineKeyboardButton(
                b["label"][:40],
                callback_data=f"stockbase:{b['id']}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def _tg_stock_reply_added(
    msg: Any,
    added: int,
    price: float,
    base_sel: str,
    country_code: str,
) -> None:
    lbl = html.escape(stock_base_label(base_sel))
    await msg.reply_text(
        f"✅ Added <b>{added}</b> card(s) at <code>${price:.2f}</code> → <b>{lbl}</b> "
        f"(country <code>{html.escape(country_code)}</code>).\n\n"
        "Refresh the shop on the site.\n\n"
        "💡 <b>Next time (long lists):</b> <code>/stock &lt;price&gt;</code> → paste all parts "
        "(split messages are OK) → <code>/done</code> commits the <b>full</b> batch.",
        parse_mode="HTML",
    )


async def _tg_stock_start_batch(
    msg: Any,
    context: Any,
    *,
    telegram_uid: int,
    price: float,
    base_sel: str,
    country_code: str,
    first_chunk: str = "",
) -> None:
    _stock_batch_start(
        context,
        telegram_uid=telegram_uid,
        price=price,
        base_sel=base_sel,
        country_code=country_code,
    )
    n = 0
    if first_chunk.strip():
        n = _stock_batch_append(context, first_chunk, telegram_uid)
    lbl = html.escape(stock_base_label(base_sel))
    await msg.reply_text(
        f"📥 <b>Stock upload started</b> — <code>${price:.2f}</code> → <b>{lbl}</b>\n"
        f"Buffered <b>{n}</b> line(s) so far.\n\n"
        "Telegram often <b>splits</b> long pastes into several messages — keep pasting "
        "every part here.\n\n"
        "⚠️ <b>Nothing is added to the shop until you send</b> <code>/done</code> "
        "(that commits the full batch).\n"
        "<code>/cancelstock</code> to abort.",
        parse_mode="HTML",
    )


async def _tg_stock_commit_blob(
    update: Any,
    context: Any,
    *,
    base_sel: str,
    price: float,
    country_code: str,
    blob: str,
    uid: int,
) -> None:
    msg = update.effective_message
    if not msg:
        return
    cards = parse_stock_cards_bulk(blob)
    if not cards:
        await msg.reply_text("No card lines found.")
        return
    if len(cards) > STOCK_BATCH_MAX:
        await msg.reply_text(
            f"Too many lines ({len(cards)}). Max is {STOCK_BATCH_MAX} per batch "
            "(raise STOCK_BATCH_MAX in .env up to 10000)."
        )
        return
    added = commit_stock_cards(
        cards,
        price,
        base_sel,
        country_code,
        log_line=f"/stock +{len(cards)} @ ${price:.2f} base={base_sel} country={country_code}",
        uid=uid,
    )
    _stock_batch_clear(context, uid)
    await _tg_stock_reply_added(msg, added, price, base_sel, country_code)


async def tg_done(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if not _is_staff(update.effective_user.id):
        await msg.reply_text(TG_AUTH_FAIL)
        return
    uid = int(update.effective_user.id)
    sess = _stock_batch_session(context, uid)
    if not sess:
        await msg.reply_text(
            "No upload in progress. Pick a base, then "
            "<code>/stock &lt;price&gt;</code>, paste lines, then <code>/done</code>.\n\n"
            "If you already pasted everything, run <code>/stock &lt;price&gt;</code> again and "
            "re-paste, then <code>/done</code> (sessions expire after 6 hours).",
            parse_mode="HTML",
        )
        return
    blob = _stock_batch_merged_blob(sess)
    n_ready = int(sess.get("card_count") or 0) or len(parse_stock_cards_bulk(blob))
    if n_ready:
        await msg.reply_text(
            f"Committing <b>{n_ready}</b> buffered line(s) to stock…",
            parse_mode="HTML",
        )
    await _tg_stock_commit_blob(
        update,
        context,
        base_sel=str(sess["base"]),
        price=float(sess["price"]),
        country_code=str(sess.get("country") or "US"),
        blob=blob,
        uid=update.effective_user.id,
    )


async def tg_cancelstock(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if not _is_staff(update.effective_user.id):
        await msg.reply_text(TG_AUTH_FAIL)
        return
    uid = int(update.effective_user.id)
    had = bool(_stock_batch_session(context, uid))
    _stock_batch_clear(context, uid)
    if had:
        await msg.reply_text("Stock upload cancelled.")
    else:
        await msg.reply_text("Nothing to cancel.")


async def tg_stock_batch_message(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user or not msg.text:
        return
    if not _is_staff(update.effective_user.id):
        return
    uid = int(update.effective_user.id)
    if not _stock_batch_session(context, uid):
        return
    n = _stock_batch_append(context, msg.text, uid)
    await msg.reply_text(
        f"Buffered <b>{n}</b> line(s) total.\n\n"
        "Send <code>/done</code> when all parts are pasted — "
        "<b>/done</b> commits the <b>full</b> stock batch.",
        parse_mode="HTML",
    )


async def tg_stock(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if not _is_staff(update.effective_user.id):
        await msg.reply_text(TG_AUTH_FAIL)
        return
    text = msg.text or ""
    mo = re.match(r"^/stock(?:@[A-Za-z0-9_]+)?\s*(.*)$", text, flags=re.IGNORECASE | re.DOTALL)
    body = (mo.group(1) if mo else "").strip()
    stock_base_kb = _build_stock_base_kb()
    uid = update.effective_user.id

    if not body:
        await msg.reply_text(
            "<b>Choose a base</b> — buttons or <code>/stockbase NAME</code> "
            "(BLACKJACK🃏, MoneyJR, UHQ USA 🔥, FOREIGN Rich Fuckers👲).\n\n"
            "<b>Country:</b> <code>/stockcountry US</code> …\n\n"
            "<b>One message:</b>\n"
            "<code>/stock BLACKJACK_BASE &lt;price&gt;\n&lt;PAN|MM|YY|CVV|…&gt;</code>\n\n"
            "<b>Large paste (1000+ lines):</b>\n"
            "1) Pick base · 2) <code>/stock &lt;price&gt;</code> · 3) paste (many msgs OK) · "
            "4) <code>/done</code>",
            parse_mode="HTML",
            reply_markup=stock_base_kb,
        )
        return

    country_code = normalize_stock_upload_country(
        str(context.user_data.get("stock_upload_country") or "US")
    )
    known = set(all_known_stock_bases_unlocked())

    # Card line(s) only — append to open batch or ask for /stock <price> first.
    if re.match(r"^\d{6,19}\|", body):
        sess = _stock_batch_session(context, uid)
        if sess:
            n = _stock_batch_append(context, body, uid)
            await msg.reply_text(
                f"Buffered <b>{n}</b> line(s) total.\n\n"
                "Send <code>/done</code> to commit the <b>full</b> batch to stock.",
                parse_mode="HTML",
            )
            return
        await msg.reply_text(
            "Pick a base, then <code>/stock &lt;price&gt;</code>, paste card lines, then "
            "<code>/done</code>.",
            parse_mode="HTML",
            reply_markup=stock_base_kb,
        )
        return

    base_sel: str | None = None
    price: float
    blob = ""

    m_full = re.match(
        r"^([A-Za-z][A-Za-z0-9_]{2,31})\s+(\d+(?:\.\d+)?)\s+([\s\S]*)$",
        body,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if m_full:
        cand_base = m_full.group(1).upper()
        if cand_base not in known:
            await msg.reply_text(
                f"Unknown base <code>{html.escape(cand_base)}</code>.",
                parse_mode="HTML",
                reply_markup=stock_base_kb,
            )
            return
        base_sel = cand_base
        try:
            price = float(m_full.group(2))
        except ValueError:
            await msg.reply_text("Invalid price.")
            return
        blob = (m_full.group(3) or "").strip()
    else:
        saved = context.user_data.get("stock_upload_base")
        if saved not in known:
            await msg.reply_text(
                "⚠️ <b>Pick a base first</b> (buttons below).",
                parse_mode="HTML",
                reply_markup=stock_base_kb,
            )
            return
        base_sel = str(saved)

        if re.match(r"^\d+(?:\.\d+)?$", body):
            try:
                price = float(body)
            except ValueError:
                await msg.reply_text("Invalid price.")
                return
            await _tg_stock_start_batch(
                msg,
                context,
                telegram_uid=uid,
                price=price,
                base_sel=base_sel,
                country_code=country_code,
            )
            return

        m_price = re.match(r"^(\d+(?:\.\d+)?)\s+([\s\S]+)$", body, flags=re.DOTALL)
        if not m_price:
            await msg.reply_text(
                "Usage: <code>/stock &lt;price&gt;</code> then paste lines, or "
                "<code>/stock BASE &lt;price&gt; &lt;bulk&gt;</code>.",
                parse_mode="HTML",
            )
            return
        g1 = m_price.group(1)
        g2 = (m_price.group(2) or "").strip()
        if re.match(r"^\d{6,19}$", g1) and "|" in body:
            blob = body
            sess = _stock_batch_session(context, uid)
            if sess:
                n = _stock_batch_append(context, blob, uid)
                await msg.reply_text(
                    f"Buffered <b>{n}</b> line(s) total.\n\n"
                    "Send <code>/done</code> to commit the <b>full</b> batch to stock.",
                    parse_mode="HTML",
                )
                return
            await msg.reply_text(
                "Send <code>/stock &lt;price&gt;</code> first, then paste card lines, then "
                "<code>/done</code>.",
                parse_mode="HTML",
            )
            return
        try:
            price = float(g1)
        except ValueError:
            await msg.reply_text("Invalid price.")
            return
        blob = g2

    if base_sel is None:
        await msg.reply_text("Could not determine base.")
        return

    if not blob.strip():
        await _tg_stock_start_batch(
            msg,
            context,
            telegram_uid=uid,
            price=price,
            base_sel=base_sel,
            country_code=country_code,
        )
        return

    cards = parse_stock_cards_bulk(blob)
    if len(cards) > STOCK_BATCH_MAX:
        await _tg_stock_start_batch(
            msg,
            context,
            telegram_uid=uid,
            price=price,
            base_sel=base_sel,
            country_code=country_code,
            first_chunk=blob,
        )
        await msg.reply_text(
            f"Batch mode — over {STOCK_BATCH_MAX} lines in one message. "
            "Keep pasting, then <code>/done</code> to commit the full batch.",
            parse_mode="HTML",
        )
        return

    # Multi-line pastes: always buffer until /done. Telegram may deliver only the
    # first chunk on a /stock BASE price bulk line — immediate commit caused partial stock.
    if len(cards) >= 2 or "\n" in blob:
        await _tg_stock_start_batch(
            msg,
            context,
            telegram_uid=uid,
            price=price,
            base_sel=base_sel,
            country_code=country_code,
            first_chunk=blob,
        )
        return

    if len(cards) == 1:
        await _tg_stock_commit_blob(
            update,
            context,
            base_sel=base_sel,
            price=price,
            country_code=country_code,
            blob=blob,
            uid=uid,
        )
        return

    await msg.reply_text("No card lines found in that message.", parse_mode="HTML")


async def tg_removestockslot(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if not _is_staff(update.effective_user.id):
        await msg.reply_text(TG_AUTH_FAIL)
        return
    if not context.args:
        await msg.reply_text("Usage: /removestockslot <id,id,...>")
        return
    raw = " ".join(context.args)
    ids: set[int] = set()
    for p in re.split(r"[,\s]+", raw):
        p = p.strip()
        if p.isdigit():
            ids.add(int(p))
    if not ids:
        await msg.reply_text("No valid ids.")
        return
    sold_doc: list[str] = []
    with state_lock:
        stock = state.setdefault("stock", [])
        for s in stock:
            if int(s.get("id", -1)) in ids:
                fi = str(s.get("full_info") or "").strip()
                if fi:
                    sold_doc.append(fi)
        before = len(stock)
        stock[:] = [s for s in stock if int(s.get("id", -1)) not in ids]
        removed = before - len(stock)
        if sold_doc:
            remove_sold_lines_from_shop_stock_document(sold_doc)
        _action_log_unlocked(
            f"/removestockslot removed {removed} row(s)",
            uid=update.effective_user.id,
        )
        save_state()
    await msg.reply_text(f"Removed {removed} row(s).")


async def tg_clearstock(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if not _is_staff(update.effective_user.id):
        await msg.reply_text(TG_AUTH_FAIL)
        return
    with state_lock:
        n = len(state.get("stock", []))
        state["stock"] = []
        try:
            if SHOP_STOCK_DOCUMENT_PATH.is_file():
                SHOP_STOCK_DOCUMENT_PATH.unlink()
        except OSError:
            pass
        _action_log_unlocked(f"/clearstock cleared {n} row(s)", uid=update.effective_user.id)
        save_state()
    await msg.reply_text(f"Cleared {n} items from shop stock.")


async def tg_addadmin(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if not _is_owner(update.effective_user.id):
        await msg.reply_text("Owner only. Set OWNER_TELEGRAM_ID on the server to your /myid")
        return
    if not context.args or not context.args[0].isdigit():
        await msg.reply_text("Usage: /addadmin <telegram_id>")
        return
    aid = int(context.args[0])
    with state_lock:
        lst = state.setdefault("admin_telegram_ids", [])
        if aid not in lst:
            lst.append(aid)
        _action_log_unlocked(f"/addadmin telegram_id={aid}", uid=update.effective_user.id)
        save_state()
    await msg.reply_text(f"Admin added: {aid}")


async def tg_removeadmin(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if not _is_owner(update.effective_user.id):
        await msg.reply_text("Owner only.")
        return
    if not context.args or not context.args[0].isdigit():
        await msg.reply_text("Usage: /removeadmin <telegram_id>")
        return
    aid = int(context.args[0])
    oid = _owner_id_from_env_or_state()
    if oid is not None and aid == int(oid):
        await msg.reply_text("Cannot remove owner.")
        return
    with state_lock:
        lst = state.setdefault("admin_telegram_ids", [])
        state["admin_telegram_ids"] = [x for x in lst if int(x) != aid]
        _action_log_unlocked(f"/removeadmin telegram_id={aid}", uid=update.effective_user.id)
        save_state()
    await msg.reply_text(f"Removed admin {aid} if present.")


async def tg_admins(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if not _is_staff(update.effective_user.id):
        await msg.reply_text(TG_AUTH_FAIL)
        return
    oid = _owner_id_from_env_or_state()
    with state_lock:
        lst = list(state.get("admin_telegram_ids", []))
    lines = [
        f"Owner: <code>{oid}</code>",
        "Telegram admins:",
    ]
    for a in lst:
        lines.append(f"- <code>{a}</code>")
    await msg.reply_text("\n".join(lines), parse_mode="HTML")


async def tg_addsiteadmin(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if not _is_owner(update.effective_user.id):
        await msg.reply_text("Owner only.")
        return
    if not context.args:
        await msg.reply_text("Usage: /addsiteadmin <site_username>")
        return
    u = norm_user(context.args[0])
    if not USER_RE_VALID.match(u):
        await msg.reply_text("Invalid username (3-32, letters/digits/./-/_).")
        return
    with state_lock:
        lst = state.setdefault("site_admin_usernames", [])
        if u not in [norm_user(x) for x in lst]:
            lst.append(u)
            save_state()
        get_balance_record(u)
        save_state()
    await msg.reply_text(f"Website admin added: <code>{u}</code>", parse_mode="HTML")


async def tg_removesiteadmin(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if not _is_owner(update.effective_user.id):
        await msg.reply_text("Owner only.")
        return
    if not context.args:
        await msg.reply_text("Usage: /removesiteadmin <site_username>")
        return
    u = norm_user(context.args[0])
    with state_lock:
        lst = state.setdefault("site_admin_usernames", [])
        state["site_admin_usernames"] = [x for x in lst if norm_user(x) != u]
        save_state()
    await msg.reply_text(f"Removed website admin if present: <code>{u}</code>", parse_mode="HTML")


async def tg_listsiteadmins(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if not _is_owner(update.effective_user.id):
        await msg.reply_text("Owner only.")
        return
    with state_lock:
        lst = list(state.get("site_admin_usernames", []))
    if not lst:
        await msg.reply_text("No website admins yet.")
        return
    body = "\n".join(f"- <code>{html.escape(norm_user(x))}</code>" for x in lst)
    await msg.reply_text("Website panel admins:\n" + body, parse_mode="HTML")


async def tg_allbalances(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if not _is_staff(update.effective_user.id):
        await msg.reply_text(TG_AUTH_FAIL)
        return
    with state_lock:
        rows: list[tuple[str, float]] = []
        users = state.get("users", {})
        for uname, rec in users.items():
            if isinstance(rec, dict):
                bal = float(rec.get("balance", 0) or 0)
            else:
                bal = 0.0
            rows.append((norm_user(str(uname)), bal))
    rows.sort(key=lambda x: (-x[1], x[0]))
    if not rows:
        await msg.reply_text("No user balances.")
        return
    cap = 50
    lines = [f"{u}: ${b:.2f}" for u, b in rows[:cap]]
    tail = ""
    if len(rows) > cap:
        tail = f"\n… +{len(rows) - cap} more users"
    await msg.reply_text("All balances:\n" + "\n".join(lines) + tail)


async def tg_status(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if not _is_staff(update.effective_user.id):
        await msg.reply_text(TG_AUTH_FAIL)
        return
    with state_lock:
        nu = len(state.get("users", {}))
        ns = len(state.get("stock", []))
        ld = bool(state.get("lockdown", False))
    alive = telegram_worker_alive()
    tok = "yes" if TELEGRAM_BOT_TOKEN else "missing"
    await msg.reply_text(
        "🔒 <b>System status</b>\n"
        f"👤 Registered accounts: <code>{nu}</code>\n"
        f"📦 Stock rows: <code>{ns}</code>\n"
        f"Lockdown (blocks checkout): <code>{'ON' if ld else 'off'}</code>\n"
        f"<code>TELEGRAM_BOT_TOKEN</code>: <code>{tok}</code>\n"
        f"Polling thread alive: <code>{alive}</code>\n"
        f"<code>DISABLE_TELEGRAM_BOT</code>: <code>{TELEGRAM_BOT_DISABLED}</code>",
        parse_mode="HTML",
    )


async def tg_logs(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if not _is_staff(update.effective_user.id):
        await msg.reply_text(TG_AUTH_FAIL)
        return
    with state_lock:
        logs = list(state.get("action_logs", []))[:18]
    if not logs:
        await msg.reply_text("No logs yet.")
        return
    parts: list[str] = []
    for entry in logs:
        t = html.escape(str(entry.get("t", "?")))
        line = html.escape(str(entry.get("line", "")))[:380]
        parts.append(f"<b>{t}</b>\n{line}")
    await msg.reply_text("Recent logs (newest first):\n\n" + "\n\n".join(parts), parse_mode="HTML")


async def tg_lockdown(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if not _is_owner(update.effective_user.id):
        await msg.reply_text("Owner only.")
        return
    oid = int(update.effective_user.id)
    with state_lock:
        new_val = not bool(state.get("lockdown", False))
        state["lockdown"] = new_val
        _action_log_unlocked(f"/lockdown -> {'ON' if new_val else 'OFF'}", uid=oid)
        save_state()
    note = (
        "🔒 Lockdown ON — web checkout blocked until you run /lockdown again."
        if new_val
        else "🔓 Lockdown OFF — checkout allowed."
    )
    await msg.reply_text(note)


async def tg_addpurchase(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if not _is_staff(update.effective_user.id):
        await msg.reply_text(TG_AUTH_FAIL)
        return
    if len(context.args) < 3:
        await msg.reply_text(
            "Usage: /addpurchase <username> <item_description> <amount>\n"
            'Example: /addpurchase alice base_pack 49.99'
        )
        return
    u = norm_user(context.args[0])
    try:
        amt = float(context.args[-1])
    except ValueError:
        await msg.reply_text("Amount must be a number.")
        return
    item_txt = " ".join(context.args[1:-1]).strip() or "(no description)"
    tid = update.effective_user.id
    with state_lock:
        plog = state.setdefault("purchase_log", [])
        plog.insert(
            0,
            {
                "t": _utc_now_z(),
                "username": u,
                "amount": round(amt, 2),
                "item": item_txt[:200],
                "source": "manual",
            },
        )
        if len(plog) > 500:
            del plog[500:]
        _action_log_unlocked(
            f"/addpurchase {u} '{item_txt[:48]}' ${amt:.2f}",
            uid=tid,
        )
        save_state()
    await msg.reply_text(f"Logged for <code>{html.escape(u)}</code>", parse_mode="HTML")


async def tg_purchases(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if not _is_staff(update.effective_user.id):
        await msg.reply_text(TG_AUTH_FAIL)
        return
    if not context.args:
        await msg.reply_text("Usage: /purchases <username>")
        return
    u = norm_user(context.args[0])
    with state_lock:
        plog_all = state.get("purchase_log", []) or []
    plog = [x for x in plog_all if norm_user(str(x.get("username", ""))) == u][:18]
    if not plog:
        await msg.reply_text(f"No logged purchases for {u}.")
        return
    lines: list[str] = []
    for x in plog:
        amt = float(x.get("amount", 0) or 0)
        item = html.escape(str(x.get("item", "")))[:80]
        src = html.escape(str(x.get("source", "")))
        lines.append(f"{html.escape(str(x.get('t', '')))}\n • ${amt:.2f} · {item} ({src})")
    await msg.reply_text(f"Purchases <code>{html.escape(u)}</code>:\n\n" + "\n\n".join(lines), parse_mode="HTML")


async def tg_recentpurchases(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if not _is_staff(update.effective_user.id):
        await msg.reply_text(TG_AUTH_FAIL)
        return
    with state_lock:
        plog = list(state.get("purchase_log", []))[:20]
    if not plog:
        await msg.reply_text("No purchases logged yet.")
        return
    lines: list[str] = []
    for x in plog:
        amt = float(x.get("amount", 0) or 0)
        un = html.escape(str(x.get("username", "")))
        item = html.escape(str(x.get("item", "")))[:52]
        src = html.escape(str(x.get("source", "")))
        lines.append(f"{html.escape(str(x.get('t', '')))} | <code>{un}</code> | ${amt:.2f}\n └ {item} ({src})")
    await msg.reply_text("<b>Recent purchases</b>\n\n" + "\n".join(lines), parse_mode="HTML")


async def tg_mystock(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if not _is_staff(update.effective_user.id):
        await msg.reply_text(TG_AUTH_FAIL)
        return
    with state_lock:
        n = len(state.get("stock", []))
    await msg.reply_text(
        f"📦 Active shop rows: <code>{n}</code>\n"
        f"Today's card sales by base: <b>/soldstock</b>",
        parse_mode="HTML",
    )


async def tg_viewallstock(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if not _is_staff(update.effective_user.id):
        await msg.reply_text(TG_AUTH_FAIL)
        return
    with state_lock:
        stock = list(state.get("stock", []))[:35]
    if not stock:
        await msg.reply_text("Stock is empty.")
        return
    lines = []
    for s in stock:
        pid = s.get("id", "?")
        price = float(s.get("price", 0) or 0)
        bin6 = s.get("bin", "")
        lines.append(
            f"<code>{pid}</code> · ${price:.2f} · {html.escape(str(s.get('base', '') or '—'))} · BIN {html.escape(str(bin6))}"
        )
    await msg.reply_text("Stock preview:\n" + "\n".join(lines), parse_mode="HTML")


async def tg_allkeys(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if not _is_staff(update.effective_user.id):
        await msg.reply_text(TG_AUTH_FAIL)
        return
    with state_lock:
        raw_ids: list[int] = []
        for x in state.get("stock", []):
            pid = x.get("id")
            try:
                raw_ids.append(int(pid))
            except (TypeError, ValueError):
                continue
    if not raw_ids:
        await msg.reply_text("No active product IDs.")
        return
    txt = ", ".join(str(i) for i in raw_ids[:80])
    extra = ""
    if len(raw_ids) > 80:
        extra = f"\n(+{len(raw_ids) - 80} more IDs in stock)"
    await msg.reply_text(f"Product IDs in stock:\n<code>{txt}</code>{extra}", parse_mode="HTML")


async def tg_stats(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if not _is_staff(update.effective_user.id):
        await msg.reply_text(TG_AUTH_FAIL)
        return
    with state_lock:
        nu = len(state.get("users", {}))
        ns = len(state.get("stock", []))
        np = len(state.get("purchase_log", []))
        na = len(state.get("action_logs", []))
    await msg.reply_text(
        "<b>Stats</b>\n"
        f"👤 Accounts: <code>{nu}</code>\n"
        f"📦 Stock rows: <code>{ns}</code>\n"
        f"📋 Purchase log rows: <code>{np}</code>\n"
        f"📜 Action log rows: <code>{na}</code>",
        parse_mode="HTML",
    )


async def tg_redeem(update, context) -> None:
    msg = update.effective_message
    if not msg:
        return
    await msg.reply_text(
        "<b>/redeem</b>\n"
        "Card details are revealed after payment on the <b>website</b> checkout.\n\n"
        "Log off-site orders here:\n<code>/addpurchase username item amount</code>",
        parse_mode="HTML",
    )


async def tg_leads(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if not _is_staff(update.effective_user.id):
        await msg.reply_text(TG_AUTH_FAIL)
        return
    raw_full = (msg.text or msg.caption or "").strip()
    mo = re.match(r"^/leads(?:@[A-Za-z0-9_]+)?\s*(.*)$", raw_full, flags=re.IGNORECASE | re.DOTALL)
    blob = ((mo.group(1) if mo else "") or "").strip()
    if not blob:
        await msg.reply_text(
            "<b>/leads</b> · add rows to the site <b>Leads</b> page\n\n"
            "<b>Usage:</b>\n<code>/leads &lt;price&gt; &lt;paste&gt;</code>\n"
            "Price first (e.g. <code>$1</code>), then glue one or many blocks starting with "
            "<code>[n] BIN …</code>\n\n"
            "Visitors see first & last name, city, state, and price only.",
            parse_mode="HTML",
        )
        return
    priced = parse_leads_price_prefix(blob)
    if not priced:
        await msg.reply_text(
            "Start with a price then the paste:\n<code>/leads $1 [1] BIN …971…</code>\n"
            "You can paste multiple <code>[n] BIN …</code> tails together (no newline needed).",
            parse_mode="HTML",
        )
        return
    price, leads_blob = priced
    chunks = split_concatenated_lead_chunks(leads_blob)
    if len(chunks) > LEADS_BATCH_TELEGRAM_MAX:
        await msg.reply_text(
            f"Too many lead blocks ({len(chunks)}). Maximum {LEADS_BATCH_TELEGRAM_MAX} per /leads.",
        )
        return
    to_save: list[tuple[str, dict[str, str]]] = []
    failed_positions: list[int] = []
    for idx, ch in enumerate(chunks, start=1):
        parsed = parse_lead_paste(ch)
        if parsed:
            to_save.append((ch, parsed))
        else:
            failed_positions.append(idx)
    if not to_save:
        await msg.reply_text(
            "Nothing parsed — check pipes / name / city / state fields (need 8+ segments after BIN head).",
        )
        return
    with state_lock:
        nid = int(state.get("next_lead_id") or 1)
        nid_start = nid
        lst = state.setdefault("leads", [])
        for ch, parsed in to_save:
            row = {
                "id": nid,
                "created": _utc_now_z(),
                "price": price,
                "firstName": parsed["firstName"],
                "lastName": parsed["lastName"],
                "city": parsed["city"],
                "state": parsed["state"],
                "country": str(parsed.get("country") or ""),
                "raw": ch[:2400],
            }
            lst.insert(0, row)
            nid += 1
        state["next_lead_id"] = nid
        if len(lst) > LEADS_MAX_ROWS:
            del lst[LEADS_MAX_ROWS:]
        _action_log_unlocked(
            f"/leads ×{len(to_save)} @ ${price:.2f} ids {nid_start}-{nid - 1}",
            uid=update.effective_user.id,
        )
        save_state()
    n = len(to_save)
    nid_end = nid - 1
    id_line = (
        f"ID <code>#{nid_start}</code>." if n == 1 else f"IDs <code>#{nid_start}</code>–<code>#{nid_end}</code>."
    )
    lines_r = [
        f"✅ Saved <b>{n}</b> lead(s) at <b>${price:.2f}</b> each.",
        id_line,
    ]
    if failed_positions:
        lines_r.append(f"⚠ Skipped chunk index(es): <code>{', '.join(map(str, failed_positions[:40]))}</code>")
        if len(failed_positions) > 40:
            lines_r.append("…")
    await msg.reply_text("\n".join(lines_r), parse_mode="HTML")


def _tg_staff_ticket_digest_html(row: dict[str, Any]) -> str:
    tid = html.escape(str(row.get("id", "") or ""))
    mu = html.escape(norm_user(str(row.get("site_username", "") or "")))
    status = html.escape(str(row.get("status", "") or ""))
    subj_esc = html.escape(_clip_text(str(row.get("subject") or ""), 140))
    tid_plain = str(row.get("id") or "")
    lines: list[str] = [
        f"🎫 <b>Ticket</b> <code>{tid}</code>",
        f"👤 <code>{mu}</code> · <b>{status}</b>",
        f"📌 {subj_esc}",
        "",
    ]
    msgs_in = row.get("messages") or []
    slice_msgs = msgs_in[-22:] if len(msgs_in) > 22 else msgs_in
    for m in slice_msgs:
        is_ad = bool(m.get("is_admin"))
        who_raw = str(m.get("sender") or ("Admin" if is_ad else "?"))
        if is_ad:
            lab = "🛡️ Admin"
        else:
            lab = html.escape(norm_user(who_raw))
        when_s = html.escape(str(m.get("time") or "")[:32])
        body_esc = html.escape(_clip_text(str(m.get("text") or ""), 3000))
        lines.append(f"— {lab} · <i>{when_s}</i>\n{body_esc}\n")
    lines.extend(
        ["", "<b>Actions</b>",
         f"<code>/treply {tid_plain} …</code>",
         f"<code>/tresolve {tid_plain}</code>"]
    )
    return "\n".join(lines)[:4090]


async def tg_ticket_cmd(update, context) -> None:
    msg = update.effective_message
    if not msg or not update.effective_user:
        return
    if not _is_staff(update.effective_user.id):
        await msg.reply_text(TG_AUTH_FAIL)
        return
    args = list(context.args or [])
    scope_open = True
    if args:
        tid = norm_ticket_id(str(args[0]))
        if tid:
            with state_lock:
                row = (state.get("support_tickets") or {}).get(tid)
            if not row:
                await msg.reply_text("Ticket not found.")
                return
            await msg.reply_text(_tg_staff_ticket_digest_html(row), parse_mode="HTML")
            return
        if str(args[0]).strip().lower() != "all":
            await msg.reply_text(
                "Usage:\n<code>/ticket</code> — open tickets\n"
                "<code>/ticket all</code> — recent tickets (any status)\n"
                "<code>/ticket tk…</code> — view thread",
                parse_mode="HTML",
            )
            return
        scope_open = False

    with state_lock:
        all_rows = list((state.get("support_tickets") or {}).values())
    rows = sorted(all_rows, key=lambda r: str(r.get("created_at") or ""), reverse=True)
    if scope_open:
        rows = [r for r in rows if r.get("status") == "progressing"]
    rows = rows[:35]
    if not rows:
        await msg.reply_text(
            "No tickets in this list." if scope_open else "No tickets yet."
        )
        return
    title = "🎫 <b>Open tickets</b>" if scope_open else "🎫 <b>Recent tickets</b>"
    lines_bt: list[str] = [title, ""]
    for i, r in enumerate(rows, 1):
        tid_e = html.escape(str(r.get("id", "") or ""))
        u_e = html.escape(norm_user(str(r.get("site_username", "") or "")))
        st_e = html.escape(str(r.get("status", "") or ""))
        sj = html.escape(_clip_text(str(r.get("subject") or ""), 60))
        lines_bt.append(
            f"{i}. <code>{tid_e}</code> · <code>{u_e}</code> · {st_e}\n   {sj}"
        )
    lines_bt.extend(
        [
            "",
            "<code>/ticket &lt;id&gt;</code> · <code>/treply &lt;id&gt; msg</code> · <code>/tresolve &lt;id&gt;</code>",
        ]
    )
    await msg.reply_text("\n".join(lines_bt)[:4090], parse_mode="HTML")


async def tg_treply(update, context) -> None:
    msg = update.effective_message
    eu = update.effective_user
    if not msg or not eu:
        return
    if not _is_staff(eu.id):
        await msg.reply_text(TG_AUTH_FAIL)
        return
    args = list(context.args or [])
    if len(args) < 2:
        await msg.reply_text(
            "Usage: <code>/treply &lt;ticket_id&gt; &lt;message&gt;</code>\n"
            "Example: <code>/treply tkabcd123456ef Thanks — we'll update you shortly.</code>",
            parse_mode="HTML",
        )
        return
    tid = norm_ticket_id(str(args[0]))
    if not tid:
        await msg.reply_text("Invalid ticket id (use the id shown on web or in /ticket, e.g. tk…).")
        return
    body = _clip_text(" ".join(args[1:]), 6000)
    if len(body) < 1:
        await msg.reply_text("Message is empty.")
        return
    now = _utc_now_z()
    lbl_raw = eu.username or eu.first_name or str(eu.id)
    lbl = _clip_text(str(lbl_raw), 48)
    sender = f"tg:{lbl}"
    err_early: str | None = None
    site_u_out = ""
    with state_lock:
        row = (state.get("support_tickets") or {}).get(tid)
        if not row:
            err_early = "not_found"
        elif row.get("status") != "progressing":
            err_early = "closed"
        else:
            row.setdefault("messages", []).append(
                {"sender": sender, "text": body, "time": now, "is_admin": True}
            )
            row["updated_at"] = now
            save_state()
            site_u_out = norm_user(str(row.get("site_username") or ""))
    if err_early == "not_found":
        await msg.reply_text("Ticket not found.")
        return
    if err_early == "closed":
        await msg.reply_text("That ticket is already closed (/tresolve already used).")
        return
    _action_log_unlocked(
        f"[ticket reply tg] id={tid} staff={lbl} → {site_u_out}", uid=int(eu.id)
    )
    await msg.reply_text(
        f"✅ Posted reply to ticket <code>{html.escape(tid)}</code> "
        f"(user <code>{html.escape(site_u_out)}</code>). They'll see it on the site.",
        parse_mode="HTML",
    )


async def tg_tresolve(update, context) -> None:
    msg = update.effective_message
    eu = update.effective_user
    if not msg or not eu:
        return
    if not _is_staff(eu.id):
        await msg.reply_text(TG_AUTH_FAIL)
        return
    args = list(context.args or [])
    if len(args) < 1:
        await msg.reply_text("Usage: <code>/tresolve &lt;ticket_id&gt;</code>", parse_mode="HTML")
        return
    tid = norm_ticket_id(str(args[0]))
    if not tid:
        await msg.reply_text("Invalid ticket id.")
        return
    now = _utc_now_z()
    lbl = eu.username or eu.first_name or str(eu.id)
    err_early: str | None = None
    site_u = ""
    with state_lock:
        row = state.get("support_tickets", {}).get(tid)
        if not row:
            err_early = "not_found"
        elif row.get("status") != "progressing":
            err_early = "done"
        else:
            row["status"] = "resolved"
            row["updated_at"] = now
            row.setdefault("messages", []).append(
                {
                    "sender": "telegram",
                    "text": f"Ticket closed by staff ({_clip_text(str(lbl), 40)}).",
                    "time": now,
                    "is_admin": True,
                }
            )
            save_state()
            site_u = norm_user(str(row.get("site_username") or ""))
    if err_early == "not_found":
        await msg.reply_text("Ticket not found.")
        return
    if err_early == "done":
        await msg.reply_text("Ticket was already resolved.")
        return
    _action_log_unlocked(
        f"[ticket resolve tg] id={tid} user={site_u} by {lbl}",
        uid=int(eu.id),
    )
    await msg.reply_text(
        f"✅ Ticket <code>{html.escape(tid)}</code> marked resolved (<code>{html.escape(site_u)}</code>).",
        parse_mode="HTML",
    )


async def tg_topup_callback(update, context) -> None:
    q = update.callback_query
    if not q or not q.data:
        return
    eu = update.effective_user
    if not eu:
        return
    if not _is_staff(int(eu.id)):
        await q.answer("Not authorized.", show_alert=True)
        return
    data = str(q.data)
    prefix, sep, pid = data.partition(":")
    if sep != ":" or prefix not in ("tua", "tur"):
        return
    if len(pid) != 16 or not re.fullmatch(r"[a-f0-9]{16}", pid):
        await q.answer("Invalid request id.", show_alert=True)
        return
    accept = prefix == "tua"
    err = ""
    amt = 0.0
    site_u = ""
    _reload_crypto_topups_from_disk()
    with state_lock:
        pend = state.get("crypto_topups", {}).get(pid)
        if not pend:
            err = "missing"
        elif pend.get("status") != "pending":
            err = "done"
        else:
            site_u = norm_user(str(pend["site_username"]))
            amt = float(pend.get("amount_usd", 0) or 0)
            if accept:
                credit_user_deposit_unlocked(
                    site_u,
                    amt,
                    log_line=f"topup accept {site_u} ${amt:.2f} id={pid}",
                    uid=int(eu.id),
                )
                pend["status"] = "accepted"
            else:
                pend["status"] = "rejected"
            pend["resolved_at"] = _utc_now_z()
            pend["resolved_by_tg"] = int(eu.id)
            if not accept:
                _action_log_unlocked(
                    f"topup reject {site_u} ${amt:.2f} id={pid}",
                    uid=int(eu.id),
                )
            save_state()
    if err == "missing":
        await q.answer(
            f"Request not found (id {pid}). The site may have saved it on another server — "
            "use one backend with a shared data/state.json, or accept via the site admin panel.",
            show_alert=True,
        )
        return
    if err == "done":
        await q.answer("Already handled.", show_alert=False)
        return
    old_text = (q.message.text if q.message else "") or ""
    suffix = (
        f"\n\n✅ ACCEPTED — credited ${amt:.2f} to {site_u}"
        if accept
        else f"\n\n❌ REJECTED — no credit ({site_u}, ${amt:.2f})"
    )
    try:
        await q.edit_message_text(text=old_text + suffix, reply_markup=None)
    except Exception as exc:
        print(f"[topup] edit_message_text: {exc!r}", flush=True)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
    await q.answer("Credited." if accept else "Rejected.")


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists but not ours to signal
    except OSError:
        return False
    return True


def _telegram_read_leader_lock_pid(lock_path: Path) -> int | None:
    try:
        raw = lock_path.read_text(encoding="utf-8").strip().split()
        if not raw:
            return None
        return int(raw[0])
    except (OSError, ValueError):
        return None


def _telegram_try_become_poll_leader() -> tuple[bool, str]:
    """
    Telegram allows only ONE getUpdates consumer per bot token. Gunicorn / multiple Flask
    processes on the SAME machine would conflict; take an exclusive PID lock under data/.
    Across separate hosts (e.g. two Railway replicas) each tries to poll → 409 Conflict;
    then set DISABLE_TELEGRAM_BOT=1 on all but ONE deploy / use a single worker.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = DATA_DIR / "telegram_poll.lock"
    mode = os.environ.get("PLUXO_TELEGRAM_POLL", "").strip().lower()

    # Per-process opt-out so extra Gunicorn workers only serve HTTP.
    if mode in ("never", "0", "false", "no", "off"):
        return False, "PLUXO_TELEGRAM_POLL=never (this worker will not poll; expected on worker 2+)"

    # Emergency override: skips lock — do NOT use with multiple workers or two hosts sharing the token.
    if mode == "force":
        return True, "PLUXO_TELEGRAM_POLL=force — lock skipped (single-instance only)"

    for _ in range(6):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, f"{os.getpid()}\n".encode())
            finally:
                os.close(fd)
            return True, f"leader lock OK (pid {os.getpid()})"
        except FileExistsError:
            other_pid = _telegram_read_leader_lock_pid(lock_path)
            if other_pid is None or not _pid_is_running(other_pid):
                try:
                    lock_path.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            return False, (
                f"another process holds telegram_poll.lock (pid {other_pid}) — "
                "this worker will not poll. Use one worker, or PLUXO_TELEGRAM_POLL=never here."
            )
    return False, "could not acquire telegram_poll.lock (try deleting data/telegram_poll.lock if stale)"


# Set by ensure_telegram_bot_started() for /telegram-status and logs.
TELEGRAM_SPAWN_RESULT: str = "not_started"


def run_telegram_bot() -> None:
    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN not set - skipping Telegram bot.")
        return
    from telegram import BotCommand
    from telegram.error import Conflict, TimedOut
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
    from telegram.request import HTTPXRequest

    connect_read_write, pool_t = _telegram_http_timeouts()

    async def post_init(app) -> None:
        await app.bot.delete_webhook(drop_pending_updates=True)
        # Persist OWNER_TELEGRAM_ID from env into state.json so owner survives restarts consistently.
        if OWNER_RAW.isdigit():
            oid = int(OWNER_RAW)
            with state_lock:
                state["owner_telegram_id"] = oid
                lst = state.setdefault("admin_telegram_ids", [])
                if oid not in lst:
                    lst.append(oid)
                save_state()
        await app.bot.set_my_commands(
            [
                BotCommand("start", "Main menu"),
                BotCommand("help", "All commands"),
                BotCommand("myid", "Your Telegram user id"),
                BotCommand("balance", "View user balance"),
                BotCommand("setbalance", "Set user balance"),
                BotCommand("addbalance", "Add to user balance"),
                BotCommand("removebalance", "Remove from user balance"),
                BotCommand("users", "List site users"),
                BotCommand("stockcountry", "Country for next /stock batch (US, DE, FR…)"),
                BotCommand("stock", "Add stock (pick base + optional /stockcountry)"),
                BotCommand("done", "Finish multi-message /stock upload"),
                BotCommand("cancelstock", "Cancel in-progress /stock upload"),
                BotCommand("stockbase", "Set default stock base"),
                BotCommand("soldstock", "Today's sold cards by base"),
                BotCommand("leads", "Add Leads (/leads price then BIN paste batch)"),
                BotCommand("removestockslot", "Remove stock by id"),
                BotCommand("clearstock", "Clear all stock"),
                BotCommand("addadmin", "Add admin (owner)"),
                BotCommand("removeadmin", "Remove admin (owner)"),
                BotCommand("admins", "List admins"),
                BotCommand("addsiteadmin", "Website panel admin"),
                BotCommand("removesiteadmin", "Remove panel admin"),
                BotCommand("listsiteadmins", "List panel admins"),
                BotCommand("allbalances", "All user balances"),
                BotCommand("addpurchase", "Log a purchase"),
                BotCommand("purchases", "User purchase log"),
                BotCommand("recentpurchases", "Recent purchases"),
                BotCommand("status", "System status"),
                BotCommand("logs", "Recent action logs"),
                BotCommand("lockdown", "Toggle checkout lock (owner)"),
                BotCommand("mystock", "Stock row count"),
                BotCommand("viewallstock", "Preview stock"),
                BotCommand("allkeys", "List product IDs"),
                BotCommand("stats", "Inventory stats"),
                BotCommand("redeem", "How cards are fulfilled"),
                BotCommand("ticket", "Support tickets (list / detail)"),
                BotCommand("treply", "Reply to a ticket (/treply id text)"),
                BotCommand("tresolve", "Close a ticket"),
            ]
        )

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        err = context.error
        if err:
            import traceback

            traceback.print_exception(type(err), err, err.__traceback__)

    http_request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=connect_read_write,
        read_timeout=connect_read_write,
        write_timeout=connect_read_write,
        pool_timeout=pool_t,
    )

    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .request(http_request)
        .post_init(post_init)
        .build()
    )
    application.add_error_handler(on_error)
    application.add_handler(
        CallbackQueryHandler(tg_stock_base_callback, pattern=r"^stockbase:([A-Za-z0-9_]+)$")
    )
    application.add_handler(
        CallbackQueryHandler(tg_topup_callback, pattern=r"^(tua|tur):[a-f0-9]{16}$")
    )
    application.add_handler(CommandHandler("start", tg_start))
    application.add_handler(CommandHandler("help", tg_help))
    application.add_handler(CommandHandler("myid", tg_myid))
    application.add_handler(CommandHandler("balance", tg_balance))
    application.add_handler(CommandHandler("setbalance", tg_setbalance))
    application.add_handler(CommandHandler("addbalance", tg_addbalance))
    application.add_handler(CommandHandler("removebalance", tg_removebalance))
    application.add_handler(CommandHandler("users", tg_users))
    application.add_handler(CommandHandler("stockcountry", tg_stockcountry))
    application.add_handler(CommandHandler("stock", tg_stock))
    application.add_handler(CommandHandler("done", tg_done))
    application.add_handler(CommandHandler("cancelstock", tg_cancelstock))
    application.add_handler(CommandHandler("stockbase", tg_stockbase))
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            tg_stock_batch_message,
        )
    )
    application.add_handler(CommandHandler("soldstock", tg_soldstock))
    application.add_handler(CommandHandler("removestockslot", tg_removestockslot))
    application.add_handler(CommandHandler("clearstock", tg_clearstock))
    application.add_handler(CommandHandler("addadmin", tg_addadmin))
    application.add_handler(CommandHandler("removeadmin", tg_removeadmin))
    application.add_handler(CommandHandler("admins", tg_admins))
    application.add_handler(CommandHandler("addsiteadmin", tg_addsiteadmin))
    application.add_handler(CommandHandler("removesiteadmin", tg_removesiteadmin))
    application.add_handler(CommandHandler("listsiteadmins", tg_listsiteadmins))
    application.add_handler(CommandHandler("allbalances", tg_allbalances))
    application.add_handler(CommandHandler("addpurchase", tg_addpurchase))
    application.add_handler(CommandHandler("purchases", tg_purchases))
    application.add_handler(CommandHandler("recentpurchases", tg_recentpurchases))
    application.add_handler(CommandHandler("status", tg_status))
    application.add_handler(CommandHandler("logs", tg_logs))
    application.add_handler(CommandHandler("lockdown", tg_lockdown))
    application.add_handler(CommandHandler("mystock", tg_mystock))
    application.add_handler(CommandHandler("viewallstock", tg_viewallstock))
    application.add_handler(CommandHandler("allkeys", tg_allkeys))
    application.add_handler(CommandHandler("stats", tg_stats))
    application.add_handler(CommandHandler("redeem", tg_redeem))
    application.add_handler(CommandHandler("leads", tg_leads))
    application.add_handler(CommandHandler("ticket", tg_ticket_cmd))
    application.add_handler(CommandHandler("treply", tg_treply))
    application.add_handler(CommandHandler("tresolve", tg_tresolve))

    # python-telegram-bot calls asyncio.get_event_loop(); threads have no loop on Python 3.10+.
    asyncio.set_event_loop(asyncio.new_event_loop())

    print(
        f"[telegram] starting long polling (HTTP timeout {connect_read_write}s, pool {pool_t}s)...",
        flush=True,
    )
    try:
        for attempt in range(1, 6):
            try:
                application.run_polling(
                    drop_pending_updates=True,
                    stop_signals=None,
                    allowed_updates=None,
                )
                break
            except TimedOut:
                if attempt >= 5:
                    print(
                        "[telegram] FATAL: still timing out after 5 attempts. "
                        "Check network/VPN/firewall to api.telegram.org, or set TELEGRAM_HTTP_TIMEOUT=90 (max 120).",
                        flush=True,
                    )
                    raise
                wait = 4 * attempt
                print(
                    f"[telegram] TimedOut connecting to Telegram API (attempt {attempt}/5), retry in {wait}s...",
                    flush=True,
                )
                time.sleep(wait)
    except Conflict:
        print(
            "[telegram] Conflict: another process is already using getUpdates for this bot token.\n"
            "  Stop other instances (second terminal, second Railway service), or set DISABLE_TELEGRAM_BOT=1 here.",
            flush=True,
        )
    except Exception as exc:
        import traceback

        print("[telegram] FATAL: polling crashed:", repr(exc), flush=True)
        traceback.print_exc()


def telegram_worker_alive() -> bool:
    return any(
        getattr(t, "name", "") == "telegram-bot" and t.is_alive() for t in threading.enumerate()
    )


_telegram_spawn_lock = threading.Lock()


def run_bot_thread(reason: str = "auto") -> str:
    global TELEGRAM_SPAWN_RESULT
    if TELEGRAM_BOT_DISABLED:
        print(
            "[telegram] DISABLE_TELEGRAM_BOT is set - bot not started here (API only).",
            flush=True,
        )
        TELEGRAM_SPAWN_RESULT = "disabled"
        return "disabled"
    if not TELEGRAM_BOT_TOKEN:
        print("[telegram] TELEGRAM_BOT_TOKEN is empty - bot not started.", flush=True)
        TELEGRAM_SPAWN_RESULT = "no_token"
        return "no_token"

    masked = TELEGRAM_BOT_TOKEN[:6] + "..." + TELEGRAM_BOT_TOKEN[-4:] if len(TELEGRAM_BOT_TOKEN) > 12 else "set"
    with _telegram_spawn_lock:
        if telegram_worker_alive():
            if reason == "api":
                print(
                    "[telegram] start-bot request ignored: polling thread already running",
                    flush=True,
                )
            TELEGRAM_SPAWN_RESULT = "already_running"
            return "already_running"

        ok_leader, leader_detail = _telegram_try_become_poll_leader()
        if not ok_leader:
            print(f"[telegram] {leader_detail}", flush=True)
            TELEGRAM_SPAWN_RESULT = "not_polling_leader"
            return "not_polling_leader"

        print(f"[telegram] launching bot thread ({reason}, token {masked}) — {leader_detail}", flush=True)
        threading.Thread(
            target=run_telegram_bot, name="telegram-bot", daemon=True
        ).start()
    TELEGRAM_SPAWN_RESULT = "started"
    return "started"


load_state()


# Start Telegram bot once when this module loads (needed for Gunicorn/Railway, not only `python pluxo_backend.py`).
_telegram_bootstrapped = False
_telegram_lock = threading.Lock()


def ensure_telegram_bot_started() -> None:
    global _telegram_bootstrapped
    with _telegram_lock:
        if _telegram_bootstrapped:
            return
        _telegram_bootstrapped = True
    print(
        f"[telegram] env check: token={'yes' if TELEGRAM_BOT_TOKEN else 'NO'} "
        f"owner={OWNER_RAW or 'NO'} disabled={TELEGRAM_BOT_DISABLED}",
        flush=True,
    )
    outcome = run_bot_thread("startup")
    if outcome == "started":
        print("[telegram] Polling thread started. If Telegram still silent: two hosts may share the bot token "
              "(Railway replicas / second PC)—only one must poll—or check server logs for Conflict / TimedOut.",
              flush=True)
    elif outcome == "not_polling_leader":
        print(
            "[telegram] This worker is not the Telegram poll leader. That is normal for Gunicorn worker 2+ "
            "if worker 1 holds data/telegram_poll.lock.",
            flush=True,
        )


ensure_telegram_bot_started()


@app.get("/telegram-status")
def telegram_status():
    """Quick check (no secret) so you can confirm Railway sees the env vars."""
    return jsonify(
        {
            "token_set": bool(TELEGRAM_BOT_TOKEN),
            "owner_set": bool(OWNER_RAW),
            "disabled": TELEGRAM_BOT_DISABLED,
            "bot_thread_started": _telegram_bootstrapped,
            "bot_thread_alive": telegram_worker_alive(),
            "spawn_result": TELEGRAM_SPAWN_RESULT,
            "polling_env": os.environ.get("PLUXO_TELEGRAM_POLL", "").strip() or None,
            "hints": [
                "If messages show two checkmarks but no reply: nothing is polling this bot token, or a second server also polls (Telegram returns 409 Conflict).",
                "Only one machine/process may call getUpdates per bot. Extra Gunicorn workers: set PLUXO_TELEGRAM_POLL=never on workers that should not poll.",
                "Two Railway replicas with the same token: set DISABLE_TELEGRAM_BOT=1 on one replica, or scale to 1 instance for the app that runs the bot.",
            ],
        }
    )


@app.post("/api/telegram/start-bot")
def api_telegram_start_bot():
    """Spawn Telegram long-polling thread if token is set and DISABLE_TELEGRAM_BOT is off."""
    require_secret_or_site_admin_web()
    st = run_bot_thread("api")
    if st == "disabled":
        return (
            jsonify(
                {"ok": False, "status": "disabled", "error": "DISABLE_TELEGRAM_BOT is set on server"}
            ),
            400,
        )
    if st == "no_token":
        return (
            jsonify({"ok": False, "status": "no_token", "error": "TELEGRAM_BOT_TOKEN is not set"}),
            400,
        )
    if st == "already_running":
        return jsonify(
            {
                "ok": True,
                "status": "already_running",
                "detail": "Bot polling thread is already running on this worker.",
            }
        )
    if st == "not_polling_leader":
        return (
            jsonify(
                {
                    "ok": False,
                    "status": "not_polling_leader",
                    "error": "Another process holds data/telegram_poll.lock or PLUXO_TELEGRAM_POLL=never is set.",
                }
            ),
            409,
        )
    return jsonify(
        {"ok": True, "status": "started", "detail": "Telegram polling thread started on this worker."}
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    idx = resolve_index_html()
    print("-" * 60)
    print("PLUXO")
    print("  Folder:", ROOT_DIR)
    print("  HTML:  ", idx if idx else "(none - add index.html or index (27).html)")
    print("  Open:  http://127.0.0.1:%s/  or  http://localhost:%s/" % (port, port))
    print("  Routes:", ", ".join(sorted(str(r.rule) for r in app.url_map.iter_rules())))
    print("-" * 60)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
