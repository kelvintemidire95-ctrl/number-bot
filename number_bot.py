# pip install "pyTelegramBotAPI>=4.23" flask requests
# Env vars: BOT_TOKEN (required)   ADMINS=8243824380 (default, comma-separate more IDs)
# Everything else is set inside the bot:  /admin
import os, re, time, json, sqlite3, threading, hashlib
from datetime import datetime
import requests, telebot
from telebot import types
from flask import Flask

TOKEN = os.getenv("BOT_TOKEN", "")
ADMINS = [int(x) for x in os.getenv("ADMINS", "8243824380").split(",") if x.strip().isdigit()]
DB_PATH = os.getenv("DB_PATH", "bot.db")
if not TOKEN or not ADMINS:
    raise SystemExit("Set BOT_TOKEN and ADMINS environment variables first.")

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
lock = threading.RLock()
LINE = "━━━━━━━━━━━━━━━━━━━━"
CODES = {"NIGERIA": "234", "EGYPT": "20", "UZBEKISTAN": "998", "AFGHANISTAN": "93", "MALI": "223",
         "TAJIKISTAN": "992", "ITALY": "39", "GHANA": "233", "KENYA": "254", "INDIA": "91",
         "PAKISTAN": "92", "INDONESIA": "62", "BRAZIL": "55", "RUSSIA": "7", "USA": "1"}

def q(sql, args=(), one=False, commit=False):
    with lock:
        cur = db.execute(sql, args)
        if commit:
            db.commit(); return cur
        return cur.fetchone() if one else cur.fetchall()

