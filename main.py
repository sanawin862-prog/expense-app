import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from datetime import date
from html import escape

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_PG = bool(DATABASE_URL)

if USE_PG:
    import psycopg2
    from psycopg2.extras import RealDictCursor

app = FastAPI()
DB = "expense.db"
SESSION_DAYS = 30


class DBConn:
    def __init__(self, raw_conn, is_pg: bool):
        self._conn = raw_conn
        self._is_pg = is_pg

    def _sql(self, query: str) -> str:
        if not self._is_pg:
            return query
        q = query.replace("?", "%s")
        q = q.replace("INSERT OR IGNORE INTO", "INSERT INTO")
        if "INSERT INTO members" in q and "ON CONFLICT" not in q:
            q = q.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        return q

    def execute(self, query: str, params=()):
        cur = self._conn.cursor()
        cur.execute(self._sql(query), params)
        return cur

    def executescript(self, script: str):
        if not self._is_pg:
            self._conn.executescript(script)
            return
        cur = self._conn.cursor()
        for stmt in script.split(";"):
            s = stmt.strip()
            if s:
                cur.execute(s)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        self._conn.close()


def db():
    if USE_PG:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        return DBConn(conn, True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return DBConn(con, False)


def init_db():
    with db() as con:
        if USE_PG:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS entries(
                    id SERIAL PRIMARY KEY,
                    day TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    category TEXT NOT NULL,
                    amount DOUBLE PRECISION NOT NULL,
                    note TEXT DEFAULT '',
                    book_id INTEGER,
                    user_id INTEGER
                );
                CREATE TABLE IF NOT EXISTS users(
                    id SERIAL PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    display TEXT NOT NULL,
                    salt TEXT NOT NULL,
                    pw TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions(
                    token TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS books(
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    code TEXT UNIQUE NOT NULL
                );
                CREATE TABLE IF NOT EXISTS members(
                    book_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    PRIMARY KEY(book_id, user_id)
                );
                """
            )
            con.execute(
                "ALTER TABLE entries ADD COLUMN IF NOT EXISTS book_id INTEGER"
            )
            con.execute(
                "ALTER TABLE entries ADD COLUMN IF NOT EXISTS user_id INTEGER"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_entries_book_day ON entries(book_id, day)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token)"
            )
        else:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS entries(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    day TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    category TEXT NOT NULL,
                    amount REAL NOT NULL,
                    note TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS users(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    display TEXT NOT NULL,
                    salt TEXT NOT NULL,
                    pw TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions(
                    token TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS books(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    code TEXT UNIQUE NOT NULL
                );
                CREATE TABLE IF NOT EXISTS members(
                    book_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    PRIMARY KEY(book_id, user_id)
                );
                """
            )
            cols = [r[1] for r in con.execute("PRAGMA table_info(entries)").fetchall()]
            if "book_id" not in cols:
                con.execute("ALTER TABLE entries ADD COLUMN book_id INTEGER")
            if "user_id" not in cols:
                con.execute("ALTER TABLE entries ADD COLUMN user_id INTEGER")

        con.execute("UPDATE entries SET category='ค่าน้ำมัน' WHERE category='เดินทาง'")


init_db()

# หมวดหมู่ + อีโมจิ (เพิ่มหมวดใหม่ได้ที่นี่)
CATEGORIES = {
    "อาหาร": "🍜",
    "ค่าน้ำมัน": "⛽",
    "ที่พัก": "🏠",
    "ช้อปปิ้ง": "🛍️",
    "ค่าน้ำ/ไฟ/เน็ต": "💡",
    "สุขภาพ": "💊",
    "บันเทิง": "🎮",
    "เงินเดือน": "💰",
    "รายได้เสริม": "🌟",
    "อื่นๆ": "🎀",
}

THAI_MONTHS = [
    "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
    "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม",
]
THAI_SHORT = [
    "ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
    "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค.",
]
PALETTE = [
    "#ff8fb3", "#6fdcb8", "#ffd873", "#b9a7ff", "#8ecbff",
    "#ffb38a", "#c5e384", "#f7a8e8", "#9ee6ee", "#d9c3a5",
]


# ---------------------------------------------------------------- helpers
def valid_month(s: str) -> bool:
    try:
        return len(s) == 7 and s[4] == "-" and 1 <= int(s[5:7]) <= 12 and int(s[:4]) >= 1900
    except ValueError:
        return False


def shift(ym: str, n: int) -> str:
    t = int(ym[:4]) * 12 + int(ym[5:7]) - 1 + n
    return f"{t // 12:04d}-{t % 12 + 1:02d}"


def month_name(ym: str) -> str:
    return f"{THAI_MONTHS[int(ym[5:7]) - 1]} {ym[:4]}"


def hash_pw(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt_hex), 200_000
    ).hex()


def new_code(con) -> str:
    while True:
        code = secrets.token_hex(3).upper()
        if not con.execute("SELECT 1 FROM books WHERE code=?", (code,)).fetchone():
            return code


def create_book(con, name: str, user_id: int) -> int:
    code = new_code(con)
    if USE_PG:
        row = con.execute(
            "INSERT INTO books(name, code) VALUES (?,?) RETURNING id", (name, code)
        ).fetchone()
        book_id = row["id"]
    else:
        cur = con.execute(
            "INSERT INTO books(name, code) VALUES (?,?)", (name, code)
        )
        book_id = cur.lastrowid
    con.execute("INSERT INTO members(book_id, user_id) VALUES (?,?)", (book_id, user_id))
    return book_id


def current_user(request: Request):
    token = request.cookies.get("sid")
    if not token:
        return None
    with db() as con:
        return con.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token=?",
            (token,),
        ).fetchone()


def is_member(con, user_id: int, book_id: int) -> bool:
    return bool(
        con.execute(
            "SELECT 1 FROM members WHERE book_id=? AND user_id=?", (book_id, user_id)
        ).fetchone()
    )


def start_session(resp, user_id: int):
    token = secrets.token_urlsafe(32)
    with db() as con:
        con.execute("INSERT INTO sessions(token, user_id) VALUES (?,?)", (token, user_id))
    resp.set_cookie(
        "sid", token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * SESSION_DAYS
    )
    return resp


# ---------------------------------------------------------------- style
CSS = """
:root{
  --bg:#fff6fa; --ink:#4b3a5e; --muted:#9a8bab;
  --pink:#ff8fb3; --pink-soft:#ffe0ec;
  --mint:#6fdcb8; --mint-soft:#d9f7ec;
  --butter:#ffd873; --butter-soft:#fff1c9;
  --lav:#b9a7ff; --lav-soft:#ece6ff;
}
*{box-sizing:border-box}
body{
  margin:0;padding:20px 16px 48px;color:var(--ink);
  font-family:'Mali','Noto Sans Thai',system-ui,sans-serif;
  background-color:var(--bg);
  background-image:radial-gradient(#ffd3e4 2px,transparent 2px);
  background-size:26px 26px;
}
.wrap{max-width:720px;margin:0 auto}
header{text-align:center;margin-bottom:14px}
header .pig{font-size:56px;display:inline-block;transition:transform .3s}
header .pig:hover{transform:rotate(-12deg) scale(1.1)}
h1{margin:4px 0 2px;font-size:28px;font-weight:700}
header p{margin:0;color:var(--muted);font-size:15px}
.hello{display:flex;justify-content:center;align-items:center;gap:10px;font-size:14px;color:var(--muted);margin-top:6px}
.hello form{display:inline}
.hello button{font:inherit;border:0;background:none;color:#d6457a;cursor:pointer;text-decoration:underline}

.chips{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;margin:10px 0 2px}
.chip{
  padding:7px 16px;border-radius:999px;border:3px solid var(--lav);background:#fff;
  color:var(--ink);text-decoration:none;font-size:14px;font-weight:600;
}
.chip.sel{background:var(--lav);color:#fff}
.notice{
  background:var(--butter-soft);border:3px solid var(--butter);border-radius:18px;
  padding:10px 14px;margin:12px 0;text-align:center;
}
.notice.err{background:var(--pink-soft);border-color:var(--pink)}

.monthnav{display:flex;justify-content:center;align-items:center;gap:10px;margin:14px 0}
.monthnav a{
  width:40px;height:40px;border-radius:50%;display:grid;place-items:center;
  background:#fff;border:3px solid var(--lav);color:var(--ink);text-decoration:none;
  font-size:16px;box-shadow:0 4px 0 var(--lav-soft);
}
.monthnav a:hover{background:var(--lav-soft)}
.monthnav input{
  font:inherit;color:var(--ink);background:#fff;border:3px solid var(--lav);
  border-radius:999px;padding:8px 18px;box-shadow:0 4px 0 var(--lav-soft);
}

.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:10px 0 20px}
.card{
  border-radius:24px;padding:14px 8px;text-align:center;border:3px solid;
  font-size:14px;font-weight:600;
}
.card .em{font-size:26px;display:block}
.card b{display:block;font-size:19px;margin-top:4px;word-break:break-all}
.card.in{background:var(--mint-soft);border-color:var(--mint);box-shadow:0 5px 0 var(--mint)}
.card.out{background:var(--pink-soft);border-color:var(--pink);box-shadow:0 5px 0 var(--pink)}
.card.bal{background:var(--butter-soft);border-color:var(--butter);box-shadow:0 5px 0 var(--butter)}

form.add{
  background:#fff;border:3px solid var(--lav);border-radius:28px;padding:18px;
  box-shadow:0 6px 0 var(--lav);display:grid;grid-template-columns:1fr 1fr;gap:10px;
}
form.add h2{grid-column:1/3;margin:0;font-size:18px}
form.add .full{grid-column:1/3}
.kind{grid-column:1/3;display:grid;grid-template-columns:1fr 1fr;gap:10px}
.kind input{position:absolute;opacity:0}
.kind label{
  text-align:center;padding:12px;border-radius:999px;border:3px solid;cursor:pointer;
  font-weight:700;background:#fff;
}
.kind .k-out{border-color:var(--pink);color:#d6457a}
.kind .k-in{border-color:var(--mint);color:#1f9a76}
.kind input:checked + .k-out{background:var(--pink);color:#fff}
.kind input:checked + .k-in{background:var(--mint);color:#fff}
.kind input:focus-visible + label{outline:3px solid var(--ink);outline-offset:2px}
form.add input[type=date],form.add input[type=number],form.add input[type=text],
form.add input[type=password],form.add select,.two input{
  font:inherit;color:var(--ink);padding:11px 14px;border:3px solid var(--lav-soft);
  border-radius:16px;background:#fcfaff;width:100%;
}
form.add input:focus,form.add select:focus,.two input:focus{outline:none;border-color:var(--lav)}
form.add button{
  font:inherit;font-weight:700;font-size:17px;color:#fff;background:var(--pink);
  border:0;border-radius:999px;padding:13px;cursor:pointer;box-shadow:0 5px 0 #e0709a;
}
form.add button:active{transform:translateY(3px);box-shadow:0 2px 0 #e0709a}
.switch{text-align:center;margin-top:14px;font-size:14px}
.switch a{color:#d6457a}
.auth{max-width:400px;margin:24px auto}

.panel{
  background:#fff;border:3px solid var(--butter);border-radius:28px;padding:16px 18px 18px;
  box-shadow:0 6px 0 var(--butter);margin-top:22px;
}
.panel h2{margin:0 0 12px;font-size:18px}
.panel p{margin:6px 0;font-size:14px}
.code{font-size:22px;letter-spacing:3px;background:var(--lav-soft);border-radius:12px;padding:2px 12px}
.two{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px}
.two form{display:grid;gap:8px;align-content:start}
.two button{
  font:inherit;font-weight:700;color:#fff;background:var(--lav);border:0;
  border-radius:999px;padding:10px;cursor:pointer;
}
.donutbox{display:flex;flex-wrap:wrap;gap:18px;align-items:center;justify-content:center}
.donut{width:170px;height:170px;border-radius:50%;position:relative;flex:none}
.hole{
  position:absolute;inset:34px;background:#fff;border-radius:50%;
  display:grid;place-content:center;text-align:center;font-size:12px;color:var(--muted);
}
.hole b{display:block;font-size:16px;color:var(--ink)}
.legend{flex:1;min-width:190px;display:grid;gap:7px}
.lg{display:flex;align-items:center;gap:8px;font-size:14px}
.lg i{width:14px;height:14px;border-radius:50%;flex:none}
.lg span{flex:1}
.lg em{font-style:normal;color:var(--muted);font-size:13px;white-space:nowrap}

.mbars{display:grid;grid-template-columns:repeat(6,1fr);gap:6px}
.mcol{
  display:flex;flex-direction:column;align-items:center;gap:2px;text-decoration:none;
  color:var(--ink);padding:8px 2px 6px;border-radius:18px;border:3px solid transparent;font-size:13px;
}
.mcol:hover{background:var(--butter-soft)}
.mcol.sel{background:var(--lav-soft);border-color:var(--lav)}
.bars{height:116px;display:flex;align-items:flex-end;justify-content:center;gap:4px}
.b{display:block;width:15px;border-radius:8px 8px 0 0}
.b.in{background:var(--mint)}
.b.out{background:var(--pink)}
.mcol small{color:var(--muted);font-size:11px}
.key{display:flex;justify-content:center;gap:16px;margin-top:8px;font-size:13px;color:var(--muted)}

h2.list-title{margin:26px 4px 10px;font-size:18px}
.item{
  display:flex;align-items:center;gap:12px;background:#fff;border-radius:22px;
  padding:10px 14px;margin-bottom:10px;border:3px solid var(--lav-soft);
}
.bubble{
  width:46px;height:46px;border-radius:50%;display:grid;place-items:center;
  font-size:24px;flex:none;
}
.item.out .bubble{background:var(--pink-soft)}
.item.in .bubble{background:var(--mint-soft)}
.info{flex:1;min-width:0}
.info b{display:block;font-size:16px}
.info span{color:var(--muted);font-size:13px;overflow-wrap:anywhere}
.amt{font-weight:700;font-size:17px;white-space:nowrap}
.item.out .amt{color:#d6457a}
.item.in .amt{color:#1f9a76}
.del{
  border:0;background:var(--lav-soft);color:var(--muted);width:32px;height:32px;
  border-radius:50%;cursor:pointer;font-size:14px;
}
.del:hover{background:var(--pink-soft);color:#d6457a}
.empty{
  text-align:center;background:#fff;border:3px dashed var(--lav);border-radius:24px;
  padding:26px 16px;color:var(--muted);
}
.empty .em{font-size:38px;display:block}

@media (max-width:480px){
  .cards{gap:8px}
  .card b{font-size:16px}
  form.add{padding:14px}
  .b{width:11px}
  .two{grid-template-columns:1fr}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""

HEAD = """<!doctype html>
<html lang="th"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>สมุดเก็บตังค์</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Mali:wght@400;600;700&display=swap" rel="stylesheet">
<style>%s</style></head><body><div class="wrap">""" % CSS


# ---------------------------------------------------------------- auth pages
def auth_page(mode: str, error: str = "", username: str = "") -> HTMLResponse:
    is_reg = mode == "register"
    err = f"<div class='notice err'>{escape(error)}</div>" if error else ""
    extra = (
        "<input class='full' type='text' name='display' maxlength='30' "
        "placeholder='ชื่อที่ให้คนอื่นเห็น เช่น แม่, พี่นุ่น'>"
        if is_reg
        else ""
    )
    pw_hint = "รหัสผ่าน (อย่างน้อย 6 ตัว)" if is_reg else "รหัสผ่าน"
    button = "สมัครสมาชิก 🎀" if is_reg else "เข้าสู่ระบบ 💖"
    other = (
        "มีบัญชีแล้ว? <a href='/login'>เข้าสู่ระบบ</a>"
        if is_reg
        else "ยังไม่มีบัญชี? <a href='/register'>สมัครสมาชิก</a>"
    )
    return HTMLResponse(
        HEAD
        + f"""