with lock:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, title TEXT, link TEXT);
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, balance REAL DEFAULT 0, otps INTEGER DEFAULT 0,
      withdrawn REAL DEFAULT 0, pending REAL DEFAULT 0, ref_by INTEGER, ref_paid INTEGER DEFAULT 0,
      verified INTEGER DEFAULT 0, banned INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS numbers(number TEXT PRIMARY KEY, service TEXT, country TEXT, status TEXT DEFAULT 'free',
      holder INTEGER, held_at REAL);
    CREATE TABLE IF NOT EXISTS otps(id INTEGER PRIMARY KEY AUTOINCREMENT, user INTEGER, number TEXT, service TEXT,
      country TEXT, code TEXT, reward REAL, day TEXT);
    CREATE TABLE IF NOT EXISTS wd(id INTEGER PRIMARY KEY AUTOINCREMENT, user INTEGER, amount REAL, address TEXT,
      status TEXT DEFAULT 'pending');
    CREATE TABLE IF NOT EXISTS seen(h TEXT PRIMARY KEY);
    """)

# ================= settings (all editable from /admin) =================
DEFAULTS = {"bot_username": "", "support_user": "", "otp_group_id": "0", "otp_group_link": "",
            "otp_reward": "0.005", "ref_reward": "0.025", "min_wd": "0.1", "per_page": "4", "hold_mins": "20",
            "wd_label": "USDT TRC20 address", "api_on": "0", "api_url": "", "api_key": "", "api_auth": "bearer",
            "api_param": "key", "f_list": "data", "f_number": "number", "f_text": "message", "poll_secs": "4"}
SPEC = {"otp_reward": ("OTP reward $", "f"), "ref_reward": ("Referral reward $", "f"), "min_wd": ("Min withdrawal $", "f"),
        "per_page": ("Numbers per request", "i"), "hold_mins": ("Hold minutes", "i"),
        "support_user": ("Support username (no @)", "s"), "bot_username": ("Bot username (no @)", "s"),
        "wd_label": ("Payout label", "s"), "otp_group_id": ("OTP group ID", "i"), "otp_group_link": ("OTP group link", "s"),
        "api_url": ("API URL", "s"), "api_key": ("API key", "s"), "api_auth": ("Auth: bearer / xkey / param", "s"),
        "api_param": ("Key param name", "s"), "f_list": ("JSON list path", "s"), "f_number": ("Number field", "s"),
        "f_text": ("SMS text field", "s"), "poll_secs": ("Poll seconds", "i")}
API_KEYS = ["api_url", "api_key", "api_auth", "api_param", "f_list", "f_number", "f_text", "poll_secs"]
GRP_KEYS = ["otp_group_id", "otp_group_link"]
SET_KEYS = ["otp_reward", "ref_reward", "min_wd", "per_page", "hold_mins", "support_user", "bot_username", "wd_label"]

def S(k):
    r = q("SELECT v FROM settings WHERE k=?", (k,), one=True)
    return r["v"] if r else DEFAULTS[k]
def setS(k, v): q("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)), commit=True)
def F(k):
    try: return float(S(k))
    except Exception: return 0.0
def I(k):
    try: return int(float(S(k)))
    except Exception: return 0

_me = {}
def uname():
    if S("bot_username"): return S("bot_username")
    if "u" not in _me: _me["u"] = bot.get_me().username
    return _me["u"]

# ================= helpers =================
def esc(s): return str(s or "").replace("<", "").replace(">", "").replace("&", "")
def today(): return datetime.utcnow().strftime("%Y-%m-%d")
def head(t): return f"✦ {t} ✦\n{LINE}\n"
def btn(t, d=None, **kw): return types.InlineKeyboardButton(t, callback_data=d, **kw)
def safe_send(chat, text, **kw):
    try: return bot.send_message(chat, text, **kw)
    except Exception as e: print("send fail", chat, e)

def main_kb():
    k = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    k.add("📞 Get Number", "💰 Balance", "$ Withdrawal", "📊 Traffic", "👤 Support", "📈 My Stats")
    k.add("🎁 Refer & Earn")
    return k

def missing(uid):
    out = []
    for ch in q("SELECT * FROM channels"):
        try:
            st = bot.get_chat_member(ch["chat_id"], uid).status
            if st not in ("member", "administrator", "creator"): out.append((ch["title"], ch["link"]))
        except Exception as e:
            print("member check failed", ch["chat_id"], e)   # bot not admin there: don't lock users out
    return out

def send_verify(uid, miss):
    k = types.InlineKeyboardMarkup(row_width=1)
    for t, l in miss: k.add(types.InlineKeyboardButton(f"➕ Join {t}", url=l))
    k.add(btn("✅ Verify", "verify"))
    names = "\n".join(f"• <b>{esc(t)}</b>" for t, _ in miss)
    safe_send(uid, head("VERIFY TO CONTINUE") + f"You left or have not joined:\n{names}\n\nJoin back, then tap ✅ <b>Verify</b> 👇", reply_markup=k)

def ensure_user(u):
    if not q("SELECT 1 FROM users WHERE id=?", (u.id,), one=True):
        q("INSERT INTO users(id,name) VALUES(?,?)", (u.id, u.first_name or ""), commit=True)

def gate(fn):
    def w(m):
        if m.chat.type != "private": return
        ensure_user(m.from_user)
        if q("SELECT banned FROM users WHERE id=?", (m.from_user.id,), one=True)["banned"]:
            return safe_send(m.chat.id, "🚫 You are banned.")
        miss = missing(m.from_user.id)
        if miss: return send_verify(m.from_user.id, miss)
        return fn(m)
    return w

# ================= start / verify =================
@bot.message_handler(commands=["start"])
def start(m):
    if m.chat.type != "private": return
    uid = m.from_user.id
    if not q("SELECT 1 FROM users WHERE id=?", (uid,), one=True):
        ref = None
        p = m.text.split()
        if len(p) > 1 and p[1].startswith("ref_") and p[1][4:].isdigit() and int(p[1][4:]) != uid:
            ref = int(p[1][4:])
        q("INSERT INTO users(id,name,ref_by) VALUES(?,?,?)", (uid, m.from_user.first_name or "", ref), commit=True)
    if q("SELECT banned FROM users WHERE id=?", (uid,), one=True)["banned"]:
        return safe_send(uid, "🚫 You are banned.")
    safe_send(uid, head("WELCOME") + f"👋 Hello, <b>{esc(m.from_user.first_name)}</b>!\nWelcome to the official <b>Unified Number Bot</b>\n\n"
              + head("DASHBOARD") + "Choose an option from the keyboard below 👇", reply_markup=main_kb())
    miss = missing(uid)
    if miss: send_verify(uid, miss)
    else: do_verified(uid)

def do_verified(uid):
    u = q("SELECT * FROM users WHERE id=?", (uid,), one=True)
    if not u["verified"]:
        q("UPDATE users SET verified=1 WHERE id=?", (uid,), commit=True)
        if u["ref_by"] and not u["ref_paid"]:
            q("UPDATE users SET ref_paid=1 WHERE id=?", (uid,), commit=True)
            q("UPDATE users SET balance=balance+? WHERE id=?", (F("ref_reward"), u["ref_by"]), commit=True)
            safe_send(u["ref_by"], f"🎉 A friend joined with your link. You earned <b>${F('ref_reward'):.3f}</b>")
    safe_send(uid, head("VERIFIED") + "✅ You are verified. Enjoy!", reply_markup=main_kb())

@bot.callback_query_handler(func=lambda c: c.data == "verify")
def verify(c):
    miss = missing(c.from_user.id)
    if miss: return bot.answer_callback_query(c.id, "❌ Join all groups first", show_alert=True)
    bot.answer_callback_query(c.id)
    try: bot.delete_message(c.message.chat.id, c.message.message_id)
    except Exception: pass
    ensure_user(c.from_user)
    do_verified(c.from_user.id)

# ================= get number =================
def svc_filter(svc): return ("", ()) if svc == "ALL" else (" AND service=?", (svc,))

@bot.message_handler(func=lambda m: m.text == "📞 Get Number")
@gate
def get_number(m): send_services(m.chat.id)

def send_services(chat):
    rows = q("SELECT DISTINCT service FROM numbers WHERE status='free' ORDER BY service")
    if not rows: return safe_send(chat, head("NO STOCK") + "No numbers available right now. Please check back soon.")
    k = types.InlineKeyboardMarkup(row_width=1)
    k.add(btn("🔷 ALL IN ONE", "svc:ALL"))
    for r in rows: k.add(btn(f"🔷 {r['service']}", f"svc:{r['service']}"))
    safe_send(chat, head("SELECT SERVICE") + "📞 Choose a service below 👇", reply_markup=k)

@bot.callback_query_handler(func=lambda c: c.data.startswith("svc:") or c.data == "back_svc")
def pick_service(c):
    bot.answer_callback_query(c.id)
    chat = c.message.chat.id
    if c.data == "back_svc":
        try: bot.delete_message(chat, c.message.message_id)
        except Exception: pass
        return send_services(chat)
    svc = c.data[4:]
    f, a = svc_filter(svc)
    rows = q(f"SELECT country, COUNT(*) n FROM numbers WHERE status='free'{f} GROUP BY country", a)
    if not rows: return safe_send(chat, "❌ No stock for this service right now.")
    k = types.InlineKeyboardMarkup(row_width=1)
    for r in rows: k.add(btn(f"🌍 {r['country']} ({r['n']})", f"cty:{svc}:{r['country']}"))
    k.add(btn("⬅️ Back to Services", "back_svc"))
    safe_send(chat, head("SELECT COUNTRY") + f"📞 SERVICE : <b>{svc.replace('ALL', 'ALL IN ONE')}</b>\n\n🌍 Choose a country 👇", reply_markup=k)

def release(uid): q("UPDATE numbers SET status='free', holder=NULL, held_at=NULL WHERE holder=? AND status='held'", (uid,), commit=True)

def assign(uid, svc, cty):
    release(uid)
    f, a = svc_filter(svc)
    with lock:
        rows = db.execute(f"SELECT number FROM numbers WHERE status='free' AND country=?{f} ORDER BY RANDOM() LIMIT ?",
                          (cty,) + a + (max(1, I("per_page")),)).fetchall()
        for r in rows:
            db.execute("UPDATE numbers SET status='held', holder=?, held_at=? WHERE number=?", (uid, time.time(), r["number"]))
        db.commit()
    return len(rows)

def show_numbers(chat, uid, svc, cty, strip, edit_id=None):
    nums = q("SELECT number FROM numbers WHERE holder=? AND status='held'", (uid,))
    f, a = svc_filter(svc)
    stock = q(f"SELECT COUNT(*) n FROM numbers WHERE status='free' AND country=?{f}", (cty,) + a, one=True)["n"]
    code = CODES.get(cty, "")
    k = types.InlineKeyboardMarkup(row_width=1)
    for r in nums:
        n = r["number"]
        shown = n[len(code):] if strip and code and n.startswith(code) else n
        k.add(types.InlineKeyboardButton(f"📋 {shown}", copy_text=types.CopyTextButton(text=shown)))
    k.row(btn("🔄 Change Number", f"chg:{svc}:{cty}:{strip}"), btn("Add Code" if strip else "Remove Code", f"tog:{svc}:{cty}:{0 if strip else 1}"))
    row = [btn("⬅️ Back", "back_svc")]
    if S("otp_group_link"): row.append(types.InlineKeyboardButton("💬 Otp Group", url=S("otp_group_link")))
    k.row(*row)
    if S("support_user"): k.add(types.InlineKeyboardButton("🛠 Support", url=f"https://t.me/{S('support_user')}"))
    txt = (head("YOUR NUMBERS") + f"📞 SERVICE : <b>{svc.replace('ALL', 'ALL IN ONE')}</b>\n🌍 COUNTRY : <b>{cty}</b>\n📦 Stock left : <b>{stock}</b>\n\n"
           + head("TAP A NUMBER TO COPY") + "🎉 Use them to create or verify accounts on any app 👇\n⏳ Your OTP will arrive here automatically.")
    if edit_id:
        try: return bot.edit_message_text(txt, chat, edit_id, reply_markup=k)
        except Exception: return
    safe_send(chat, txt, reply_markup=k)

@bot.callback_query_handler(func=lambda c: c.data.split(":")[0] in ("cty", "chg", "tog"))
def numbers_cb(c):
    ensure_user(c.from_user)
    if q("SELECT banned FROM users WHERE id=?", (c.from_user.id,), one=True)["banned"]:
        return bot.answer_callback_query(c.id, "Banned", show_alert=True)
    kind, svc, cty, *rest = c.data.split(":")
    strip = int(rest[0]) if rest else 0
    uid = c.from_user.id
    if kind in ("cty", "chg") and assign(uid, svc, cty) == 0:
        return bot.answer_callback_query(c.id, "❌ No stock for this country right now", show_alert=True)
    bot.answer_callback_query(c.id)
    if kind == "cty": show_numbers(c.message.chat.id, uid, svc, cty, 0)
    else: show_numbers(c.message.chat.id, uid, svc, cty, strip, c.message.message_id)

# ================= OTP processing (group feed + API) =================
def process_sms(text, from_api=False):
    flat = re.sub(r"[\s\-+]", "", text)
    for r in q("SELECT * FROM numbers WHERE status='held'"):
        n = r["number"]
        if not (n in flat or (n[-4:] in flat and n[:3] in flat)): continue
        body = re.sub(r"\+?\d[\d*•xX\s\-]{8,}\d", " ", text)
        mm = re.search(r"(?i)(?:code|otp|pin|password|is|:)\D{0,14}(\d{3}[- ]?\d{3}|\d{4,8})\b", body) \
             or re.search(r"\b(\d{3}[- ]\d{3}|\d{4,8})\b", body)
        if not mm: continue
        code = re.sub(r"[- ]", "", mm.group(1))
        uid, rew = r["holder"], F("otp_reward")
        q("UPDATE numbers SET status='used' WHERE number=?", (n,), commit=True)
        q("UPDATE users SET balance=balance+?, otps=otps+1 WHERE id=?", (rew, uid), commit=True)
        q("INSERT INTO otps(user,number,service,country,code,reward,day) VALUES(?,?,?,?,?,?,?)",
          (uid, n, r["service"], r["country"], code, rew, today()), commit=True)
        gid = I("otp_group_id")
        if from_api and gid:
            safe_send(gid, f"📩 <b>{esc(r['service'])}</b> | {esc(r['country'])}\n📞 <code>{n[:5]}***{n[-4:]}</code>\n🔑 Code : <code>{code}</code>")
        safe_send(uid, head("OTP RECEIVED") + f"📞 Number : <code>{n}</code>\n🔑 Code : <code>{code}</code>\n💵 Earned : <b>${rew:.3f}</b>")
        return True
    return False

@bot.message_handler(func=lambda m: str(m.chat.id) == S("otp_group_id"), content_types=["text"])
@bot.channel_post_handler(func=lambda m: str(m.chat.id) == S("otp_group_id"), content_types=["text"])
def otp_in(m): process_sms(m.text or "")

def dig(o, path):
    for p in [x for x in str(path).split(".") if x]:
        if isinstance(o, list):
            try: o = o[int(p)]
            except Exception: return None
        elif isinstance(o, dict): o = o.get(p)
        else: return None
        if o is None: return None
    return o

def fetch_sms(raw=False):
    key, auth = S("api_key"), S("api_auth")
    headers, params = {}, {}
    if auth == "bearer": headers["Authorization"] = f"Bearer {key}"
    elif auth == "xkey": headers["X-API-Key"] = key
    else: params[S("api_param")] = key
    r = requests.get(S("api_url"), headers=headers, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    items = dig(data, S("f_list")) if S("f_list") else data
    if isinstance(items, dict): items = [items]
    items = items if isinstance(items, list) else []
    out = []
    for x in items:
        n, t = dig(x, S("f_number")), dig(x, S("f_text"))
        if n and t: out.append((re.sub(r"\D", "", str(n)) or str(n), str(t)))
    return (data, items, out) if raw else out

def api_poller():
    while True:
        try:
            if S("api_on") == "1" and S("api_url"):
                for n, t in fetch_sms():
                    h = hashlib.md5(f"{n}|{t}".encode()).hexdigest()
                    if q("SELECT 1 FROM seen WHERE h=?", (h,), one=True): continue
                    q("INSERT INTO seen(h) VALUES(?)", (h,), commit=True)
                    process_sms(f"{n}\n{t}", from_api=True)
        except Exception as e:
            print("API poll error:", e)
        time.sleep(max(2, I("poll_secs")))

def janitor():
    while True:
        try:
            q("UPDATE numbers SET status='free', holder=NULL, held_at=NULL WHERE status='held' AND held_at < ?",
              (time.time() - max(1, I("hold_mins")) * 60,), commit=True)
        except Exception as e: print("janitor", e)
        time.sleep(60)

# ================= user menu =================
@bot.message_handler(func=lambda m: m.text == "💰 Balance")
@gate
def balance(m):
    u = q("SELECT * FROM users WHERE id=?", (m.from_user.id,), one=True)
    safe_send(m.chat.id, "<b>YOUR BALANCE</b>\n" + LINE + f"\n🆔 User ID : <code>{u['id']}</code>\n💵 Balance : <b>${u['balance']:.3f}</b>\n✅ Completed OTPs : <b>{u['otps']}</b>")

@bot.message_handler(func=lambda m: m.text == "$ Withdrawal")
@gate
def withdraw(m):
    u = q("SELECT * FROM users WHERE id=?", (m.from_user.id,), one=True)
    if u["balance"] < F("min_wd") or u["balance"] <= 0:
        return safe_send(m.chat.id, head("LOW BALANCE") + f"💰 Balance : <b>${u['balance']:.3f}</b>\n⚙️ Minimum : <b>${F('min_wd'):.3f}</b>")
    s = safe_send(m.chat.id, head("WITHDRAWAL") + f"💰 Amount : <b>${u['balance']:.3f}</b>\n\nSend your {esc(S('wd_label'))} 👇")
    bot.register_next_step_handler(s, wd_addr)

def wd_addr(m):
    if not m.text or m.text.startswith("/") or len(m.text) < 6:
        return safe_send(m.chat.id, "❌ Invalid. Tap $ Withdrawal and try again.")
    with lock:
        u = q("SELECT * FROM users WHERE id=?", (m.from_user.id,), one=True)
        amt = u["balance"]
        if amt < F("min_wd") or amt <= 0: return
        q("UPDATE users SET balance=0, pending=pending+? WHERE id=?", (amt, u["id"]), commit=True)
        wid = q("INSERT INTO wd(user,amount,address) VALUES(?,?,?)", (u["id"], amt, m.text.strip()), commit=True).lastrowid
    safe_send(m.chat.id, head("REQUEST SENT") + f"⏳ Withdrawal #{wid} of <b>${amt:.3f}</b> is pending approval.")
    for a in ADMINS: wd_card(a, wid)

def wd_card(chat, wid):
    w = q("SELECT * FROM wd WHERE id=?", (wid,), one=True)
    k = types.InlineKeyboardMarkup().row(btn("✅ Approve", f"wd:ok:{wid}"), btn("❌ Reject", f"wd:no:{wid}"))
    safe_send(chat, f"💸 <b>Withdrawal #{wid}</b>\nUser: <code>{w['user']}</code>\nAmount: ${w['amount']:.3f}\nAddress: <code>{esc(w['address'])}</code>", reply_markup=k)

@bot.callback_query_handler(func=lambda c: c.data.startswith("wd:"))
def wd_admin(c):
    if c.from_user.id not in ADMINS: return bot.answer_callback_query(c.id, "Not allowed")
    _, act, wid = c.data.split(":")
    w = q("SELECT * FROM wd WHERE id=?", (int(wid),), one=True)
    if not w or w["status"] != "pending": return bot.answer_callback_query(c.id, "Already handled")
    if act == "ok":
        q("UPDATE wd SET status='paid' WHERE id=?", (w["id"],), commit=True)
        q("UPDATE users SET pending=pending-?, withdrawn=withdrawn+? WHERE id=?", (w["amount"], w["amount"], w["user"]), commit=True)
        safe_send(w["user"], f"✅ Withdrawal #{w['id']} of ${w['amount']:.3f} was paid.")
    else:
        q("UPDATE wd SET status='rejected' WHERE id=?", (w["id"],), commit=True)
        q("UPDATE users SET pending=pending-?, balance=balance+? WHERE id=?", (w["amount"], w["amount"], w["user"]), commit=True)
        safe_send(w["user"], f"❌ Withdrawal #{w['id']} was rejected. Amount returned to your balance.")
    bot.answer_callback_query(c.id, "Done")
    try: bot.edit_message_text(c.message.text + f"\n\n{act.upper()}", c.message.chat.id, c.message.message_id)
    except Exception: pass

def traffic_text():
    sv = q("SELECT service, COUNT(*) n FROM otps WHERE day=? GROUP BY service ORDER BY n DESC LIMIT 10", (today(),))
    ct = q("SELECT country, COUNT(*) n FROM otps WHERE day=? GROUP BY country ORDER BY n DESC LIMIT 6", (t