<div class="auth">
<header><span class="pig">🐷</span><h1>สมุดเก็บตังค์</h1><p>จดทุกบาท เก็บทุกยิ้ม</p></header>
{err}
<form class="add" method="post" action="/{mode}">
  <h2>{'✨ สมัครสมาชิก' if is_reg else '🔑 เข้าสู่ระบบ'}</h2>
  <input class="full" type="text" name="username" value="{escape(username)}"
    placeholder="ชื่อผู้ใช้ (a-z, 0-9, _ รวม 3-20 ตัว)" autocomplete="username" required>
  <input class="full" type="password" name="password" placeholder="{pw_hint}"
    autocomplete="{'new-password' if is_reg else 'current-password'}" required>
  {extra}
  <button class="full">{button}</button>
</form>
<p class="switch">{other}</p>
</div></div></body></html>"""
    )


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return auth_page("login")


@app.post("/login")
def login(username: str = Form(...), password: str = Form(...)):
    with db() as con:
        u = con.execute(
            "SELECT * FROM users WHERE username=?", (username.strip().lower(),)
        ).fetchone()
    if not u or not hmac.compare_digest(hash_pw(password, u["salt"]), u["pw"]):
        return auth_page("login", "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง", username)
    return start_session(RedirectResponse("/", status_code=303), u["id"])


@app.get("/register", response_class=HTMLResponse)
def register_page():
    return auth_page("register")


@app.post("/register")
def register(
    username: str = Form(...), password: str = Form(...), display: str = Form("")
):
    username = username.strip().lower()
    display = display.strip() or username
    if not re.fullmatch(r"[a-z0-9_]{3,20}", username):
        return auth_page("register", "ชื่อผู้ใช้ใช้ได้เฉพาะ a-z, 0-9 และ _ ความยาว 3-20 ตัว", username)
    if len(password) < 6:
        return auth_page("register", "รหัสผ่านต้องยาวอย่างน้อย 6 ตัว", username)
    with db() as con:
        if con.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            return auth_page("register", "ชื่อผู้ใช้นี้มีคนใช้แล้ว ลองชื่ออื่นนะ", username)
        first_user = con.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()["cnt"] == 0
        salt = secrets.token_hex(16)
        if USE_PG:
            row = con.execute(
                "INSERT INTO users(username, display, salt, pw) VALUES (?,?,?,?) RETURNING id",
                (username, display[:30], salt, hash_pw(password, salt)),
            ).fetchone()
            uid = row["id"]
        else:
            cur = con.execute(
                "INSERT INTO users(username, display, salt, pw) VALUES (?,?,?,?)",
                (username, display[:30], salt, hash_pw(password, salt)),
            )
            uid = cur.lastrowid
        book_id = create_book(con, f"สมุดของ{display[:30]}", uid)
        if first_user:  # ข้อมูลที่จดไว้ก่อนมีระบบล็อกอิน ให้เป็นของคนแรก
            con.execute(
                "UPDATE entries SET book_id=?, user_id=? WHERE book_id IS NULL",
                (book_id, uid),
            )
    return start_session(RedirectResponse("/", status_code=303), uid)


@app.post("/logout")
def logout(request: Request):
    token = request.cookies.get("sid")
    if token:
        with db() as con:
            con.execute("DELETE FROM sessions WHERE token=?", (token,))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("sid")
    return resp


# ---------------------------------------------------------------- main page
@app.get("/", response_class=HTMLResponse)
def home(request: Request, month: str = "", book: int = 0, notice: str = ""):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    today = date.today()
    this_month = today.strftime("%Y-%m")
    if not valid_month(month):
        month = this_month

    month_expr = "LEFT(day, 7)" if USE_PG else "substr(day, 1, 7)"

    with db() as con:
        books = con.execute(
            "SELECT b.* FROM books b JOIN members m ON m.book_id=b.id "
            "WHERE m.user_id=? ORDER BY b.id",
            (user["id"],),
        ).fetchall()
        cur_book = next((b for b in books if b["id"] == book), books[0])
        bid = cur_book["id"]

        rows = con.execute(
            "SELECT e.*, u.display AS who FROM entries e LEFT JOIN users u ON u.id=e.user_id "
            "WHERE e.book_id=? AND e.day LIKE ? ORDER BY e.day DESC, e.id DESC",
            (bid, month + "%"),
        ).fetchall()

        months = [shift(month, i) for i in range(-5, 1)]
        data = con.execute(
            f"SELECT {month_expr} AS m, kind, SUM(amount) AS s FROM entries "
            f"WHERE book_id=? AND {month_expr} BETWEEN ? AND ? GROUP BY m, kind",
            (bid, months[0], months[-1]),
        ).fetchall()

        members = [
            r["display"]
            for r in con.execute(
                "SELECT u.display FROM members m JOIN users u ON u.id=m.user_id "
                "WHERE m.book_id=? ORDER BY u.id",
                (bid,),
            ).fetchall()
        ]

    income = sum(r["amount"] for r in rows if r["kind"] == "income")
    expense = sum(r["amount"] for r in rows if r["kind"] == "expense")
    q = f"book={bid}"

    # ---------- รายการ ----------
    items = ""
    for r in rows:
        cls = "in" if r["kind"] == "income" else "out"
        sign = "+" if r["kind"] == "income" else "-"
        emoji = CATEGORIES.get(r["category"], "🏷️")
        sub = escape(r["day"])
        if r["who"]:
            sub += " · " + escape(r["who"])
        if r["note"]:
            sub += " · " + escape(r["note"])
        items += (
            f"<div class='item {cls}'>"
            f"<div class='bubble'>{emoji}</div>"
            f"<div class='info'><b>{escape(r['category'])}</b><span>{sub}</span></div>"
            f"<div class='amt'>{sign}{r['amount']:,.2f}</div>"
            f"<form method='post' action='/delete/{r['id']}?month={month}&{q}' "
            f"onsubmit="return confirm('ลบรายการนี้นะ?')">"
            f"<button class='del' title='ลบ'>✕</button></form></div>"
        )
    if not items:
        items = (
            "<div class='empty'><span class='em'>🐣</span>"
            "ยังไม่มีรายการในเดือนนี้<br>เริ่มจดรายการแรกกันเลย!</div>"
        )

    # ---------- กราฟวงกลม ----------
    if expense > 0:
        cat = {}
        for r in rows:
            if r["kind"] == "expense":
                cat[r["category"]] = cat.get(r["category"], 0) + r["amount"]
        stops, legend, acc = [], "", 0.0
        for i, (name, val) in enumerate(sorted(cat.items(), key=lambda x: -x[1])):
            color = PALETTE[i % len(PALETTE)]
            pct = val / expense * 100
            stops.append(f"{color} {acc:.2f}% {acc + pct:.2f}%")
            acc += pct
            emo = CATEGORIES.get(name, "🏷️")
            legend += (
                f"<div class='lg'><i style='background:{color}'></i>"
                f"<span>{emo} {escape(name)}</span>"
                f"<em>{val:,.0f} ({pct:.0f}%)</em></div>"
            )
        donut = (
            "<div class='donutbox'>"
            f"<div class='donut' style='background:conic-gradient({', '.join(stops)})'>"
            f"<div class='hole'>รายจ่ายรวม<b>{expense:,.0f}</b>บาท</div></div>"
            f"<div class='legend'>{legend}</div></div>"
        )
    else:
        donut = (
            "<div class='empty'><span class='em'>🍩</span>"
            "ยังไม่มีรายจ่ายในเดือนนี้ กราฟจะโผล่มาเมื่อมีรายการ</div>"
        )

    # ---------- กราฟแท่ง ----------
    totals = {(d["m"], d["kind"]): d["s"] for d in data}
    mx = max(totals.values(), default=0)

    def h(v):
        return 0 if not v or not mx else max(5, int(v / mx * 110))

    cols = ""
    for m in months:
        inc = totals.get((m, "income"), 0)
        exp = totals.get((m, "expense"), 0)
        sel = " sel" if m == month else ""
        tip = f"{month_name(m)}: รับ {inc:,.0f} / จ่าย {exp:,.0f}"
        cols += (
            f"<a class='mcol{sel}' href='/?month={m}&{q}' title='{tip}'>"
            f"<div class='bars'><i class='b in' style='height:{h(inc)}px'></i>"
            f"<i class='b out' style='height:{h(exp)}px'></i></div>"
            f"<span>{THAI_SHORT[int(m[5:7]) - 1]}</span><small>{m[:4]}</small></a>"
        )

    options = "".join(
        f"<option value='{name}'>{emo} {name}</option>" for name, emo in CATEGORIES.items()
    )
    default_day = today.isoformat() if month == this_month else month + "-01"

    chips = "".join(
        f"<a class='chip{' sel' if b['id'] == bid else ''}' "
        f"href='/?book={b['id']}&month={month}'>📒 {escape(b['name'])}</a>"
        for b in books
    )
    note_html = f"<div class='notice'>{escape(notice)}</div>" if notice else ""

    return HTMLResponse(
        HEAD
        + f"""
<header>
  <span class="pig">🐷</span>
  <h1>สมุดเก็บตังค์</h1>
  <p>จดทุกบาท เก็บทุกยิ้ม</p>
  <div class="hello">สวัสดี {escape(user['display'])} 👋
    <form method="post" action="/logout"><button>ออกจากระบบ</button></form></div>
</header>

<div class="chips">{chips}</div>
{note_html}

<form class="monthnav" method="get">
  <a href="/?month={shift(month, -1)}&{q}" title="เดือนก่อน">◀</a>
  <input type="hidden" name="book" value="{bid}">
  <input type="month" name="month" value="{month}" onchange="this.form.submit()" aria-label="เลือกเดือน">
  <a href="/?month={shift(month, 1)}&{q}" title="เดือนถัดไป">▶</a>
</form>

<div class="cards">
  <div class="card in"><span class="em">🌱</span>รายรับ<b>{income:,.2f}</b></div>
  <div class="card out"><span class="em">🍓</span>รายจ่าย<b>{expense:,.2f}</b></div>
  <div class="card bal"><span class="em">⭐</span>คงเหลือ<b>{income - expense:,.2f}</b></div>
</div>

<form class="add" method="post" action="/add">
  <input type="hidden" name="book" value="{bid}">
  <h2>✏️ จดรายการใหม่</h2>
  <div class="kind">
    <input type="radio" id="k-out" name="kind" value="expense" checked>
    <label for="k-out" class="k-out">🍓 รายจ่าย</label>
    <input type="radio" id="k-in" name="kind" value="income">
    <label for="k-in" class="k-in">🌱 รายรับ</label>
  </div>
  <input type="date" name="day" value="{default_day}" required>
  <select name="category">{options}</select>
  <input class="full" type="number" name="amount" step="0.01" min="0.01" placeholder="จำนวนเงิน (บาท)" required>
  <input class="full" type="text" name="note" placeholder="โน้ตเล็กๆ (ไม่บังคับ)">
  <button class="full">บันทึกเลย 💖</button>
</form>

<div class="panel">
  <h2>🍩 รายจ่ายแยกตามหมวด · {month_name(month)}</h2>
  {donut}
</div>

<div class="panel">
  <h2>📊 เปรียบเทียบรายเดือน (6 เดือนล่าสุด)</h2>
  <div class="mbars">{cols}</div>
  <div class="key"><span>🌱 รายรับ</span><span>🍓 รายจ่าย</span><span>แตะแท่งเพื่อดูเดือนนั้น</span></div>
</div>

<h2 class="list-title">📒 รายการเดือน{THAI_MONTHS[int(month[5:7]) - 1]}</h2>
{items}

<div class="panel">
  <h2>👨‍👩‍👧 สมุดเล่มนี้: {escape(cur_book['name'])}</h2>
  <p>สมาชิก: {escape(', '.join(members))}</p>
  <p>รหัสชวนคนเข้าสมุด: <span class="code">{cur_book['code']}</span></p>
  <p style="color:var(--muted)">ส่งรหัสนี้ให้คนในครอบครัว/ทีม เขาสมัครสมาชิกแล้วกด "เข้าร่วม" ก็จะเห็นและจดสมุดเล่มเดียวกันได้</p>
  <div class="two">
    <form method="post" action="/book/new">
      <input type="text" name="name" maxlength="40" placeholder="ชื่อสมุดใหม่ เช่น บ้านเรา" required>
      <button>➕ สร้างสมุดใหม่</button>
    </form>
    <form method="post" action="/book/join">
      <input type="text" name="code" maxlength="12" placeholder="รหัสเชิญ 6 ตัว" required>
      <button>🔑 เข้าร่วมสมุด</button>
    </form>
  </div>
</div>

</div></body></html>"""
    )


# ---------------------------------------------------------------- actions
@app.post("/add")
def add(
    request: Request,
    book: int = Form(...),
    kind: str = Form(...),
    day: str = Form(...),
    category: str = Form(...),
    amount: float = Form(...),
    note: str = Form(""),
):
    token = request.cookies.get("sid")
    if not token:
        return RedirectResponse("/login", status_code=303)
    with db() as con:
        user = con.execute(
            "SELECT u.id FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token=?",
            (token,),
        ).fetchone()
        if not user:
            return RedirectResponse("/login", status_code=303)
        if is_member(con, user["id"], book) and kind in ("income", "expense") and amount > 0:
            con.execute(
                "INSERT INTO entries(day, kind, category, amount, note, book_id, user_id) "
                "VALUES (?,?,?,?,?,?,?)",
                (day, kind, category.strip(), amount, note.strip(), book, user["id"]),
            )
    return RedirectResponse(f"/?month={day[:7]}&book={book}", status_code=303)


@app.post("/delete/{entry_id}")
def delete(request: Request, entry_id: int, month: str = "", book: int = 0):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    with db() as con:
        e = con.execute("SELECT book_id FROM entries WHERE id=?", (entry_id,)).fetchone()
        if e and e["book_id"] and is_member(con, user["id"], e["book_id"]):
            con.execute("DELETE FROM entries WHERE id=?", (entry_id,))
    target = f"/?book={book}"
    if valid_month(month):
        target += f"&month={month}"
    return RedirectResponse(target, status_code=303)


@app.post("/book/new")
def book_new(request: Request, name: str = Form(...)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    with db() as con:
        bid = create_book(con, name.strip()[:40] or "สมุดใหม่", user["id"])
    return RedirectResponse(f"/?book={bid}", status_code=303)


@app.post("/book/join")
def book_join(request: Request, code: str = Form(...)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    with db() as con:
        b = con.execute("SELECT * FROM books WHERE code=?", (code.strip().upper(),)).fetchone()
        if not b:
            return RedirectResponse("/?notice=ไม่พบรหัสเชิญนี้ ลองตรวจสอบอีกครั้งนะ", status_code=303)
        con.execute(
            "INSERT OR IGNORE INTO members(book_id, user_id) VALUES (?,?)", (b["id"], user["id"])
        )
    return RedirectResponse(f"/?book={b['id']}&notice=เข้าร่วมสมุดแล้ว 🎉", status_code=303)
