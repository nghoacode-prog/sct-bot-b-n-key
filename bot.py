#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PAWNOVA BOT + WEB ADMIN
- Telegram bot (HTTP Bot API trực tiếp, webhook) + Flask web admin
- Toàn bộ ID / kênh / bank / link kích hoạt đọc từ biến môi trường (xem README trong render.yaml)
"""
import os, io, re, csv, json, time, hmac, base64, hashlib, secrets, sqlite3, threading
import traceback, html, random, tempfile
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from functools import wraps
from urllib.parse import quote

import requests
from flask import (Flask, request, jsonify, session, redirect, send_file,
                   send_from_directory, Response)

# ============================================================
# PHẦN 0: CẤU HÌNH TỪ BIẾN MÔI TRƯỜNG
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
START_TS = time.time()
TZ = timezone(timedelta(hours=7))  # UTC+7 cho mọi thứ


def env(k, d=''):
    return os.environ.get(k, d).strip()


TOKEN = env('TELEGRAM_BOT_TOKEN')
ADMIN_ID = int(env('ADMIN_ID', '0') or 0)
PUBLIC_URL = env('PUBLIC_URL').rstrip('/')
BOT_USERNAME = env('BOT_USERNAME', 'PawBanKey_Bot').lstrip('@')
GROUP_LINK = env('GROUP_LINK', 'https://t.me/pawtoolvip')
DB_PATH = env('DB_PATH', 'bot.db')
PORT = int(env('PORT', '10000') or 10000)
ACTIVATE_URL = env('ACTIVATE_URL', 'https://pawdanhbaynhacaiv1.netlify.app')
SUPPORT_LINK = env('SUPPORT_LINK') or GROUP_LINK
STORE_NAME = env('STORE_NAME', 'PAWNOVA KEY STORE')


def parse_channels():
    """CHANNELS = 'id|link,id|link' ; link bỏ trống thì tự suy: @name -> t.me/name, id số -> GROUP_LINK"""
    raw = env('CHANNELS', '@PawTool789,@PAW8386,@pawtoolvip,-1004376361739')
    out = []
    for part in raw.split(','):
        part = part.strip()
        if not part:
            continue
        cid, _, link = part.partition('|')
        cid, link = cid.strip(), link.strip()
        if not link:
            link = 'https://t.me/' + cid.lstrip('@') if cid.startswith('@') else GROUP_LINK
        out.append((cid, link))
    return out


CHANNELS = parse_channels()

# ---- Hằng số nghiệp vụ ----
PACKAGES = {'1d': ('1 ngày', 22000), '3d': ('3 ngày', 55000),
            '7d': ('7 ngày', 105000), 'life': ('Vĩnh viễn', 270000)}
FREE_TIERS = ['15m', '30m', '1h']
SELL_TIERS = ['1d', '3d', '7d', 'life']
ALL_TIERS = FREE_TIERS + SELL_TIERS
TIER_DURATION = {'15m': 900, '30m': 1800, '1h': 3600, '1d': 86400,
                 '3d': 259200, '7d': 604800, 'life': None}
TIER_NAME = {'15m': '15 phút', '30m': '30 phút', '1h': '1 giờ', '1d': '1 ngày',
             '3d': '3 ngày', '7d': '7 ngày', 'life': 'Vĩnh viễn'}
REDEEM_TIERS = {6: '1h', 15: '1d', 40: '3d', 100: '7d'}
TOPUP_TIERS_DEFAULT = [(100000, 10000), (500000, 100000), (1000000, 250000)]
ARCHIVE_AFTER_HOURS = 3
ORDER_LOCK_SEC = 15
STATE_TTL_MIN = 15
SEP = '━━━━━━━━━━━━━━━━━━'

# Cài đặt mặc định (lấy từ env, sau đó chỉnh được trong Web Admin > Cài đặt)
DEFAULTS = {
    'bank_code': env('BANK_CODE', 'BIDV'),
    'bank_account': env('BANK_ACCOUNT', '8970267100'),
    'bank_name': env('BANK_NAME', 'NGUYEN VAN HOA'),
    'qr_mode': env('QR_MODE', 'static'),          # static | vietqr
    'qr_file_id': '',
    'order_ttl_min': env('ORDER_TTL_MIN', '15'),
    'throttle_sec': env('THROTTLE_SEC', '0.4'),
    'member_cache_sec': env('MEMBER_CACHE_SEC', '300'),
    'streak_days': env('STREAK_DAYS', '7'),
    'streak_reward': env('STREAK_REWARD', '5'),
    'checkin_miss_pct': env('CHECKIN_MISS_PCT', '55'),
    'checkin_miss_point': env('CHECKIN_MISS_POINT', '1'),
    'invite_reward': env('INVITE_REWARD', '3'),
    'auto_backup': '0',
    'price_1d': env('PRICE_1D', '22000'),
    'price_3d': env('PRICE_3D', '55000'),
    'price_7d': env('PRICE_7D', '105000'),
    'price_life': env('PRICE_LIFE', '270000'),
}
EDITABLE_SETTINGS = [k for k in DEFAULTS if k != 'qr_file_id']

PERMS = ['dashboard', 'users', 'keys', 'orders', 'topups', 'coupons', 'admins',
         'broadcast', 'revenue', 'backup', 'settings']
LEVEL_DEFAULT = {'super': PERMS,
                 'admin': ['dashboard', 'users', 'keys', 'orders', 'topups', 'coupons'],
                 'viewer': ['dashboard']}

# ============================================================
# PHẦN 1: DATABASE (SQLite, idempotent)
# ============================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(telegram_id INTEGER PRIMARY KEY, username TEXT DEFAULT '', first_name TEXT DEFAULT '',
  points INTEGER DEFAULT 0, balance INTEGER DEFAULT 0, total_topup INTEGER DEFAULT 0, total_spent INTEGER DEFAULT 0,
  streak_days INTEGER DEFAULT 0, last_checkin TEXT, inviter_id INTEGER, invite_confirmed INTEGER DEFAULT 0,
  banned INTEGER DEFAULT 0, created_at INTEGER);
CREATE TABLE IF NOT EXISTS admins(telegram_id INTEGER PRIMARY KEY, level TEXT DEFAULT 'admin', permissions TEXT,
  added_by INTEGER, added_at INTEGER);
CREATE TABLE IF NOT EXISTS keys_pool(id INTEGER PRIMARY KEY AUTOINCREMENT, tier TEXT NOT NULL, license_key TEXT UNIQUE NOT NULL,
  user_id INTEGER, sold_at INTEGER, expires_at INTEGER, order_ref TEXT, archived INTEGER DEFAULT 0, created_at INTEGER);
CREATE INDEX IF NOT EXISTS idx_keys ON keys_pool(tier, user_id, archived);
CREATE TABLE IF NOT EXISTS orders(ref TEXT PRIMARY KEY, telegram_id INTEGER, package TEXT, amount INTEGER,
  discount INTEGER DEFAULT 0, coupon TEXT, status TEXT DEFAULT 'pending', bill_file_id TEXT, key_sent TEXT,
  created_at INTEGER, expires_at INTEGER, approved_at INTEGER, approved_by INTEGER, method TEXT DEFAULT 'bank');
CREATE TABLE IF NOT EXISTS topups(ref TEXT PRIMARY KEY, telegram_id INTEGER, amount INTEGER, bonus INTEGER DEFAULT 0,
  coupon TEXT, status TEXT DEFAULT 'pending', bill_file_id TEXT, created_at INTEGER, expires_at INTEGER,
  approved_at INTEGER, approved_by INTEGER);
CREATE TABLE IF NOT EXISTS coupons(code TEXT PRIMARY KEY, type TEXT, value INTEGER, min_order INTEGER DEFAULT 0,
  max_uses INTEGER DEFAULT 0, used_count INTEGER DEFAULT 0, expires_at INTEGER, active INTEGER DEFAULT 1,
  created_by INTEGER, created_at INTEGER);
CREATE TABLE IF NOT EXISTS coupon_uses(code TEXT, telegram_id INTEGER, used_at INTEGER, PRIMARY KEY(code, telegram_id));
CREATE TABLE IF NOT EXISTS pending_state(telegram_id INTEGER PRIMARY KEY, action TEXT, meta TEXT, expires_at INTEGER);
CREATE TABLE IF NOT EXISTS pending_discount(telegram_id INTEGER PRIMARY KEY, code TEXT);
CREATE TABLE IF NOT EXISTS topup_tiers(amount INTEGER PRIMARY KEY, bonus INTEGER);
CREATE TABLE IF NOT EXISTS invite_log(id INTEGER PRIMARY KEY AUTOINCREMENT, inviter_id INTEGER,
  invitee_id INTEGER UNIQUE, reward INTEGER, created_at INTEGER);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS points_log(id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER, delta INTEGER,
  reason TEXT, created_at INTEGER);
"""
BACKUP_TABLES = ['users', 'admins', 'keys_pool', 'orders', 'topups', 'coupons', 'coupon_uses',
                 'pending_state', 'pending_discount', 'topup_tiers', 'invite_log', 'settings', 'points_log']


def conn():
    c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA busy_timeout=30000')
    return c


def q(sql, a=()):
    """SELECT nhiều dòng -> list[dict]"""
    c = conn()
    try:
        return [dict(r) for r in c.execute(sql, a).fetchall()]
    finally:
        c.close()


def q1(sql, a=()):
    r = q(sql, a)
    return r[0] if r else None


def ex(sql, a=()):
    c = conn()
    try:
        return c.execute(sql, a).rowcount
    finally:
        c.close()


@contextmanager
def tx():
    """Mọi thao tác ghi quan trọng: BEGIN IMMEDIATE ... COMMIT / ROLLBACK"""
    c = conn()
    try:
        c.execute('BEGIN IMMEDIATE')
        yield c
        c.execute('COMMIT')
    except BaseException:
        try:
            c.execute('ROLLBACK')
        except Exception:
            pass
        raise
    finally:
        c.close()


def c1(c, sql, a=()):
    r = c.execute(sql, a).fetchone()
    return dict(r) if r else None


def init_db():
    """Khởi tạo DB - chạy lại nhiều lần không lỗi"""
    c = conn()
    try:
        c.executescript(SCHEMA)
        try:
            c.execute("ALTER TABLE orders ADD COLUMN method TEXT DEFAULT 'bank'")
        except Exception:
            pass
        if not c.execute('SELECT 1 FROM topup_tiers LIMIT 1').fetchone():
            for a, b in TOPUP_TIERS_DEFAULT:
                c.execute('INSERT OR IGNORE INTO topup_tiers(amount,bonus) VALUES(?,?)', (a, b))
        for k, v in DEFAULTS.items():
            c.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)', (k, v))
        if ADMIN_ID:
            c.execute('INSERT OR IGNORE INTO admins(telegram_id,level,permissions,added_by,added_at) VALUES(?,?,?,?,?)',
                      (ADMIN_ID, 'super', json.dumps(PERMS), ADMIN_ID, int(time.time())))
    finally:
        c.close()


# ---- Settings ----
def sget(k, d=''):
    r = q1('SELECT value FROM settings WHERE key=?', (k,))
    return r['value'] if r else DEFAULTS.get(k, d)


def sint(k):
    try:
        return int(float(sget(k)))
    except Exception:
        return int(float(DEFAULTS.get(k, 0) or 0))


def sfloat(k):
    try:
        return float(sget(k))
    except Exception:
        return float(DEFAULTS.get(k, 0) or 0)


def sset(k, v):
    ex('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (k, str(v)))


def price(tier):
    return sint('price_' + tier)


# ---- Tiện ích định dạng ----
def now():
    return int(time.time())


def today():
    return datetime.now(TZ).strftime('%Y-%m-%d')


def yesterday():
    return (datetime.now(TZ) - timedelta(days=1)).strftime('%Y-%m-%d')


def fmt_ts(ts):
    return datetime.fromtimestamp(ts, TZ).strftime('%d/%m/%Y %H:%M') if ts else '—'


def money(n):
    return f'{int(n or 0):,}'.replace(',', '.') + 'đ'


def esc(s):
    return html.escape(str(s if s is not None else ''))


def mask_key(k):
    return k if len(k) <= 10 else k[:5] + '•••' + k[-4:]


def mask_uid(u):
    s = str(u)
    return s[:4] + '...' + s[-3:] if len(s) > 8 else s


def uname(u):
    return ('@' + u['username']) if u.get('username') else (u.get('first_name') or str(u['telegram_id']))


def new_ref(prefix):
    return prefix + ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(6))


# ---- Quyền admin ----
def get_admin(uid):
    if uid and uid == ADMIN_ID:
        return {'telegram_id': uid, 'level': 'super', 'permissions': PERMS}
    r = q1('SELECT * FROM admins WHERE telegram_id=?', (uid,))
    if not r:
        return None
    try:
        perms = json.loads(r['permissions']) if r['permissions'] else None
    except Exception:
        perms = None
    if r['level'] == 'super':
        perms = PERMS
    elif perms is None:
        perms = LEVEL_DEFAULT.get(r['level'], [])
    r['permissions'] = perms
    return r


def is_admin(uid):
    return get_admin(uid) is not None


def has_perm(uid, p):
    a = get_admin(uid)
    if not a:
        return False
    if a['level'] == 'super':
        return True
    return p != 'admins' and p in a['permissions']


def admin_ids():
    ids = {ADMIN_ID} if ADMIN_ID else set()
    for r in q("SELECT telegram_id FROM admins WHERE level IN ('super','admin')"):
        ids.add(r['telegram_id'])
    return ids


# ============================================================
# PHẦN 2: TELEGRAM HTTP API + BÀN PHÍM + KIỂM TRA KÊNH
# ============================================================
API = f'https://api.telegram.org/bot{TOKEN}'
_http = requests.Session()


def tg(method, files=None, **params):
    """Gọi Bot API. Trả về dict {'ok':..., 'result':...}"""
    try:
        if files:
            data = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in params.items()}
            r = _http.post(f'{API}/{method}', data=data, files=files, timeout=60)
        else:
            r = _http.post(f'{API}/{method}', json=params, timeout=25)
        return r.json()
    except Exception as e:
        print('[tg error]', method, e)
        return {'ok': False, 'description': str(e)}


def split_text(text, n=4000):
    """Cắt text > 4000 ký tự theo dòng"""
    out, cur = [], ''
    for line in text.split('\n'):
        while len(line) > n:
            if cur:
                out.append(cur)
                cur = ''
            out.append(line[:n])
            line = line[n:]
        if len(cur) + len(line) + 1 > n:
            out.append(cur)
            cur = line
        else:
            cur = cur + '\n' + line if cur else line
    out.append(cur)
    return out


def send(chat, text, kb=None, parse='HTML'):
    chunks = split_text(text)
    res = None
    for i, ch in enumerate(chunks):
        p = {'chat_id': chat, 'text': ch, 'disable_web_page_preview': True}
        if parse:
            p['parse_mode'] = parse
        if kb and i == len(chunks) - 1:
            p['reply_markup'] = kb  # markup chỉ gắn vào tin cuối
        r = tg('sendMessage', **p)
        if not r.get('ok') and parse and 'parse' in r.get('description', '').lower():
            p.pop('parse_mode', None)
            r = tg('sendMessage', **p)
        if r.get('ok'):
            res = r['result']
    return res


def show(chat, mid, text, kb=None):
    """Sửa tin cũ nếu có thể, nếu không thì gửi tin mới (không spam khi 'not modified')"""
    if mid:
        r = tg('editMessageText', chat_id=chat, message_id=mid, text=text[:4000], parse_mode='HTML',
               reply_markup=kb or {'inline_keyboard': []}, disable_web_page_preview=True)
        if r.get('ok'):
            return r['result']
        d = r.get('description', '')
        if 'not modified' in d:
            return None
        if 'no text' in d:
            tg('deleteMessage', chat_id=chat, message_id=mid)
    return send(chat, text, kb)


def cb_answer(cid, text='', alert=False):
    tg('answerCallbackQuery', callback_query_id=cid, text=text[:190], show_alert=alert)


def B(text, data=None, url=None, copy=None):
    b = {'text': text}
    if url:
        b['url'] = url
    elif copy:
        b['copy_text'] = {'text': copy}
    else:
        b['callback_data'] = data
    return b


def KB(*rows):
    return {'inline_keyboard': [list(r) for r in rows if r]}


def download_file(file_id):
    r = tg('getFile', file_id=file_id)
    if not r.get('ok'):
        return None
    try:
        return _http.get(f"https://api.telegram.org/file/bot{TOKEN}/{r['result']['file_path']}", timeout=60).content
    except Exception:
        return None


# ---- Kiểm tra thành viên 4 kênh (cache, fail-open khi API lỗi) ----
_mcache = {}


def _chat_id(ch):
    return int(ch) if ch.lstrip('-').isdigit() else ch


def _is_member(uid, ch, force=False):
    k, t = (uid, ch), time.time()
    if not force and k in _mcache and t - _mcache[k][0] < sint('member_cache_sec'):
        return _mcache[k][1]
    r = tg('getChatMember', chat_id=_chat_id(ch), user_id=uid)
    if not r.get('ok'):
        return True  # fail-open: chỉ khi API lỗi
    st = r['result'].get('status')
    ok = st in ('creator', 'administrator', 'member') or (st == 'restricted' and r['result'].get('is_member'))
    _mcache[k] = (t, ok)
    return ok


def missing_channels(uid, force=False):
    return [c for c in CHANNELS if not _is_member(uid, c[0], force)]


def wall(chat, mid, miss, retry_data):
    rows = [[B(f'📢 Tham gia kênh {i + 1}', url=link)] for i, (_, link) in enumerate(miss)]
    rows.append([B('✅ Tôi đã tham gia', retry_data)])
    rows.append([B('🏠 Home', 'home')])
    show(chat, mid, f'🔒 <b>CẦN THAM GIA KÊNH</b>\n{SEP}\nBạn cần tham gia đủ các kênh bên dưới để dùng tính năng này 👇',
         {'inline_keyboard': rows})


# ---- State (chỉ dùng cho: coupon, topup custom, bill ảnh + vài thao tác admin) ----
def set_state(uid, action, meta=''):
    ex('INSERT INTO pending_state(telegram_id,action,meta,expires_at) VALUES(?,?,?,?) '
       'ON CONFLICT(telegram_id) DO UPDATE SET action=excluded.action,meta=excluded.meta,expires_at=excluded.expires_at',
       (uid, action, meta, now() + STATE_TTL_MIN * 60))


def get_state(uid):
    r = q1('SELECT * FROM pending_state WHERE telegram_id=?', (uid,))
    if r and r['expires_at'] < now():
        clear_state(uid)
        return None
    return r


def clear_state(uid):
    ex('DELETE FROM pending_state WHERE telegram_id=?', (uid,))


# ============================================================
# PHẦN 3: NGHIỆP VỤ (user, coupon, key, đơn, nạp, checkin, đổi điểm)
# ============================================================
def get_user(uid):
    return q1('SELECT * FROM users WHERE telegram_id=?', (uid,))


def ensure_user(frm, inviter=None):
    """Đăng ký user; lưu inviter_id nếu hợp lệ. KHÔNG check kênh, KHÔNG cộng điểm."""
    uid = frm['id']
    un, fn = frm.get('username') or '', frm.get('first_name') or ''
    with tx() as c:
        u = c1(c, 'SELECT * FROM users WHERE telegram_id=?', (uid,))
        if not u:
            inv = None
            if inviter and inviter != uid and c1(c, 'SELECT 1 x FROM users WHERE telegram_id=?', (inviter,)):
                inv = inviter
            c.execute('INSERT INTO users(telegram_id,username,first_name,inviter_id,created_at) VALUES(?,?,?,?,?)',
                      (uid, un, fn, inv, now()))
        else:
            c.execute('UPDATE users SET username=?,first_name=? WHERE telegram_id=?', (un, fn, uid))
    return get_user(uid)


def add_points(c, uid, delta, reason):
    c.execute('UPDATE users SET points=MAX(points+?,0) WHERE telegram_id=?', (delta, uid))
    c.execute('INSERT INTO points_log(telegram_id,delta,reason,created_at) VALUES(?,?,?,?)', (uid, delta, reason, now()))


# ---- Coupon ----
def coupon_check(code, uid, amount=None, ctype=None):
    """Kiểm tra đủ: active / hết hạn / hết lượt / đã dùng / min_order. Trả (coupon, lỗi)"""
    cp = q1('SELECT * FROM coupons WHERE code=?', ((code or '').strip().upper(),))
    if not cp or not cp['active']:
        return None, 'Mã không tồn tại hoặc đã bị tắt'
    if cp['expires_at'] and cp['expires_at'] < now():
        return None, 'Mã đã hết hạn'
    if cp['max_uses'] and cp['used_count'] >= cp['max_uses']:
        return None, 'Mã đã hết lượt sử dụng'
    if q1('SELECT 1 x FROM coupon_uses WHERE code=? AND telegram_id=?', (cp['code'], uid)):
        return None, 'Bạn đã dùng mã này rồi'
    if ctype and cp['type'] != ctype:
        return None, 'Mã này không áp dụng cho giao dịch này'
    if amount is not None and amount < (cp['min_order'] or 0):
        return None, f"Cần tối thiểu {money(cp['min_order'])} để dùng mã"
    return cp, None


def coupon_amount(cp, base):
    """value <= 100 => phần trăm, > 100 => số tiền cố định (VNĐ)"""
    v = int(cp['value'])
    return min(base, base * v // 100 if v <= 100 else v)


def coupon_consume(c, code, uid):
    n = c.execute('INSERT OR IGNORE INTO coupon_uses(code,telegram_id,used_at) VALUES(?,?,?)', (code, uid, now())).rowcount
    if n:
        c.execute('UPDATE coupons SET used_count=used_count+1 WHERE code=?', (code,))
    return n


def coupon_release(c, code, uid):
    if code and c.execute('DELETE FROM coupon_uses WHERE code=? AND telegram_id=?', (code, uid)).rowcount:
        c.execute('UPDATE coupons SET used_count=MAX(used_count-1,0) WHERE code=?', (code,))


def pending_coupon(uid, amount=None, ctype='discount'):
    r = q1('SELECT code FROM pending_discount WHERE telegram_id=?', (uid,))
    if not r:
        return None
    cp, err = coupon_check(r['code'], uid, amount, ctype)
    if err and 'không áp dụng' not in err and 'tối thiểu' not in err:
        ex('DELETE FROM pending_discount WHERE telegram_id=?', (uid,))
    return cp


# ---- Key ----
def stock(tiers):
    ph = ','.join('?' * len(tiers))
    return q1(f'SELECT COUNT(*) n FROM keys_pool WHERE tier IN ({ph}) AND user_id IS NULL AND archived=0', tiers)['n']


def pick_key(c, tiers, rnd=False):
    ph = ','.join('?' * len(tiers))
    return c1(c, f'SELECT * FROM keys_pool WHERE tier IN ({ph}) AND user_id IS NULL AND archived=0 '
                 f'ORDER BY {"RANDOM()" if rnd else "id"} LIMIT 1', tiers)


def assign_key(c, k, uid, ref):
    d = TIER_DURATION[k['tier']]
    exp = now() + d if d else None
    n = c.execute('UPDATE keys_pool SET user_id=?,sold_at=?,expires_at=?,order_ref=? WHERE id=? AND user_id IS NULL',
                  (uid, now(), exp, ref, k['id'])).rowcount
    return n > 0, exp


KEY_RE = re.compile(r'^[\w\-.:=+/@#]{4,200}$')


def parse_keys(text):
    return [x for x in re.split(r'[,\s]+', text or '') if x]


def insert_keys(tier, keys):
    """license_key UNIQUE toàn cục: trùng ở bất kỳ tier nào => bỏ qua"""
    ins = dup = inv = 0
    seen = set()
    with tx() as c:
        for k in keys:
            if not KEY_RE.match(k):
                inv += 1
            elif k in seen:
                dup += 1
            else:
                seen.add(k)
                if c.execute('INSERT OR IGNORE INTO keys_pool(tier,license_key,created_at) VALUES(?,?,?)',
                             (tier, k, now())).rowcount:
                    ins += 1
                else:
                    dup += 1
    return {'inserted': ins, 'duplicate': dup, 'invalid': inv}


def archive_old_keys():
    """Archive key đã bán + hết hạn quá 3h (không xoá life, không xoá chưa bán, không hard delete)"""
    return ex('UPDATE keys_pool SET archived=1 WHERE user_id IS NOT NULL AND expires_at IS NOT NULL '
              'AND expires_at<? AND archived=0', (now() - ARCHIVE_AFTER_HOURS * 3600,))


def expire_pending():
    """Huỷ đơn/nạp quá hạn, nhả coupon, xoá state hết hạn"""
    with tx() as c:
        for r in c.execute("SELECT ref,telegram_id,coupon FROM orders WHERE status='pending' AND expires_at<?", (now(),)).fetchall():
            c.execute("UPDATE orders SET status='expired' WHERE ref=?", (r['ref'],))
            coupon_release(c, r['coupon'], r['telegram_id'])
        for r in c.execute("SELECT ref,telegram_id,coupon FROM topups WHERE status='pending' AND expires_at<?", (now(),)).fetchall():
            c.execute("UPDATE topups SET status='expired' WHERE ref=?", (r['ref'],))
            coupon_release(c, r['coupon'], r['telegram_id'])
        c.execute('DELETE FROM pending_state WHERE expires_at<?', (now(),))


# ---- Gửi key cho user ----
def deliver_key(uid, key, tier, exp, title='🎉 NHẬN KEY THÀNH CÔNG', extra=''):
    text = (f'<b>{title}</b>\n{SEP}\n🔑 Key:\n<pre>{esc(key)}</pre>\n'
            f'▸ Gói: {TIER_NAME.get(tier, tier)}\n▸ Hết hạn: {fmt_ts(exp) if exp else "Vĩnh viễn"}\n'
            f'🔗 Kích hoạt tại: {esc(ACTIVATE_URL)}{extra}')
    send(uid, text, KB([B('📋 Copy key', copy=key), B('🔗 Kích hoạt', url=ACTIVATE_URL)], [B('🏠 Home', 'home')]))


# ---- Đơn mua ----
def calc_price(uid, tier):
    base = price(tier)
    cp = pending_coupon(uid, base, 'discount')
    disc = coupon_amount(cp, base) if cp else 0
    final = max(base - disc, 1000) if disc else base
    return base, (base - final if disc else 0), final, cp


def create_order(uid, tier):
    """Tạo đơn CK. Reuse đơn chờ cùng gói tạo < 15s (chống bấm đúp)."""
    if tier not in SELL_TIERS:
        return None, 'Gói không hợp lệ'
    if stock([tier]) == 0:
        return None, 'Gói này đang hết key, Admin đang nạp thêm 🙏'
    dup = q1("SELECT * FROM orders WHERE telegram_id=? AND package=? AND status='pending' AND created_at>? AND method='bank'",
             (uid, tier, now() - ORDER_LOCK_SEC))
    if dup:
        return dup, None
    base, disc, final, cp = calc_price(uid, tier)
    ref = new_ref('PAW')
    with tx() as c:
        c.execute('INSERT INTO orders(ref,telegram_id,package,amount,discount,coupon,status,created_at,expires_at,method) '
                  "VALUES(?,?,?,?,?,?,'pending',?,?, 'bank')",
                  (ref, uid, tier, final, disc, cp['code'] if cp else None, now(), now() + sint('order_ttl_min') * 60))
        if cp:
            coupon_consume(c, cp['code'], uid)
        c.execute('DELETE FROM pending_discount WHERE telegram_id=?', (uid,))
    return q1('SELECT * FROM orders WHERE ref=?', (ref,)), None


def pay_wallet(uid, tier):
    """Trả bằng ví: BEGIN IMMEDIATE -> pick key -> trừ balance -> gửi key"""
    if tier not in SELL_TIERS:
        return None, 'Gói không hợp lệ'
    base, disc, final, cp = calc_price(uid, tier)
    ref = new_ref('W')
    with tx() as c:
        u = c1(c, 'SELECT * FROM users WHERE telegram_id=?', (uid,))
        if u['balance'] < final:
            return None, 'Số dư ví không đủ'
        k = pick_key(c, [tier])
        if not k:
            return None, 'Gói này đang hết key, Admin đang nạp thêm 🙏'
        ok, exp = assign_key(c, k, uid, ref)
        if not ok:
            return None, 'Có lỗi, vui lòng thử lại'
        c.execute('UPDATE users SET balance=balance-?,total_spent=total_spent+? WHERE telegram_id=?', (final, final, uid))
        c.execute("INSERT INTO orders(ref,telegram_id,package,amount,discount,coupon,status,key_sent,created_at,expires_at,"
                  "approved_at,approved_by,method) VALUES(?,?,?,?,?,?,'approved',?,?,?,?,0,'wallet')",
                  (ref, uid, tier, final, disc, cp['code'] if cp else None, k['license_key'], now(), now(), now()))
        if cp:
            coupon_consume(c, cp['code'], uid)
        c.execute('DELETE FROM pending_discount WHERE telegram_id=?', (uid,))
    deliver_key(uid, k['license_key'], tier, exp, '🎉 MUA KEY THÀNH CÔNG', f'\n💰 Đã trừ ví: {money(final)}')
    return ref, None


def approve_order(ref, admin_uid):
    """Duyệt đơn (dùng chung bot + web): pick key -> gán -> gửi key user"""
    with tx() as c:
        o = c1(c, 'SELECT * FROM orders WHERE ref=?', (ref,))
        if not o:
            return False, 'Không thấy đơn'
        if o['status'] not in ('pending', 'expired'):
            return False, f"Đơn đã ở trạng thái {o['status']}"
        k = pick_key(c, [o['package']])
        if not k:
            return False, f"Kho {o['package']} đã hết key"
        ok, exp = assign_key(c, k, o['telegram_id'], ref)
        if not ok:
            return False, 'Lỗi gán key, thử lại'
        c.execute("UPDATE orders SET status='approved',key_sent=?,approved_at=?,approved_by=? WHERE ref=?",
                  (k['license_key'], now(), admin_uid, ref))
        c.execute('UPDATE users SET total_spent=total_spent+? WHERE telegram_id=?', (o['amount'], o['telegram_id']))
        if o['status'] == 'expired' and o['coupon']:
            coupon_consume(c, o['coupon'], o['telegram_id'])
    deliver_key(o['telegram_id'], k['license_key'], o['package'], exp, '🎉 ĐƠN ĐÃ ĐƯỢC DUYỆT', f"\n🧾 Mã đơn: {ref}")
    return True, 'Đã duyệt & gửi key'


def reject_order(ref, admin_uid, reason):
    with tx() as c:
        o = c1(c, 'SELECT * FROM orders WHERE ref=?', (ref,))
        if not o:
            return False, 'Không thấy đơn'
        if o['status'] not in ('pending', 'expired'):
            return False, f"Đơn đã ở trạng thái {o['status']}"
        c.execute("UPDATE orders SET status='rejected',approved_at=?,approved_by=? WHERE ref=?", (now(), admin_uid, ref))
        if o['status'] == 'pending':
            coupon_release(c, o['coupon'], o['telegram_id'])
    send(o['telegram_id'], f"❌ <b>ĐƠN {ref} BỊ TỪ CHỐI</b>\n{SEP}\n▸ Lý do: {esc(reason)}\nCó thắc mắc vui lòng liên hệ hỗ trợ.",
         KB([B('💬 Hỗ trợ', url=SUPPORT_LINK)], [B('🏠 Home', 'home')]))
    return True, 'Đã từ chối'


# ---- Nạp ví ----
def create_topup(uid, amount):
    bonus = 0
    t = q1('SELECT bonus FROM topup_tiers WHERE amount=?', (amount,))
    if t:
        bonus = t['bonus']
    cp = pending_coupon(uid, amount, 'topup')
    if cp:
        bonus += coupon_amount(cp, amount)
    ref = new_ref('TUP')
    with tx() as c:
        c.execute("INSERT INTO topups(ref,telegram_id,amount,bonus,coupon,status,created_at,expires_at) VALUES(?,?,?,?,?,'pending',?,?)",
                  (ref, uid, amount, bonus, cp['code'] if cp else None, now(), now() + sint('order_ttl_min') * 60))
        if cp:
            coupon_consume(c, cp['code'], uid)
            c.execute('DELETE FROM pending_discount WHERE telegram_id=?', (uid,))
    return q1('SELECT * FROM topups WHERE ref=?', (ref,))


def approve_topup(ref, admin_uid):
    with tx() as c:
        t = c1(c, 'SELECT * FROM topups WHERE ref=?', (ref,))
        if not t:
            return False, 'Không thấy giao dịch nạp'
        if t['status'] not in ('pending', 'expired'):
            return False, f"Giao dịch đã ở trạng thái {t['status']}"
        c.execute("UPDATE topups SET status='approved',approved_at=?,approved_by=? WHERE ref=?", (now(), admin_uid, ref))
        c.execute('UPDATE users SET balance=balance+?,total_topup=total_topup+? WHERE telegram_id=?',
                  (t['amount'] + t['bonus'], t['amount'], t['telegram_id']))
        if t['status'] == 'expired' and t['coupon']:
            coupon_consume(c, t['coupon'], t['telegram_id'])
    u = get_user(t['telegram_id'])
    send(t['telegram_id'], f"✅ <b>NẠP VÍ THÀNH CÔNG</b>\n{SEP}\n▸ Nạp: {money(t['amount'])}\n▸ Thưởng: +{money(t['bonus'])}\n"
                           f"▸ Số dư mới: <b>{money(u['balance'])}</b>", KB([B('🛒 Mua Key', 'buy'), B('🏠 Home', 'home')]))
    return True, 'Đã cộng ví'


def reject_topup(ref, admin_uid, reason):
    with tx() as c:
        t = c1(c, 'SELECT * FROM topups WHERE ref=?', (ref,))
        if not t:
            return False, 'Không thấy giao dịch nạp'
        if t['status'] not in ('pending', 'expired'):
            return False, f"Giao dịch đã ở trạng thái {t['status']}"
        c.execute("UPDATE topups SET status='rejected',approved_at=?,approved_by=? WHERE ref=?", (now(), admin_uid, ref))
        if t['status'] == 'pending':
            coupon_release(c, t['coupon'], t['telegram_id'])
    send(t['telegram_id'], f"❌ <b>NẠP VÍ {ref} BỊ TỪ CHỐI</b>\n{SEP}\n▸ Lý do: {esc(reason)}",
         KB([B('💬 Hỗ trợ', url=SUPPORT_LINK)], [B('🏠 Home', 'home')]))
    return True, 'Đã từ chối'


# ---- Điểm danh / Mời bạn / Đổi điểm ----
def do_checkin(uid):
    """55% +1đ / 45% key ngẫu nhiên union(15m,30m,1h). Streak, invite thưởng khi ra KEY lần đầu."""
    t, y = today(), yesterday()
    res, notify = {}, None
    with tx() as c:
        u = c1(c, 'SELECT * FROM users WHERE telegram_id=?', (uid,))
        if u['last_checkin'] == t:
            return {'status': 'done', 'streak': u['streak_days']}
        roll = random.SystemRandom().randint(0, 99)
        key = pick_key(c, FREE_TIERS, True) if roll >= sint('checkin_miss_pct') else None
        streak = u['streak_days'] + 1 if u['last_checkin'] == y else 1
        res = {'status': 'ok'}
        if key:
            ok, exp = assign_key(c, key, uid, 'checkin')
            if ok:
                res.update(kind='key', key=key['license_key'], tier=key['tier'], exp=exp)
            else:
                key = None
        if not key:  # miss hoặc pool rỗng -> an ủi điểm
            pt = sint('checkin_miss_point')
            add_points(c, uid, pt, 'checkin')
            res.update(kind='point', point=pt)
        res['streak'] = streak
        res['bonus'] = 0
        if streak >= sint('streak_days'):
            res['bonus'] = sint('streak_reward')
            add_points(c, uid, res['bonus'], 'streak')
            streak = 0
        c.execute('UPDATE users SET last_checkin=?,streak_days=? WHERE telegram_id=?', (t, streak, uid))
        if res.get('kind') == 'key' and u['inviter_id'] and not u['invite_confirmed']:
            inv = c1(c, 'SELECT * FROM users WHERE telegram_id=?', (u['inviter_id'],))
            if inv and not inv['banned']:
                rw = sint('invite_reward')
                if c.execute('INSERT OR IGNORE INTO invite_log(inviter_id,invitee_id,reward,created_at) VALUES(?,?,?,?)',
                             (inv['telegram_id'], uid, rw, now())).rowcount:
                    add_points(c, inv['telegram_id'], rw, 'invite')
                    notify = (inv['telegram_id'], rw, c1(c, 'SELECT points FROM users WHERE telegram_id=?', (inv['telegram_id'],))['points'])
                    res['invited'] = rw
            c.execute('UPDATE users SET invite_confirmed=1 WHERE telegram_id=?', (uid,))
    if notify:
        threading.Thread(target=lambda: send(
            notify[0], f"👥 <b>NGƯỜI BẠN MỜI VỪA CHECKIN RA KEY</b>\n{SEP}\n▸ User: {mask_uid(uid)}\n"
                       f"▸ Bạn nhận: +{notify[1]} điểm\n▸ Tổng điểm mới: {notify[2]}"), daemon=True).start()
    return res


def do_redeem(uid, cost):
    tier = REDEEM_TIERS[cost]
    ref = new_ref('RD')
    with tx() as c:
        u = c1(c, 'SELECT * FROM users WHERE telegram_id=?', (uid,))
        if u['points'] < cost:
            return None, f"Bạn cần {cost} điểm (hiện có {u['points']})"
        k = pick_key(c, [tier])
        if not k:
            return None, f'Kho {TIER_NAME[tier]} đang hết key, Admin đang nạp thêm 🙏'
        ok, exp = assign_key(c, k, uid, ref)
        if not ok:
            return None, 'Có lỗi, vui lòng thử lại'
        add_points(c, uid, -cost, f'redeem {tier}')
    deliver_key(uid, k['license_key'], tier, exp, '♻️ ĐỔI ĐIỂM THÀNH CÔNG', f'\n⭐ Đã trừ: {cost} điểm')
    return True, None


# ============================================================
# PHẦN 4: DOANH THU, BROADCAST, MAGIC LINK, HƯỚNG DẪN
# ============================================================
def month_range(m):
    y, mo = map(int, m.split('-'))
    s = datetime(y, mo, 1, tzinfo=TZ)
    e = datetime(y + (mo == 12), (mo % 12) + 1, 1, tzinfo=TZ)
    return int(s.timestamp()), int(e.timestamp()), (e - s).days


def revenue(m=None):
    m = m or datetime.now(TZ).strftime('%Y-%m')
    s, e, ndays = month_range(m)
    orders = q("SELECT * FROM orders WHERE status='approved' AND approved_at>=? AND approved_at<?", (s, e))
    tops = q("SELECT * FROM topups WHERE status='approved' AND approved_at>=? AND approved_at<?", (s, e))
    daily = [{'d': i + 1, 'purchase': 0, 'topup': 0, 'real': 0} for i in range(ndays)]
    by_pkg = {t: {'count': 0, 'amount': 0} for t in SELL_TIERS}
    for o in orders:
        d = datetime.fromtimestamp(o['approved_at'], TZ).day - 1
        daily[d]['purchase'] += o['amount']
        if o.get('method') != 'wallet':  # đơn ví đã được tính ở nạp ví
            daily[d]['real'] += o['amount']
        p = by_pkg.setdefault(o['package'], {'count': 0, 'amount': 0})
        p['count'] += 1
        p['amount'] += o['amount']
    for t in tops:
        d = datetime.fromtimestamp(t['approved_at'], TZ).day - 1
        daily[d]['topup'] += t['amount']
        daily[d]['real'] += t['amount']
    return {'month': m, 'purchase': sum(x['purchase'] for x in daily), 'topup': sum(x['topup'] for x in daily),
            'real': sum(x['real'] for x in daily), 'orders': len(orders), 'topups': len(tops),
            'by_package': by_pkg, 'daily': daily}


JOBS = {}


def audience_users(aud):
    base = 'SELECT telegram_id FROM users WHERE banned=0'
    if aud == 'active7d':
        d7 = (datetime.now(TZ) - timedelta(days=7)).strftime('%Y-%m-%d')
        rows = q(base + ' AND (last_checkin>=? OR created_at>? OR telegram_id IN '
                        '(SELECT telegram_id FROM orders WHERE created_at>?))', (d7, now() - 7 * 86400, now() - 7 * 86400))
    elif aud == 'balance':
        rows = q(base + ' AND balance>0')
    elif aud == 'ctv':  # CTV = người đã mời thành công >= 1 bạn
        rows = q(base + ' AND telegram_id IN (SELECT DISTINCT inviter_id FROM invite_log)')
    else:
        rows = q(base)
    return [r['telegram_id'] for r in rows]


def _bc_run(jid, text, uids):
    job = JOBS[jid]
    for uid in uids:
        for _ in range(3):
            r = tg('sendMessage', chat_id=uid, text=text[:4000], parse_mode='HTML', disable_web_page_preview=True)
            if not r.get('ok') and 'parse' in r.get('description', '').lower():
                r = tg('sendMessage', chat_id=uid, text=text[:4000])
            if r.get('ok'):
                job['sent'] += 1
                break
            wait = (r.get('parameters') or {}).get('retry_after')
            if wait:
                time.sleep(min(wait, 30))
                continue
            job['failed'] += 1
            break
        time.sleep(sfloat('throttle_sec'))
    job['done'] = True


def start_broadcast(text, aud):
    for k in [k for k, v in JOBS.items() if now() - v['ts'] > 3600]:
        JOBS.pop(k, None)  # job hết hạn sau 1h
    uids = audience_users(aud)
    jid = secrets.token_hex(6)
    JOBS[jid] = {'total': len(uids), 'sent': 0, 'failed': 0, 'done': False, 'ts': now()}
    threading.Thread(target=_bc_run, args=(jid, text, uids), daemon=True).start()
    return jid


# ---- Magic link web admin ----
SECRET = hashlib.sha256((TOKEN + '|pawnova-web').encode()).digest()
WEBHOOK_SECRET = env('WEBHOOK_SECRET') or hashlib.sha256((TOKEN + '|hook').encode()).hexdigest()[:40]
_used_tokens = {}


def _sign(p):
    return hmac.new(SECRET, p.encode(), hashlib.sha256).hexdigest()


def make_token(uid):
    p = f'{uid}:{now() + 300}'  # hết hạn 5 phút
    return base64.urlsafe_b64encode(p.encode()).decode().rstrip('=') + '.' + _sign(p)


def verify_token(t):
    try:
        b, sig = t.split('.', 1)
        p = base64.urlsafe_b64decode(b + '=' * (-len(b) % 4)).decode()
        if not hmac.compare_digest(_sign(p), sig):
            return None
        uid, exp = p.split(':')
        for k in [k for k, v in _used_tokens.items() if v < now()]:
            _used_tokens.pop(k, None)
        if int(exp) < now() or sig in _used_tokens:  # không dùng lại
            return None
        _used_tokens[sig] = int(exp)
        return int(uid)
    except Exception:
        return None


GUIDE = [
    ('/admin', 'Mở bảng điều khiển admin'),
    ('/webadmin', 'Nhận link đăng nhập Web Admin (hết hạn 5 phút, dùng 1 lần)'),
    ('/addkey <tier>', 'Nạp key vào kho. tier: 15m 30m 1h 1d 3d 7d life. Sau đó paste text hoặc gửi file .txt'),
    ('/cleanup', 'Archive ngay các key đã bán và hết hạn quá 3h'),
    ('/setadmin <uid> [level]', 'Thêm admin. level: super | admin | viewer (mặc định admin)'),
    ('/deladmin <uid>', 'Xoá admin (không xoá được super)'),
    ('/settier <amount> <bonus>', 'Thêm/sửa mốc nạp nhanh, ví dụ /settier 200000 30000'),
    ('/deltier <amount>', 'Xoá mốc nạp nhanh'),
    ('/newcp <code> <type> <value> <min_order> <max_uses> <days>',
     'Tạo coupon. type: discount | topup. value <=100 là %, >100 là số tiền VNĐ. max_uses=0 là không giới hạn'),
    ('/delcp <code>', 'Vô hiệu hoá coupon'),
    ('/listcp', 'Liệt kê coupon'),
    ('/revenue [YYYY-MM]', 'Xem doanh thu tháng'),
    ('/orders [status]', 'Xem đơn. status: pending | approved | rejected | expired'),
    ('/userinfo <uid>', 'Thông tin chi tiết một user'),
    ('/invites <uid>', 'Danh sách người user đã mời thành công'),
    ('/ban <uid> | /unban <uid>', 'Khoá / mở khoá user'),
    ('/broadcast <text>', 'Gửi thông báo tới toàn bộ user (hỗ trợ HTML)'),
    ('/backup', 'Gửi file bot.db qua Telegram'),
    ('/setqr', 'Gửi kèm ảnh QR (caption /setqr) để đặt ảnh QR tĩnh'),
]


# ============================================================
# PHẦN 5: GIAO DIỆN USER TRÊN BOT (100% nút)
# ============================================================
def home_text(u):
    return (f'🐾 <b>{esc(STORE_NAME)}</b>\n{SEP}\n⭐ Điểm: <b>{u["points"]}</b> · 💰 Ví: <b>{money(u["balance"])}</b>\n\n'
            f'Chọn chức năng 👇')


def v_home(chat, mid, uid, note=''):
    u = get_user(uid)
    rows = [[B('🛒 Mua Key', 'buy'), B('💰 Ví Tiền', 'wallet')],
            [B('🔑 Key Của Tôi', 'keys'), B('🎯 Nhiệm Vụ', 'tasks')],
            [B('🎁 Nhập Mã Ưu Đãi', 'coupon'), B('💬 Hỗ Trợ', url=SUPPORT_LINK)],
            [B('🏆 Top Tháng', 'top')]]
    if is_admin(uid):
        rows.append([B('🛠 Admin', 'a:home')])
    show(chat, mid, (note + '\n\n' if note else '') + home_text(u), {'inline_keyboard': rows})


def v_buy(chat, mid, uid):
    rows, row = [], []
    for t in SELL_TIERS:
        row.append(B(f'{PACKAGES[t][0]} · {money(price(t))}', f'pk:{t}'))
        if len(row) == 2:
            rows.append(row)
            row = []
    rows.append([B('🏠 Home', 'home')])
    show(chat, mid, f'🛒 <b>MUA KEY</b>\n{SEP}\nChọn gói bạn muốn 👇', {'inline_keyboard': rows})


def v_pkg(chat, mid, uid, tier):
    u = get_user(uid)
    base, disc, final, cp = calc_price(uid, tier)
    if stock([tier]) == 0:
        return show(chat, mid, f'⚠️ Gói <b>{PACKAGES[tier][0]}</b> đang hết key.\nAdmin đang nạp thêm, vui lòng quay lại sau 🙏',
                    KB([B('⬅️ Quay lại', 'buy')]))
    t = (f'📦 <b>GÓI {PACKAGES[tier][0].upper()}</b>\n{SEP}\n▸ Giá gốc: {money(base)}\n')
    if cp:
        t += f'▸ Mã <code>{esc(cp["code"])}</code>: -{money(disc)}\n'
    t += f'▸ Cần thanh toán: <b>{money(final)}</b>\n▸ Ví hiện tại: {money(u["balance"])}'
    show(chat, mid, t, KB([B('💳 Chuyển khoản', f'pay:ck:{tier}'), B('💰 Trả bằng ví', f'pay:wl:{tier}')],
                          [B('🎟 Đổi mã', f'cp:{tier}')], [B('⬅️ Quay lại', 'buy')]))


def bank_block(amount, ref):
    return (f'<blockquote>🧾 Mã đơn: <code>{ref}</code></blockquote>\n'
            f'<blockquote expandable>🏦 Ngân hàng: {esc(sget("bank_code"))}\nSTK: <code>{esc(sget("bank_account"))}</code>\n'
            f'Chủ TK: {esc(sget("bank_name"))}\nSố tiền: <code>{int(amount)}</code>\nNội dung CK: <code>{ref}</code></blockquote>')


def send_qr(chat, amount, ref, caption, kb):
    """Mode A: ảnh QR tĩnh admin upload. Mode B (hoặc chưa có ảnh): VietQR động theo từng đơn."""
    fid = sget('qr_file_id')
    if sget('qr_mode') == 'static' and fid:
        photo = fid
    else:
        photo = (f'https://img.vietqr.io/image/{sget("bank_code")}-{sget("bank_account")}-compact2.png'
                 f'?amount={int(amount)}&addInfo={quote(ref)}&accountName={quote(sget("bank_name"))}')
    r = tg('sendPhoto', chat_id=chat, photo=photo, caption=caption[:1024], parse_mode='HTML', reply_markup=kb)
    if not r.get('ok'):
        send(chat, caption, kb)


def ref_kb(kind, ref):
    return KB([B('📤 Gửi bill', f'bill:{kind}:{ref}'), B('🔄 Check', f'chk:{kind}:{ref}')],
              [B('❌ Huỷ', f'cx:{kind}:{ref}')])


def v_wallet(chat, mid, uid):
    u = get_user(uid)
    tiers = q('SELECT * FROM topup_tiers ORDER BY amount')
    t = (f'💰 <b>VÍ TIỀN</b>\n{SEP}\n▸ Số dư: <b>{money(u["balance"])}</b>\n▸ Tổng đã nạp: {money(u["total_topup"])}\n'
         f'▸ Tổng đã chi: {money(u["total_spent"])}\n\n<b>Mốc nạp nhanh:</b>\n')
    t += '\n'.join(f'• {money(x["amount"])} → thưởng +{money(x["bonus"])}' for x in tiers) or '• Chưa có mốc nạp'
    rows = [[B(f'{money(x["amount"])} (+{money(x["bonus"])})', f'tp:{x["amount"]}')] for x in tiers]
    rows += [[B('✍️ Nhập số khác', 'tpc')], [B('🏠 Home', 'home')]]
    show(chat, mid, t, {'inline_keyboard': rows})


def start_topup(chat, uid, amount):
    tp = create_topup(uid, amount)
    cap = (f'💳 <b>NẠP VÍ {money(amount)}</b>\n▸ Thưởng: +{money(tp["bonus"])}\n'
           f'{bank_block(amount, tp["ref"])}\n⏳ Hết hạn sau {sint("order_ttl_min")} phút')
    send_qr(chat, amount, tp['ref'], cap, ref_kb('t', tp['ref']))


def v_tasks(chat, mid, uid, force=False):
    miss = missing_channels(uid, force)
    if miss:
        return wall(chat, mid, miss, 'tasks')
    u = get_user(uid)
    show(chat, mid, f'🎯 <b>NHIỆM VỤ & ĐIỂM</b>\n{SEP}\n⭐ Điểm: <b>{u["points"]}</b> · 🔥 Streak: {u["streak_days"]}/{sint("streak_days")}',
         KB([B('🎁 Điểm danh', 'ci'), B('👥 Mời bạn', 'inv')],
            [B('♻️ 6đ→1h', 'rd:6'), B('♻️ 15đ→1d', 'rd:15')],
            [B('♻️ 40đ→3d', 'rd:40'), B('♻️ 100đ→7d', 'rd:100')],
            [B('🏆 Top', 'top'), B('🏠 Home', 'home')]))


def v_keys(chat, mid, uid):
    ks = q('SELECT * FROM keys_pool WHERE user_id=? AND archived=0 ORDER BY sold_at DESC LIMIT 10', (uid,))
    if not ks:
        return show(chat, mid, f'🔑 <b>KEY CỦA TÔI</b>\n{SEP}\nBạn chưa có key nào.', KB([B('🛒 Mua Key', 'buy'), B('🏠 Home', 'home')]))
    rows = [[B(f'{TIER_NAME[k["tier"]]} · {mask_key(k["license_key"])} · {fmt_ts(k["expires_at"])[:10] if k["expires_at"] else "∞"}',
               f'k:{k["id"]}')] for k in ks]
    rows.append([B('🏠 Home', 'home')])
    show(chat, mid, f'🔑 <b>KEY CỦA TÔI</b>\n{SEP}\nBấm vào key để xem đầy đủ 👇', {'inline_keyboard': rows})


def v_top(chat, mid):
    rows = q('SELECT * FROM users WHERE points>0 AND banned=0 ORDER BY points DESC LIMIT 10')
    medals = ['🥇', '🥈', '🥉'] + ['▫️'] * 7
    t = f'🏆 <b>TOP ĐIỂM THÁNG</b>\n{SEP}\n'
    t += '\n'.join(f'{medals[i]} {esc(mask_uid(r["telegram_id"]))} — <b>{r["points"]}</b>⭐' for i, r in enumerate(rows)) or 'Chưa có ai trên bảng xếp hạng.'
    show(chat, mid, t, KB([B('🏠 Home', 'home')]))


def on_user_cb(cb, uid, chat, mid, d):
    """Điều phối callback của user. Trả True nếu đã xử lý."""
    p = d.split(':')
    a = p[0]
    cid = cb['id']
    if a == 'home':
        clear_state(uid)
        v_home(chat, mid, uid)
    elif a == 'buy':
        v_buy(chat, mid, uid)
    elif a == 'pk':
        v_pkg(chat, mid, uid, p[1])
    elif a == 'cp':  # đổi mã từ màn gói
        set_state(uid, 'coupon', p[1])
        show(chat, mid, f'🎟 Gõ <b>mã ưu đãi</b> của bạn vào khung chat 👇', KB([B('⬅️ Huỷ', f'pk:{p[1]}')]))
    elif a == 'pay' and p[1] == 'ck':
        o, err = create_order(uid, p[2])
        if err:
            return cb_answer(cid, err, True)
        cb_answer(cid)
        cap = (f'💳 <b>THANH TOÁN CHUYỂN KHOẢN</b>\n▸ Gói: {PACKAGES[o["package"]][0]}\n▸ Số tiền: <b>{money(o["amount"])}</b>\n'
               f'{bank_block(o["amount"], o["ref"])}\n⏳ Hết hạn sau {sint("order_ttl_min")} phút')
        return send_qr(chat, o['amount'], o['ref'], cap, ref_kb('o', o['ref']))
    elif a == 'pay' and p[1] == 'wl':
        u = get_user(uid)
        base, disc, final, cp = calc_price(uid, p[2])
        if u['balance'] < final:
            return show(chat, mid, f'⚠️ Số dư không đủ.\n▸ Cần: {money(final)}\n▸ Ví: {money(u["balance"])}',
                        KB([B('💰 Nạp ví', 'wallet')], [B('⬅️ Quay lại', f'pk:{p[2]}')]))
        show(chat, mid, f'💰 <b>XÁC NHẬN TRẢ BẰNG VÍ</b>\n{SEP}\n▸ Gói: {PACKAGES[p[2]][0]}\n▸ Trừ: <b>{money(final)}</b>',
             KB([B(f'✅ Trừ {money(final)}', f'pay:wlok:{p[2]}')], [B('⬅️ Quay lại', f'pk:{p[2]}')]))
    elif a == 'pay' and p[1] == 'wlok':
        ref, err = pay_wallet(uid, p[2])
        if err:
            return cb_answer(cid, err, True)
        show(chat, mid, '✅ Thanh toán bằng ví thành công, key đã được gửi bên dưới 👇', KB([B('🏠 Home', 'home')]))
    elif a == 'bill':
        tb = 'orders' if p[1] == 'o' else 'topups'
        r = q1(f'SELECT * FROM {tb} WHERE ref=? AND telegram_id=?', (p[2], uid))
        if not r or r['status'] != 'pending':
            return cb_answer(cid, 'Đơn không còn ở trạng thái chờ', True)
        set_state(uid, 'bill', f'{p[1]}:{p[2]}')
        cb_answer(cid)
        send(chat, f'📤 Hãy gửi <b>ảnh bill chuyển khoản</b> cho đơn <code>{p[2]}</code> vào khung chat 👇')
    elif a == 'chk':
        tb = 'orders' if p[1] == 'o' else 'topups'
        r = q1(f'SELECT status FROM {tb} WHERE ref=? AND telegram_id=?', (p[2], uid))
        labels = {'pending': '⏳ Đang chờ duyệt', 'approved': '✅ Đã duyệt', 'rejected': '❌ Bị từ chối', 'expired': '⌛ Đã hết hạn'}
        cb_answer(cid, labels.get(r['status'], '?') if r else 'Không thấy đơn', True)
    elif a == 'cx':
        tb = 'orders' if p[1] == 'o' else 'topups'
        with tx() as c:
            r = c1(c, f'SELECT * FROM {tb} WHERE ref=? AND telegram_id=?', (p[2], uid))
            if r and r['status'] == 'pending':
                c.execute(f"UPDATE {tb} SET status='expired' WHERE ref=?", (p[2],))
                coupon_release(c, r['coupon'], uid)
        cb_answer(cid, 'Đã huỷ đơn')
        tg('deleteMessage', chat_id=chat, message_id=mid)
        v_home(chat, None, uid)
    elif a == 'wallet':
        v_wallet(chat, mid, uid)
    elif a == 'tp':
        cb_answer(cid)
        start_topup(chat, uid, int(p[1]))
    elif a == 'tpc':
        set_state(uid, 'topup_custom')
        show(chat, mid, '✍️ Gõ <b>số tiền muốn nạp</b> (VNĐ, từ 10.000 đến 50.000.000) vào khung chat 👇', KB([B('⬅️ Huỷ', 'wallet')]))
    elif a == 'coupon':
        set_state(uid, 'coupon', '')
        show(chat, mid, '🎁 Gõ <b>mã ưu đãi</b> của bạn vào khung chat 👇', KB([B('⬅️ Huỷ', 'home')]))
    elif a == 'tasks':
        v_tasks(chat, mid, uid, force=True)
    elif a == 'ci':
        if missing_channels(uid, True):
            return v_tasks(chat, mid, uid, True)
        r = do_checkin(uid)
        back = KB([B('🎯 Nhiệm vụ', 'tasks'), B('🏠 Home', 'home')])
        if r['status'] == 'done':
            return cb_answer(cid, f'Hôm nay bạn đã điểm danh rồi (streak {r["streak"]}/{sint("streak_days")})', True)
        st = f'\n🔥 Streak: {r["streak"]}/{sint("streak_days")}'
        if r['bonus']:
            st += f'\n🎊 Đủ chuỗi! Thưởng +{r["bonus"]} điểm'
        if r['kind'] == 'key':
            t = (f'🎉 <b>ĐIỂM DANH THÀNH CÔNG</b>\n{SEP}\n🔑 Key:\n<pre>{esc(r["key"])}</pre>\n▸ Tier: {r["tier"]}\n'
                 f'▸ Hết hạn: {fmt_ts(r["exp"])}{st}\n🔗 Kích hoạt: {esc(ACTIVATE_URL)}')
            if r.get('invited'):
                t += f'\n👥 Người mời bạn vừa nhận +{r["invited"]}đ'
            show(chat, mid, t, KB([B('📋 Copy key', copy=r['key']), B('🔗 Kích hoạt', url=ACTIVATE_URL)],
                                  [B('🎯 Nhiệm vụ', 'tasks'), B('🏠 Home', 'home')]))
        else:
            show(chat, mid, f'🎉 <b>ĐIỂM DANH THÀNH CÔNG</b>\n{SEP}\n⭐ +{r["point"]} điểm{st}', back)
    elif a == 'inv':
        if missing_channels(uid):
            return v_tasks(chat, mid, uid)
        n = q1('SELECT COUNT(*) n FROM invite_log WHERE inviter_id=?', (uid,))['n']
        link = f'https://t.me/{BOT_USERNAME}?start=ref_{uid}'
        show(chat, mid, f'👥 <b>MỜI BẠN BÈ</b>\n{SEP}\nLink của bạn:\n<code>{link}</code>\n\n'
                        f'Khi bạn được mời điểm danh <b>ra key</b> lần đầu, bạn nhận <b>+{sint("invite_reward")} điểm</b>.\n'
                        f'▸ Đã mời thành công: {n}',
             KB([B('📤 Chia sẻ', url=f'https://t.me/share/url?url={quote(link)}')], [B('🎯 Nhiệm vụ', 'tasks')]))
    elif a == 'rd':
        if missing_channels(uid):
            return v_tasks(chat, mid, uid)
        ok, err = do_redeem(uid, int(p[1]))
        if err:
            return cb_answer(cid, err, True)
        cb_answer(cid, 'Đổi điểm thành công, key đã được gửi 👇')
        v_tasks(chat, mid, uid)
    elif a == 'keys':
        v_keys(chat, mid, uid)
    elif a == 'k':
        k = q1('SELECT * FROM keys_pool WHERE id=? AND user_id=? AND archived=0', (int(p[1]), uid))
        if not k:
            return cb_answer(cid, 'Không thấy key', True)
        show(chat, mid, f'🔑 <b>KEY CỦA BẠN</b>\n{SEP}\n<pre>{esc(k["license_key"])}</pre>\n▸ Gói: {TIER_NAME[k["tier"]]}\n'
                        f'▸ Hết hạn: {fmt_ts(k["expires_at"]) if k["expires_at"] else "Vĩnh viễn"}\n🔗 {esc(ACTIVATE_URL)}',
             KB([B('📋 Copy', copy=k['license_key']), B('🔗 Kích hoạt', url=ACTIVATE_URL)], [B('⬅️ Danh sách', 'keys')]))
    elif a == 'top':
        v_top(chat, mid)
    else:
        return False
    return True


# ============================================================
# PHẦN 6: ADMIN TRÊN BOT + ĐIỀU PHỐI UPDATE
# ============================================================
_answered = set()
_orig_cb_answer = cb_answer


def cb_answer(cid, text='', alert=False):  # ghi nhớ callback đã trả lời để không trả lời 2 lần
    if len(_answered) > 5000:
        _answered.clear()
    _answered.add(cid)
    _orig_cb_answer(cid, text, alert)


REASONS = ['Ảnh mờ, không đọc được', 'Không thấy chuyển khoản', 'Trùng đơn / đã xử lý']


def make_backup_file():
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    dst = sqlite3.connect(path)
    src = conn()
    src.backup(dst)
    dst.close()
    src.close()
    return path


def send_backup(chat):
    path = make_backup_file()
    try:
        with open(path, 'rb') as f:
            tg('sendDocument', files={'document': (f'bot-{today()}.db', f.read())}, chat_id=chat,
               caption=f'💾 Backup {fmt_ts(now())}')
    finally:
        os.remove(path)


def admin_home(chat, mid):
    show(chat, mid, f'🛠 <b>ADMIN PANEL</b>\n{SEP}\nChọn chức năng 👇',
         KB([B('📦 Kho Key', 'a:pool'), B('🧾 Đơn Chờ', 'a:orders')],
            [B('👥 Users', 'a:users'), B('🎁 Coupons', 'a:cps')],
            [B('📊 Doanh Thu', 'a:rev'), B('📢 Broadcast', 'a:bc')],
            [B('💾 Backup', 'a:bk'), B('📖 Hướng dẫn', 'a:guide')],
            [B('🖥️ Web Admin', 'a:web'), B('↩️ Home', 'home')]))


def mark_msg(cb, suffix):
    """Gỡ nút + ghi chú kết quả vào tin bill (ảnh hoặc text)"""
    msg = cb['message']
    chat, mid = msg['chat']['id'], msg['message_id']
    if msg.get('photo'):
        tg('editMessageCaption', chat_id=chat, message_id=mid, caption=((msg.get('caption') or '') + suffix)[:1024],
           reply_markup={'inline_keyboard': []})
    else:
        tg('editMessageText', chat_id=chat, message_id=mid, text=((msg.get('text') or '') + suffix)[:4000],
           reply_markup={'inline_keyboard': []})


def pending_list_text():
    os_ = q("SELECT * FROM orders WHERE status='pending' ORDER BY created_at DESC LIMIT 10")
    ts = q("SELECT * FROM topups WHERE status='pending' ORDER BY created_at DESC LIMIT 10")
    t = f'🧾 <b>ĐƠN CHỜ DUYỆT</b>\n{SEP}\n'
    rows = []
    for o in os_:
        t += f'🛒 <code>{o["ref"]}</code> · {mask_uid(o["telegram_id"])} · {o["package"]} · {money(o["amount"])}' + (' 📎' if o['bill_file_id'] else '') + '\n'
        rows.append([B(f'✅ {o["ref"]}', f'ap:o:{o["ref"]}'), B('❌', f'rj:o:{o["ref"]}')])
    for x in ts:
        t += f'💰 <code>{x["ref"]}</code> · {mask_uid(x["telegram_id"])} · {money(x["amount"])} (+{money(x["bonus"])})' + (' 📎' if x['bill_file_id'] else '') + '\n'
        rows.append([B(f'✅ {x["ref"]}', f'ap:t:{x["ref"]}'), B('❌', f'rj:t:{x["ref"]}')])
    if not rows:
        t += 'Không có đơn nào đang chờ 🎉'
    rows.append([B('⬅️ Admin', 'a:home')])
    return t, {'inline_keyboard': rows}


def revenue_text(m=None):
    r = revenue(m)
    t = (f'📊 <b>DOANH THU {r["month"]}</b>\n{SEP}\n▸ Tổng mua: {money(r["purchase"])} ({r["orders"]} đơn)\n'
         f'▸ Tổng nạp: {money(r["topup"])} ({r["topups"]} lần)\n▸ Thực thu: <b>{money(r["real"])}</b>\n\n<b>Theo gói:</b>\n')
    t += '\n'.join(f'• {PACKAGES.get(k, (k,))[0]}: {v["count"]} đơn · {money(v["amount"])}' for k, v in r['by_package'].items())
    return t


def on_admin_cb(cb, uid, chat, mid, d):
    p = d.split(':')
    a, cid = p[0], cb['id']
    if a == 'a':
        w = p[1]
        if w == 'home':
            admin_home(chat, mid)
        elif w == 'pool':
            t = f'📦 <b>KHO KEY</b>\n{SEP}\n'
            for tier in ALL_TIERS:
                r = q1('SELECT SUM(user_id IS NULL AND archived=0) free, COUNT(*) total FROM keys_pool WHERE tier=?', (tier,))
                t += f'• {TIER_NAME[tier]} ({tier}): còn <b>{r["free"] or 0}</b> / {r["total"]}\n'
            show(chat, mid, t, KB([B('➕ Nạp key', 'ak:'), B('⬅️ Admin', 'a:home')]))
        elif w == 'orders':
            t, k = pending_list_text()
            show(chat, mid, t, k)
        elif w == 'users':
            r = q1('SELECT COUNT(*) n, SUM(banned) b, SUM(balance) bal FROM users')
            show(chat, mid, f'👥 <b>USERS</b>\n{SEP}\n▸ Tổng: {r["n"]}\n▸ Bị khoá: {r["b"] or 0}\n▸ Tổng số dư ví: {money(r["bal"])}\n\n'
                            f'Dùng /userinfo &lt;uid&gt; để xem chi tiết.', KB([B('⬅️ Admin', 'a:home')]))
        elif w == 'cps':
            show(chat, mid, coupon_list_text(), KB([B('⬅️ Admin', 'a:home')]))
        elif w == 'rev':
            show(chat, mid, revenue_text(), KB([B('⬅️ Admin', 'a:home')]))
        elif w == 'bc':
            if not has_perm(uid, 'broadcast'):
                return cb_answer(cid, 'Bạn không có quyền', True)
            set_state(uid, 'broadcast')
            show(chat, mid, '📢 Gõ nội dung broadcast (hỗ trợ HTML) — sẽ gửi tới <b>toàn bộ user</b> ngay khi bạn gửi 👇',
                 KB([B('⬅️ Huỷ', 'a:home')]))
        elif w == 'bk':
            if not has_perm(uid, 'backup'):
                return cb_answer(cid, 'Bạn không có quyền', True)
            cb_answer(cid, 'Đang tạo backup...')
            send_backup(chat)
        elif w == 'guide':
            show(chat, mid, '📖 <b>HƯỚNG DẪN ADMIN</b>\n' + SEP + '\n' + '\n'.join(f'<code>{esc(c)}</code>\n   {esc(dsc)}' for c, dsc in GUIDE),
                 KB([B('⬅️ Admin', 'a:home')]))
        elif w == 'web':
            if not PUBLIC_URL:
                return cb_answer(cid, 'Chưa cấu hình PUBLIC_URL', True)
            send(chat, '🖥️ Link đăng nhập Web Admin (hết hạn 5 phút, dùng 1 lần):',
                 KB([B('🔐 Mở Web Admin', url=f'{PUBLIC_URL}/admin/login?t={make_token(uid)}')]))
    elif a == 'ak':  # chọn tier nạp key
        if p[1] in ALL_TIERS:
            set_state(uid, 'addkey', p[1])
            show(chat, mid, f'📥 Nạp key tier <b>{p[1]}</b>.\nPaste danh sách key (cách nhau bằng dấu cách / xuống dòng / dấu phẩy) '
                            f'hoặc gửi file .txt 👇', KB([B('⬅️ Huỷ', 'a:pool')]))
        else:
            rows = [[B(t, f'ak:{t}') for t in ALL_TIERS[i:i + 3]] for i in range(0, len(ALL_TIERS), 3)]
            rows.append([B('⬅️ Huỷ', 'a:pool')])
            show(chat, mid, '📥 Chọn tier để nạp key:', {'inline_keyboard': rows})
    elif a == 'ap':
        kind, ref = p[1], p[2]
        if not has_perm(uid, 'orders' if kind == 'o' else 'topups'):
            return cb_answer(cid, 'Bạn không có quyền', True)
        ok, msg = approve_order(ref, uid) if kind == 'o' else approve_topup(ref, uid)
        cb_answer(cid, msg, not ok)
        if ok:
            if cb['message'].get('photo') or 'BILL' in (cb['message'].get('text') or ''):
                mark_msg(cb, f'\n\n✅ ĐÃ DUYỆT bởi {uid}')
            else:
                t, k = pending_list_text()
                show(chat, mid, t, k)
    elif a == 'rj':
        kind, ref = p[1], p[2]
        rows = [[B(r, f'rr:{kind}:{ref}:{i}')] for i, r in enumerate(REASONS)]
        rows.append([B('✍️ Lý do khác', f'rr:{kind}:{ref}:9')])
        send(chat, f'❌ Chọn lý do từ chối <code>{ref}</code>:', {'inline_keyboard': rows})
    elif a == 'rr':
        kind, ref, i = p[1], p[2], int(p[3])
        if not has_perm(uid, 'orders' if kind == 'o' else 'topups'):
            return cb_answer(cid, 'Bạn không có quyền', True)
        if i == 9:
            set_state(uid, 'reject', f'{kind}:{ref}')
            return show(chat, mid, f'✍️ Gõ lý do từ chối cho <code>{ref}</code> 👇')
        ok, msg = (reject_order if kind == 'o' else reject_topup)(ref, uid, REASONS[i])
        show(chat, mid, ('❌ ' if ok else '⚠️ ') + msg + f' <code>{ref}</code>')
    return True


def coupon_list_text():
    cps = q('SELECT * FROM coupons ORDER BY created_at DESC LIMIT 30')
    t = f'🎁 <b>COUPONS</b>\n{SEP}\n'
    for c in cps:
        val = f'{c["value"]}%' if c['value'] <= 100 else money(c['value'])
        t += (f'{"🟢" if c["active"] else "🔴"} <code>{esc(c["code"])}</code> · {c["type"]} · {val} · '
              f'{c["used_count"]}/{c["max_uses"] or "∞"} · HSD {fmt_ts(c["expires_at"])[:10] if c["expires_at"] else "∞"}\n')
    return t if cps else t + 'Chưa có coupon.'


def user_info_text(u):
    inv = q1('SELECT COUNT(*) n FROM invite_log WHERE inviter_id=?', (u['telegram_id'],))['n']
    return (f'👤 <b>{esc(uname(u))}</b> (<code>{u["telegram_id"]}</code>)\n{SEP}\n▸ Điểm: {u["points"]} · Streak: {u["streak_days"]}\n'
            f'▸ Ví: {money(u["balance"])} · Nạp: {money(u["total_topup"])} · Chi: {money(u["total_spent"])}\n'
            f'▸ Người mời: {u["inviter_id"] or "—"} · Đã mời: {inv}\n▸ Trạng thái: {"🚫 Bị khoá" if u["banned"] else "✅ Bình thường"}\n'
            f'▸ Tạo lúc: {fmt_ts(u["created_at"])}')


def handle_admin_cmd(cmd, text, uid, chat):
    """Lệnh admin. Trả True nếu đã xử lý."""
    args = text.split()[1:]
    if cmd == '/admin':
        ensure_qr()
        admin_home(chat, None)
    elif cmd == '/webadmin':
        if not PUBLIC_URL:
            send(chat, '⚠️ Chưa cấu hình PUBLIC_URL trên Render.')
        else:
            send(chat, '🖥️ Link đăng nhập Web Admin (hết hạn 5 phút, dùng 1 lần):',
                 KB([B('🔐 Mở Web Admin', url=f'{PUBLIC_URL}/admin/login?t={make_token(uid)}')]))
    elif cmd == '/addkey':
        if args and args[0] in ALL_TIERS:
            set_state(uid, 'addkey', args[0])
            send(chat, f'📥 Nạp key tier <b>{args[0]}</b>. Paste key hoặc gửi file .txt 👇')
        else:
            on_admin_cb({'id': '0', 'message': {}}, uid, chat, None, 'ak:')
    elif cmd == '/cleanup':
        send(chat, f'🧹 Đã archive {archive_old_keys()} key hết hạn.')
    elif cmd == '/setadmin' and has_perm(uid, 'admins'):
        if not args or not args[0].isdigit():
            return send(chat, 'Cú pháp: /setadmin &lt;uid&gt; [super|admin|viewer]') and True
        lv = args[1] if len(args) > 1 and args[1] in LEVEL_DEFAULT else 'admin'
        ex('INSERT INTO admins(telegram_id,level,permissions,added_by,added_at) VALUES(?,?,?,?,?) '
           'ON CONFLICT(telegram_id) DO UPDATE SET level=excluded.level,permissions=excluded.permissions',
           (int(args[0]), lv, json.dumps(LEVEL_DEFAULT[lv]), uid, now()))
        send(chat, f'✅ Đã đặt {args[0]} làm <b>{lv}</b>.')
    elif cmd == '/deladmin' and has_perm(uid, 'admins'):
        if args and args[0].isdigit() and int(args[0]) != ADMIN_ID:
            ex("DELETE FROM admins WHERE telegram_id=? AND level!='super'", (int(args[0]),))
            send(chat, '✅ Đã xoá admin (super không xoá được).')
        else:
            send(chat, 'Cú pháp: /deladmin &lt;uid&gt;')
    elif cmd == '/settier' and has_perm(uid, 'settings'):
        if len(args) == 2 and args[0].isdigit() and args[1].isdigit():
            ex('INSERT INTO topup_tiers(amount,bonus) VALUES(?,?) ON CONFLICT(amount) DO UPDATE SET bonus=excluded.bonus',
               (int(args[0]), int(args[1])))
            send(chat, f'✅ Mốc nạp {money(args[0])} thưởng {money(args[1])}.')
        else:
            send(chat, 'Cú pháp: /settier &lt;amount&gt; &lt;bonus&gt;')
    elif cmd == '/deltier' and has_perm(uid, 'settings'):
        if args and args[0].isdigit():
            ex('DELETE FROM topup_tiers WHERE amount=?', (int(args[0]),))
            send(chat, '✅ Đã xoá mốc nạp.')
    elif cmd == '/newcp' and has_perm(uid, 'coupons'):
        try:
            code, typ, val, mn, mx, days = args[0].upper(), args[1], int(args[2]), int(args[3]), int(args[4]), int(args[5])
            assert typ in ('discount', 'topup')
            ex('INSERT OR REPLACE INTO coupons(code,type,value,min_order,max_uses,used_count,expires_at,active,created_by,created_at) '
               'VALUES(?,?,?,?,?,COALESCE((SELECT used_count FROM coupons WHERE code=?),0),?,1,?,?)',
               (code, typ, val, mn, mx, code, now() + days * 86400 if days else None, uid, now()))
            send(chat, f'✅ Đã tạo coupon <code>{esc(code)}</code>')
        except Exception:
            send(chat, 'Cú pháp: /newcp &lt;code&gt; &lt;discount|topup&gt; &lt;value&gt; &lt;min_order&gt; &lt;max_uses&gt; &lt;days&gt;\n'
                       'VD: /newcp PAW10 discount 10 0 100 30 (giảm 10%)')
    elif cmd == '/delcp' and has_perm(uid, 'coupons'):
        if args:
            ex('UPDATE coupons SET active=0 WHERE code=?', (args[0].upper(),))
            send(chat, '✅ Đã vô hiệu hoá coupon.')
    elif cmd == '/listcp':
        send(chat, coupon_list_text())
    elif cmd == '/revenue' and has_perm(uid, 'revenue'):
        send(chat, revenue_text(args[0] if args and re.match(r'^\d{4}-\d{2}$', args[0]) else None))
    elif cmd == '/orders':
        st = args[0] if args else 'pending'
        rows = q('SELECT * FROM orders WHERE status=? ORDER BY created_at DESC LIMIT 20', (st,))
        send(chat, f'🧾 <b>ĐƠN [{esc(st)}]</b>\n' + '\n'.join(
            f'<code>{o["ref"]}</code> · {mask_uid(o["telegram_id"])} · {o["package"]} · {money(o["amount"])}' for o in rows) or 'Không có đơn.')
    elif cmd == '/userinfo' and has_perm(uid, 'users'):
        u = get_user(int(args[0])) if args and args[0].isdigit() else None
        send(chat, user_info_text(u) if u else 'Không thấy user.')
    elif cmd == '/invites' and has_perm(uid, 'users'):
        rows = q('SELECT * FROM invite_log WHERE inviter_id=? ORDER BY created_at DESC LIMIT 50', (int(args[0]),)) if args and args[0].isdigit() else []
        send(chat, '👥 <b>ĐÃ MỜI</b>\n' + ('\n'.join(f'• {r["invitee_id"]} · +{r["reward"]}đ · {fmt_ts(r["created_at"])}' for r in rows) or 'Chưa có.'))
    elif cmd in ('/ban', '/unban') and has_perm(uid, 'users'):
        if args and args[0].isdigit() and int(args[0]) != ADMIN_ID:
            ex('UPDATE users SET banned=? WHERE telegram_id=?', (1 if cmd == '/ban' else 0, int(args[0])))
            send(chat, '✅ Đã ' + ('khoá' if cmd == '/ban' else 'mở khoá') + f' {args[0]}')
    elif cmd == '/broadcast' and has_perm(uid, 'broadcast'):
        body = text.split(None, 1)[1] if len(args) else ''
        if body:
            jid = start_broadcast(body, 'all')
            send(chat, f'📢 Đang gửi tới {JOBS[jid]["total"]} user...')
        else:
            send(chat, 'Cú pháp: /broadcast &lt;nội dung&gt;')
    elif cmd == '/backup' and has_perm(uid, 'backup'):
        send_backup(chat)
    elif cmd == '/guide':
        on_admin_cb({'id': '0'}, uid, chat, None, 'a:guide')
    else:
        return False
    return True


def ensure_qr():
    """Nếu có file qr.jpg đi kèm mà chưa có qr_file_id -> tự upload để lấy file_id (Mode A)"""
    if sget('qr_file_id'):
        return
    path = os.path.join(BASE_DIR, 'qr.jpg')
    if not (os.path.exists(path) and ADMIN_ID):
        return
    with open(path, 'rb') as f:
        r = tg('sendPhoto', files={'photo': ('qr.jpg', f.read())}, chat_id=ADMIN_ID, caption='✅ Đã nạp ảnh QR mặc định cho bot.')
    if r.get('ok'):
        sset('qr_file_id', r['result']['photo'][-1]['file_id'])
        sset('qr_mode', 'static')


# ---- Xử lý message ----
def on_photo(m, uid, chat):
    fid = m['photo'][-1]['file_id']
    cap = (m.get('caption') or '').strip()
    if cap.startswith('/setqr') and has_perm(uid, 'settings'):
        sset('qr_file_id', fid)
        sset('qr_mode', 'static')
        return send(chat, '✅ Đã đặt ảnh QR tĩnh (Mode A).')
    st = get_state(uid)
    if not (st and st['action'] == 'bill'):
        return send(chat, 'Bạn chưa chọn đơn nào để gửi bill. Hãy vào <b>Mua Key</b> hoặc <b>Ví Tiền</b> trước nhé.',
                    KB([B('🏠 Home', 'home')]))
    kind, ref = st['meta'].split(':', 1)
    tb = 'orders' if kind == 'o' else 'topups'
    r = q1(f'SELECT * FROM {tb} WHERE ref=? AND telegram_id=?', (ref, uid))
    if not r or r['status'] not in ('pending', 'expired'):
        clear_state(uid)
        return send(chat, 'Đơn này không còn ở trạng thái chờ.', KB([B('🏠 Home', 'home')]))
    ex(f'UPDATE {tb} SET bill_file_id=? WHERE ref=?', (fid, ref))
    clear_state(uid)
    u = get_user(uid)
    cap = (f'🧾 BILL {"ĐƠN MUA" if kind == "o" else "NẠP VÍ"} — {ref}\n▸ User: {uname(u)} ({uid})\n'
           + (f'▸ Gói: {r["package"]} · {money(r["amount"])}' if kind == 'o' else f'▸ Nạp: {money(r["amount"])} (+{money(r["bonus"])})'))
    for aid in admin_ids():
        tg('sendPhoto', chat_id=aid, photo=fid, caption=cap,
           reply_markup=KB([B('✅ Duyệt', f'ap:{kind}:{ref}'), B('❌ Từ chối', f'rj:{kind}:{ref}')]))
    send(chat, '✅ Đã gửi bill cho admin, vui lòng chờ duyệt trong ít phút.', KB([B('🏠 Home', 'home')]))


def do_addkey_text(chat, uid, tier, text):
    r = insert_keys(tier, parse_keys(text))
    clear_state(uid)
    send(chat, f'📥 <b>Nạp key {tier}</b>\n▸ Thêm mới: {r["inserted"]}\n▸ Trùng: {r["duplicate"]}\n▸ Không hợp lệ: {r["invalid"]}',
         KB([B('➕ Nạp tiếp', f'ak:{tier}'), B('⬅️ Admin', 'a:home')]))


def on_text(m, uid, chat, text):
    st = get_state(uid)
    if not st:
        return v_home(chat, None, uid)
    a = st['action']
    if a == 'coupon':
        cp, err = coupon_check(text, uid)
        if err:
            return send(chat, f'⚠️ {err}\nGõ lại mã khác hoặc bấm Home.', KB([B('🏠 Home', 'home')]))
        ex('INSERT INTO pending_discount(telegram_id,code) VALUES(?,?) ON CONFLICT(telegram_id) DO UPDATE SET code=excluded.code',
           (uid, cp['code']))
        clear_state(uid)
        val = f'{cp["value"]}%' if cp['value'] <= 100 else money(cp['value'])
        note = f'✅ Đã lưu mã <code>{esc(cp["code"])}</code> (giảm {val}). Sẽ tự áp dụng cho ' + ('lần nạp ví tiếp theo.' if cp['type'] == 'topup' else 'đơn mua tiếp theo.')
        if st['meta'] in SELL_TIERS:
            send(chat, note)
            return v_pkg(chat, None, uid, st['meta'])
        v_home(chat, None, uid, note)
    elif a == 'topup_custom':
        digits = re.sub(r'\D', '', text)
        if not digits or not (10000 <= int(digits) <= 50000000):
            return send(chat, '⚠️ Số tiền không hợp lệ (10.000 – 50.000.000). Gõ lại nhé.')
        clear_state(uid)
        start_topup(chat, uid, int(digits))
    elif a == 'bill':
        send(chat, '📸 Vui lòng gửi <b>ẢNH</b> bill chuyển khoản (không phải chữ).')
    elif a == 'addkey' and is_admin(uid):
        do_addkey_text(chat, uid, st['meta'], text)
    elif a == 'broadcast' and has_perm(uid, 'broadcast'):
        clear_state(uid)
        jid = start_broadcast(text, 'all')
        send(chat, f'📢 Đang gửi tới {JOBS[jid]["total"]} user...')
    elif a == 'reject' and is_admin(uid):
        kind, ref = st['meta'].split(':', 1)
        clear_state(uid)
        ok, msg = (reject_order if kind == 'o' else reject_topup)(ref, uid, text[:300])
        send(chat, ('❌ ' if ok else '⚠️ ') + msg + f' <code>{ref}</code>')
    else:
        v_home(chat, None, uid)


def on_message(m):
    if m['chat'].get('type') != 'private':
        return
    frm = m.get('from') or {}
    uid, chat = frm.get('id'), m['chat']['id']
    text = (m.get('text') or '').strip()
    inviter = None
    if text.startswith('/start'):
        parts = text.split(maxsplit=1)
        if len(parts) > 1 and parts[1].startswith('ref_') and parts[1][4:].isdigit():
            inviter = int(parts[1][4:])
    u = ensure_user(frm, inviter)
    if u['banned'] and not is_admin(uid):
        return send(chat, '🚫 Tài khoản của bạn đã bị khoá.')
    if text.startswith('/'):
        cmd = text.split()[0].split('@')[0].lower()
        if cmd == '/start':
            clear_state(uid)
            return v_home(chat, None, uid)
        if is_admin(uid) and handle_admin_cmd(cmd, text, uid, chat):
            return
        return v_home(chat, None, uid)
    if m.get('photo'):
        return on_photo(m, uid, chat)
    if m.get('document') and is_admin(uid):
        st = get_state(uid)
        if st and st['action'] == 'addkey':
            data = download_file(m['document']['file_id'])
            if data is None:
                return send(chat, '⚠️ Không tải được file, thử lại.')
            return do_addkey_text(chat, uid, st['meta'], data.decode('utf-8', 'ignore'))
        return
    if text:
        on_text(m, uid, chat, text)


def on_callback(cb):
    frm = cb['from']
    uid = frm['id']
    msg = cb.get('message') or {}
    chat = (msg.get('chat') or {}).get('id', uid)
    mid = msg.get('message_id')
    d = cb.get('data', '')
    u = ensure_user(frm)
    if u['banned'] and not is_admin(uid):
        return cb_answer(cb['id'], 'Tài khoản đã bị khoá', True)
    if d.split(':')[0] in ('a', 'ak', 'ap', 'rj', 'rr'):
        if not is_admin(uid):
            return cb_answer(cb['id'], 'Bạn không phải admin', True)
        on_admin_cb(cb, uid, chat, mid, d)
    else:
        on_user_cb(cb, uid, chat, mid, d)
    if cb['id'] not in _answered:
        cb_answer(cb['id'])


_user_locks = {}
_locks_guard = threading.Lock()


def handle_update(upd):
    """Xử lý 1 update trong thread riêng; khoá theo user để tránh chạy song song cùng 1 người"""
    try:
        src = (upd.get('callback_query') or upd.get('message') or {}).get('from') or {}
        with _locks_guard:
            lk = _user_locks.setdefault(src.get('id', 0), threading.Lock())
        with lk:
            if 'callback_query' in upd:
                on_callback(upd['callback_query'])
            elif 'message' in upd:
                on_message(upd['message'])
    except Exception:
        traceback.print_exc()


# ============================================================
# PHẦN 7: FLASK - WEBHOOK, AUTH, WEB ADMIN API
# ============================================================
app = Flask(__name__)
app.secret_key = SECRET
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=PUBLIC_URL.startswith('https'),
                  SESSION_COOKIE_SAMESITE='Lax', MAX_CONTENT_LENGTH=50 * 1024 * 1024)


@app.get('/health')
def health():
    return jsonify(ok=True, users=q1('SELECT COUNT(*) n FROM users')['n'],
                   orders_pending=q1("SELECT COUNT(*) n FROM orders WHERE status='pending'")['n'],
                   topups_pending=q1("SELECT COUNT(*) n FROM topups WHERE status='pending'")['n'],
                   uptime=int(time.time() - START_TS))


@app.post('/telegram/webhook')
def webhook():
    """Xác thực secret -> trả 200 NGAY -> xử lý ở thread riêng"""
    if not hmac.compare_digest(request.headers.get('X-Telegram-Bot-Api-Secret-Token', ''), WEBHOOK_SECRET):
        return 'forbidden', 403
    upd = request.get_json(silent=True)
    if upd:
        threading.Thread(target=handle_update, args=(upd,), daemon=True).start()
    return 'ok', 200


def cur_admin():
    uid, exp = session.get('admin_uid'), session.get('admin_exp', 0)
    if not uid or exp < now():
        return None
    return uid if is_admin(uid) else None


@app.get('/admin/login')
def admin_login():
    uid = verify_token(request.args.get('t', ''))
    if not uid or not is_admin(uid):
        return redirect('/admin/login-error')
    session.clear()
    session['admin_uid'] = uid
    session['admin_exp'] = now() + 86400  # session 24h
    return redirect('/admin')


@app.get('/admin/login-error')
def admin_login_error():
    return Response('<meta charset="utf-8"><body style="background:#0a0e1a;color:#e6f1ff;font-family:sans-serif;text-align:center;padding:80px">'
                    '<h2>🔒 Link không hợp lệ hoặc đã hết hạn</h2><p>Gửi lệnh <b>/webadmin</b> cho bot để nhận link mới.</p>', 403, mimetype='text/html')


@app.get('/admin')
def admin_page():
    if not cur_admin():
        return redirect('/admin/login-error')
    return send_from_directory(BASE_DIR, 'admin.html')


@app.get('/admin/static/<name>')
def admin_static(name):
    if name not in ('admin.css', 'admin.js'):
        return 'not found', 404
    return send_from_directory(BASE_DIR, name, max_age=0)


@app.before_request
def csrf_guard():
    if request.path.startswith('/admin/api') and request.method not in ('GET', 'HEAD') \
            and request.headers.get('X-Requested-With') != 'admin':
        return jsonify(ok=False, error='forbidden'), 403


def ok(data=None):
    return jsonify(ok=True, data=data)


def err(msg, code=400):
    return jsonify(ok=False, error=msg), code


def require_perm(perm):
    def deco(fn):
        @wraps(fn)
        def w(*a, **k):
            uid = cur_admin()
            if not uid:
                return err('unauthorized', 401)
            if perm and not has_perm(uid, perm):
                return err('Bạn không có quyền thực hiện', 403)
            return fn(*a, **k)
        return w
    return deco


def body():
    return request.get_json(silent=True) or {}


def page_of(sql_from, where, args, order, page, per=20, cols='*'):
    total = q1(f'SELECT COUNT(*) n FROM {sql_from} WHERE {where}', args)['n']
    rows = q(f'SELECT {cols} FROM {sql_from} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?', list(args) + [per, (page - 1) * per])
    return {'rows': rows, 'total': total, 'page': page, 'pages': max(1, -(-total // per))}


def pg():
    try:
        return max(1, int(request.args.get('page', 1)))
    except Exception:
        return 1


# ---- Auth / Stats ----
@app.get('/admin/api/me')
@require_perm(None)
def api_me():
    a = get_admin(cur_admin())
    return ok({'uid': a['telegram_id'], 'level': a['level'], 'permissions': a['permissions'], 'bot': BOT_USERNAME})


@app.post('/admin/api/logout')
def api_logout():
    session.clear()
    return ok()


@app.get('/admin/api/guide')
@require_perm(None)
def api_guide():
    return ok([{'cmd': c, 'desc': d} for c, d in GUIDE])


@app.get('/admin/api/stats')
@require_perm('dashboard')
def api_stats():
    pools = {t: {'free': 0, 'sold': 0, 'total': 0} for t in ALL_TIERS}
    for r in q('SELECT tier, SUM(user_id IS NULL AND archived=0) free, SUM(user_id IS NOT NULL) sold, COUNT(*) total FROM keys_pool GROUP BY tier'):
        pools[r['tier']] = {'free': r['free'] or 0, 'sold': r['sold'] or 0, 'total': r['total']}
    act = q("SELECT * FROM (SELECT 'order' kind, ref, telegram_id uid, amount, status, created_at ts FROM orders "
            "UNION ALL SELECT 'topup', ref, telegram_id, amount, status, created_at FROM topups "
            "UNION ALL SELECT 'user', CAST(telegram_id AS TEXT), telegram_id, 0, 'new', created_at FROM users) ORDER BY ts DESC LIMIT 10")
    return ok({'users': q1('SELECT COUNT(*) n FROM users')['n'],
               'orders_pending': q1("SELECT COUNT(*) n FROM orders WHERE status='pending'")['n'],
               'topups_pending': q1("SELECT COUNT(*) n FROM topups WHERE status='pending'")['n'],
               'revenue_month': revenue()['real'], 'pools': pools, 'activity': act})


# ---- Users ----
USER_SORT = {'new': 'created_at DESC', 'points': 'points DESC', 'balance': 'balance DESC'}


def user_where():
    w, a = ['1=1'], []
    s = request.args.get('search', '').strip().lstrip('@')
    if s:
        if s.isdigit():
            w.append('(telegram_id=? OR username LIKE ?)')
            a += [int(s), f'%{s}%']
        else:
            w.append('(username LIKE ? OR first_name LIKE ?)')
            a += [f'%{s}%', f'%{s}%']
    f = request.args.get('filter', 'all')
    if f == 'banned':
        w.append('banned=1')
    elif f == 'active':
        w.append('banned=0')
    elif f == 'haskey':
        w.append('EXISTS(SELECT 1 FROM keys_pool k WHERE k.user_id=users.telegram_id AND k.archived=0)')
    elif f == 'nocheckin':
        w.append('(last_checkin IS NULL OR last_checkin!=?)')
        a.append(today())
    return ' AND '.join(w), a


@app.get('/admin/api/users')
@require_perm('users')
def api_users():
    w, a = user_where()
    return ok(page_of('users', w, a, USER_SORT.get(request.args.get('sort'), USER_SORT['new']), pg()))


@app.get('/admin/api/users/export')
@require_perm('users')
def api_users_export():
    w, a = user_where()
    return csv_response(q(f'SELECT * FROM users WHERE {w} ORDER BY created_at DESC', a), 'users.csv')


def csv_response(rows, name):
    out = io.StringIO()
    if rows:
        wr = csv.DictWriter(out, fieldnames=list(rows[0].keys()))
        wr.writeheader()
        wr.writerows(rows)
    return Response('\ufeff' + out.getvalue(), mimetype='text/csv', headers={'Content-Disposition': f'attachment; filename={name}'})


@app.get('/admin/api/users/<int:uid>')
@require_perm('users')
def api_user(uid):
    u = get_user(uid)
    if not u:
        return err('Không thấy user', 404)
    return ok({'user': u,
               'orders': q('SELECT * FROM orders WHERE telegram_id=? ORDER BY created_at DESC LIMIT 50', (uid,)),
               'topups': q('SELECT * FROM topups WHERE telegram_id=? ORDER BY created_at DESC LIMIT 50', (uid,)),
               'keys': q('SELECT * FROM keys_pool WHERE user_id=? ORDER BY sold_at DESC LIMIT 50', (uid,)),
               'points': q('SELECT * FROM points_log WHERE telegram_id=? ORDER BY created_at DESC LIMIT 50', (uid,)),
               'invites': q('SELECT * FROM invite_log WHERE inviter_id=? ORDER BY created_at DESC LIMIT 50', (uid,))})


@app.post('/admin/api/users/<int:uid>/points')
@require_perm('users')
def api_user_points(uid):
    b = body()
    try:
        delta = int(b.get('delta'))
    except Exception:
        return err('delta không hợp lệ')
    if not get_user(uid):
        return err('Không thấy user', 404)
    with tx() as c:
        add_points(c, uid, delta, 'admin: ' + str(b.get('reason', ''))[:100])
    return ok(get_user(uid))


@app.post('/admin/api/users/<int:uid>/balance')
@require_perm('users')
def api_user_balance(uid):
    try:
        delta = int(body().get('delta'))
    except Exception:
        return err('delta không hợp lệ')
    if not get_user(uid):
        return err('Không thấy user', 404)
    ex('UPDATE users SET balance=MAX(balance+?,0) WHERE telegram_id=?', (delta, uid))
    return ok(get_user(uid))


@app.post('/admin/api/users/<int:uid>/<act>')
@require_perm('users')
def api_user_act(uid, act):
    if act in ('ban', 'unban'):
        if uid == ADMIN_ID:
            return err('Không thể khoá super admin')
        ex('UPDATE users SET banned=? WHERE telegram_id=?', (1 if act == 'ban' else 0, uid))
        return ok(get_user(uid))
    if act == 'message':
        text = str(body().get('text', '')).strip()
        if not text:
            return err('Nội dung trống')
        r = tg('sendMessage', chat_id=uid, text=text[:4000], parse_mode='HTML')
        if not r.get('ok'):
            r = tg('sendMessage', chat_id=uid, text=text[:4000])
        return ok() if r.get('ok') else err('Gửi thất bại: ' + r.get('description', ''))
    return err('Hành động không hợp lệ', 404)


# ---- Kho key ----
@app.get('/admin/api/keys')
@require_perm('keys')
def api_keys():
    tier = request.args.get('tier', '1d')
    if tier not in ALL_TIERS:
        return err('tier không hợp lệ')
    w, a = ['tier=?'], [tier]
    f = request.args.get('filter', 'unsold')
    w.append({'unsold': 'user_id IS NULL AND archived=0', 'sold': 'user_id IS NOT NULL AND archived=0',
              'archived': 'archived=1'}.get(f, '1=1'))
    s = request.args.get('search', '').strip()
    if s:
        w.append('(license_key LIKE ? OR CAST(user_id AS TEXT)=?)')
        a += [f'%{s}%', s]
    data = page_of('keys_pool', ' AND '.join(w), a, 'id DESC', pg())
    data['counts'] = {t: 0 for t in ALL_TIERS}
    for r in q('SELECT tier, COUNT(*) n FROM keys_pool WHERE user_id IS NULL AND archived=0 GROUP BY tier'):
        data['counts'][r['tier']] = r['n']
    return ok(data)


@app.post('/admin/api/keys')
@require_perm('keys')
def api_keys_add():
    b = body()
    if b.get('tier') not in ALL_TIERS:
        return err('tier không hợp lệ')
    ks = b.get('keys') or []
    if isinstance(ks, str):
        ks = parse_keys(ks)
    return ok(insert_keys(b['tier'], [str(k).strip() for k in ks]))


@app.post('/admin/api/keys/upload')
@require_perm('keys')
def api_keys_upload():
    tier, f = request.form.get('tier'), request.files.get('file')
    if tier not in ALL_TIERS or not f:
        return err('Thiếu tier hoặc file')
    return ok(insert_keys(tier, parse_keys(f.read().decode('utf-8', 'ignore'))))


@app.delete('/admin/api/keys/<int:kid>')
@require_perm('keys')
def api_key_del(kid):
    ex('DELETE FROM keys_pool WHERE id=?', (kid,))
    return ok()


@app.post('/admin/api/keys/clear')
@require_perm('keys')
def api_keys_clear():
    t = body().get('tier')
    if t not in ALL_TIERS:
        return err('tier không hợp lệ')
    return ok({'deleted': ex('DELETE FROM keys_pool WHERE tier=? AND user_id IS NULL', (t,))})


# ---- Đơn hàng & Nạp ví (dùng chung logic) ----
def list_tx(table):
    st = request.args.get('status', 'pending')
    w, a = '1=1', []
    if st != 'all':
        w, a = 't.status=?', [st]
    total = q1(f'SELECT COUNT(*) n FROM {table} t WHERE {w}', a)['n']
    p = pg()
    rows = q(f'SELECT t.*, u.username FROM {table} t LEFT JOIN users u ON u.telegram_id=t.telegram_id WHERE {w} '
             f'ORDER BY t.created_at DESC LIMIT 20 OFFSET ?', a + [(p - 1) * 20])
    counts = {r['status']: r['n'] for r in q(f'SELECT status, COUNT(*) n FROM {table} GROUP BY status')}
    return ok({'rows': rows, 'total': total, 'page': p, 'pages': max(1, -(-total // 20)), 'counts': counts})


@app.get('/admin/api/orders')
@require_perm('orders')
def api_orders():
    return list_tx('orders')


@app.get('/admin/api/topups')
@require_perm('topups')
def api_topups():
    return list_tx('topups')


@app.get('/admin/api/<kind>/<ref>')
@require_perm(None)
def api_tx_get(kind, ref):
    if kind not in ('orders', 'topups') or not has_perm(cur_admin(), kind):
        return err('Không có quyền hoặc không hợp lệ', 403)
    r = q1(f'SELECT * FROM {kind} WHERE ref=?', (ref,))
    return ok(r) if r else err('Không thấy', 404)


@app.post('/admin/api/<kind>/<ref>/approve')
@require_perm(None)
def api_tx_approve(kind, ref):
    if kind not in ('orders', 'topups') or not has_perm(cur_admin(), kind):
        return err('Không có quyền hoặc không hợp lệ', 403)
    good, msg = (approve_order if kind == 'orders' else approve_topup)(ref, cur_admin())
    return ok(msg) if good else err(msg)


@app.post('/admin/api/<kind>/<ref>/reject')
@require_perm(None)
def api_tx_reject(kind, ref):
    if kind not in ('orders', 'topups') or not has_perm(cur_admin(), kind):
        return err('Không có quyền hoặc không hợp lệ', 403)
    reason = str(body().get('reason', '')).strip()[:300] or 'Không có lý do'
    good, msg = (reject_order if kind == 'orders' else reject_topup)(ref, cur_admin(), reason)
    return ok(msg) if good else err(msg)


@app.get('/admin/api/bill/<kind>/<ref>')
@require_perm(None)
def api_bill(kind, ref):
    tb = {'o': 'orders', 't': 'topups'}.get(kind)
    if not tb or not has_perm(cur_admin(), tb):
        return err('forbidden', 403)
    r = q1(f'SELECT bill_file_id FROM {tb} WHERE ref=?', (ref,))
    if not r or not r['bill_file_id']:
        return err('Chưa có bill', 404)
    data = download_file(r['bill_file_id'])
    return Response(data, mimetype='image/jpeg') if data else err('Không tải được ảnh', 502)


# ---- Coupons ----
def poster(cp):
    val = f'{cp["value"]}%' if cp['value'] <= 100 else money(cp['value'])
    what = 'giảm giá đơn mua' if cp['type'] == 'discount' else 'thưởng thêm khi nạp ví'
    return (f'🎟 <b>MÃ ƯU ĐÃI {esc(STORE_NAME)}</b>\n{SEP}\n🎁 Mã: <code>{esc(cp["code"])}</code>\n'
            f'▸ Ưu đãi: {what} <b>{val}</b>\n▸ Đơn tối thiểu: {money(cp["min_order"])}\n'
            f'▸ Số lượt: {cp["max_uses"] or "Không giới hạn"}\n▸ Hạn dùng: {fmt_ts(cp["expires_at"])[:10] if cp["expires_at"] else "Không giới hạn"}\n\n'
            f'👉 Vào @{BOT_USERNAME} → 🎁 Nhập Mã Ưu Đãi')


@app.get('/admin/api/coupons')
@require_perm('coupons')
def api_coupons():
    return ok(q('SELECT * FROM coupons ORDER BY created_at DESC'))


@app.post('/admin/api/coupons')
@require_perm('coupons')
def api_coupon_new():
    b = body()
    try:
        code = re.sub(r'[^A-Za-z0-9_\-]', '', str(b['code'])).upper()
        typ, val, mn, mx, days = b['type'], int(b['value']), int(b.get('min_order') or 0), int(b.get('max_uses') or 0), int(b.get('days') or 0)
        assert code and typ in ('discount', 'topup') and val > 0
    except Exception:
        return err('Dữ liệu coupon không hợp lệ')
    if q1('SELECT 1 x FROM coupons WHERE code=?', (code,)):
        return err('Mã đã tồn tại')
    ex('INSERT INTO coupons(code,type,value,min_order,max_uses,used_count,expires_at,active,created_by,created_at) VALUES(?,?,?,?,?,0,?,1,?,?)',
       (code, typ, val, mn, mx, now() + days * 86400 if days else None, cur_admin(), now()))
    cp = q1('SELECT * FROM coupons WHERE code=?', (code,))
    cp['poster'] = poster(cp)
    return ok(cp)


@app.put('/admin/api/coupons/<code>')
@require_perm('coupons')
def api_coupon_edit(code):
    b, sets, a = body(), [], []
    for k in ('active', 'value', 'min_order', 'max_uses'):
        if k in b:
            sets.append(f'{k}=?')
            a.append(int(b[k]))
    if 'days' in b:
        sets.append('expires_at=?')
        a.append(now() + int(b['days']) * 86400 if int(b['days']) else None)
    if not sets:
        return err('Không có gì để sửa')
    ex(f'UPDATE coupons SET {",".join(sets)} WHERE code=?', a + [code.upper()])
    return ok(q1('SELECT * FROM coupons WHERE code=?', (code.upper(),)))


@app.delete('/admin/api/coupons/<code>')
@require_perm('coupons')
def api_coupon_del(code):
    ex('UPDATE coupons SET active=0 WHERE code=?', (code.upper(),))
    return ok()


@app.get('/admin/api/coupons/<code>/uses')
@require_perm('coupons')
def api_coupon_uses(code):
    return ok(q('SELECT * FROM coupon_uses WHERE code=? ORDER BY used_at DESC LIMIT 200', (code.upper(),)))


# ---- Admins (chỉ super) ----
@app.get('/admin/api/admins')
@require_perm('admins')
def api_admins():
    rows = q('SELECT a.*, u.username FROM admins a LEFT JOIN users u ON u.telegram_id=a.telegram_id ORDER BY added_at')
    for r in rows:
        r['permissions'] = (get_admin(r['telegram_id']) or {}).get('permissions', [])
    return ok({'rows': rows, 'all_perms': PERMS})


@app.post('/admin/api/admins')
@require_perm('admins')
def api_admin_add():
    b = body()
    try:
        uid, lv = int(b['telegram_id']), b.get('level', 'admin')
        assert lv in LEVEL_DEFAULT
    except Exception:
        return err('Dữ liệu không hợp lệ')
    ex('INSERT INTO admins(telegram_id,level,permissions,added_by,added_at) VALUES(?,?,?,?,?) '
       'ON CONFLICT(telegram_id) DO UPDATE SET level=excluded.level,permissions=excluded.permissions',
       (uid, lv, json.dumps(LEVEL_DEFAULT[lv]), cur_admin(), now()))
    return ok()


@app.put('/admin/api/admins/<int:uid>')
@require_perm('admins')
def api_admin_edit(uid):
    b = body()
    a = get_admin(uid)
    if not a:
        return err('Không thấy admin', 404)
    if a['level'] == 'super':
        return err('Super admin có Full Access, không thể sửa')
    lv = b.get('level', a['level'])
    if lv not in LEVEL_DEFAULT or lv == 'super' and uid != ADMIN_ID:
        lv = a['level']
    perms = [p for p in b.get('permissions', a['permissions']) if p in PERMS and p != 'admins']
    ex('UPDATE admins SET level=?,permissions=? WHERE telegram_id=?', (lv, json.dumps(perms), uid))
    return ok()


@app.delete('/admin/api/admins/<int:uid>')
@require_perm('admins')
def api_admin_del(uid):
    a = get_admin(uid)
    if a and a['level'] == 'super':
        return err('Không thể xoá super admin')
    ex('DELETE FROM admins WHERE telegram_id=?', (uid,))
    return ok()


# ---- Broadcast / Revenue ----
@app.post('/admin/api/broadcast')
@require_perm('broadcast')
def api_broadcast():
    b = body()
    text = str(b.get('text', '')).strip()
    if not text:
        return err('Nội dung trống')
    jid = start_broadcast(text, b.get('audience', 'all'))
    return ok({'job_id': jid, 'total': JOBS[jid]['total']})


@app.get('/admin/api/broadcast/<jid>')
@require_perm('broadcast')
def api_broadcast_job(jid):
    j = JOBS.get(jid)
    return ok(j) if j else err('Job không tồn tại hoặc đã hết hạn', 404)


@app.get('/admin/api/revenue')
@require_perm('revenue')
def api_revenue():
    m = request.args.get('month') or datetime.now(TZ).strftime('%Y-%m')
    if not re.match(r'^\d{4}-(0[1-9]|1[0-2])$', m):
        return err('Tháng không hợp lệ')
    return ok(revenue(m))


# ---- Settings ----
NUMERIC_SETTINGS = {'order_ttl_min', 'throttle_sec', 'member_cache_sec', 'streak_days', 'streak_reward', 'checkin_miss_pct',
                    'checkin_miss_point', 'invite_reward', 'price_1d', 'price_3d', 'price_7d', 'price_life'}


@app.get('/admin/api/settings')
@require_perm('settings')
def api_settings_get():
    return ok({'settings': {k: sget(k) for k in EDITABLE_SETTINGS},
               'tiers': q('SELECT * FROM topup_tiers ORDER BY amount'),
               'qr_set': bool(sget('qr_file_id'))})


@app.post('/admin/api/settings')
@require_perm('settings')
def api_settings_save():
    b = body()
    for k, v in (b.get('settings') or {}).items():
        if k not in EDITABLE_SETTINGS:
            continue
        v = str(v).strip()
        if k in NUMERIC_SETTINGS:
            try:
                assert float(v) >= 0
            except Exception:
                return err(f'Giá trị "{k}" phải là số >= 0')
        if k == 'checkin_miss_pct' and float(v) > 100:
            return err('Tỉ lệ miss tối đa 100')
        if k == 'qr_mode' and v not in ('static', 'vietqr'):
            return err('qr_mode không hợp lệ')
        sset(k, v)
    if isinstance(b.get('tiers'), list):
        with tx() as c:
            c.execute('DELETE FROM topup_tiers')
            for t in b['tiers']:
                try:
                    c.execute('INSERT OR REPLACE INTO topup_tiers(amount,bonus) VALUES(?,?)', (int(t['amount']), int(t['bonus'])))
                except Exception:
                    pass
    return ok()


@app.post('/admin/api/settings/qr')
@require_perm('settings')
def api_settings_qr():
    f = request.files.get('file')
    if not f:
        return err('Chưa chọn ảnh')
    r = tg('sendPhoto', files={'photo': (f.filename or 'qr.jpg', f.read())}, chat_id=cur_admin(), caption='✅ Đã cập nhật ảnh QR tĩnh')
    if not r.get('ok'):
        return err('Telegram từ chối ảnh: ' + r.get('description', '') + ' (hãy /start bot trước)')
    sset('qr_file_id', r['result']['photo'][-1]['file_id'])
    sset('qr_mode', 'static')
    return ok()


# ---- Backup ----
@app.get('/admin/api/backup/db')
@require_perm('backup')
def api_backup_db():
    path = make_backup_file()
    with open(path, 'rb') as f:
        data = f.read()
    os.remove(path)
    return send_file(io.BytesIO(data), as_attachment=True, download_name='bot.db', mimetype='application/octet-stream')


@app.get('/admin/api/backup/json')
@require_perm('backup')
def api_backup_json():
    data = {t: q(f'SELECT * FROM {t}') for t in BACKUP_TABLES}
    return send_file(io.BytesIO(json.dumps(data, ensure_ascii=False).encode()), as_attachment=True,
                     download_name='backup.json', mimetype='application/json')


@app.get('/admin/api/backup/csv/<name>')
@require_perm('backup')
def api_backup_csv(name):
    if name not in ('users', 'orders'):
        return err('không hợp lệ', 404)
    return csv_response(q(f'SELECT * FROM {name}'), f'{name}.csv')


@app.post('/admin/api/backup/import')
@require_perm('backup')
def api_backup_import():
    f = request.files.get('file')
    if not f:
        return err('Chưa chọn file')
    data = f.read()
    try:
        if data[:16] == b'SQLite format 3\x00':
            fd, path = tempfile.mkstemp(suffix='.db')
            os.close(fd)
            with open(path, 'wb') as fh:
                fh.write(data)
            src = sqlite3.connect(path)
            if not src.execute("SELECT 1 FROM sqlite_master WHERE name='users'").fetchone():
                src.close()
                os.remove(path)
                return err('File .db không phải backup của bot')
            main = conn()
            src.backup(main)
            main.close()
            src.close()
            os.remove(path)
        else:
            js = json.loads(data.decode('utf-8'))
            with tx() as c:
                for t in BACKUP_TABLES:
                    if t not in js:
                        continue
                    cols = [r['name'] for r in c.execute(f'PRAGMA table_info({t})')]
                    c.execute(f'DELETE FROM {t}')
                    for row in js[t]:
                        ks = [k for k in row if k in cols]  # chỉ nhận cột có thật -> an toàn SQL
                        c.execute(f'INSERT INTO {t}({",".join(ks)}) VALUES({",".join("?" * len(ks))})', [row[k] for k in ks])
        init_db()
        return ok('Đã khôi phục dữ liệu')
    except Exception as e:
        traceback.print_exc()
        return err('Restore lỗi: ' + str(e))


# ============================================================
# PHẦN 8: BACKGROUND THREADS + MAIN
# ============================================================
def register_webhook():
    """Đăng ký webhook, retry 6 lần backoff 2/4/8/15/15/15s"""
    if not (TOKEN and PUBLIC_URL):
        print('[warn] Thiếu TELEGRAM_BOT_TOKEN hoặc PUBLIC_URL -> không đăng ký webhook')
        return
    for wait in (2, 4, 8, 15, 15, 15):
        r = tg('setWebhook', url=f'{PUBLIC_URL}/telegram/webhook', secret_token=WEBHOOK_SECRET,
               allowed_updates=['message', 'callback_query'])
        if r.get('ok'):
            print('[ok] Webhook đã đăng ký:', f'{PUBLIC_URL}/telegram/webhook')
            try:
                ensure_qr()
            except Exception:
                traceback.print_exc()
            return
        print('[webhook] lỗi, thử lại:', r.get('description'))
        time.sleep(wait)


def maintenance_loop():
    """Auto-cancel đơn quá hạn (5p), auto-clean key (30p), backup 00:00 (nếu bật)"""
    last_exp = last_clean = 0
    last_bk = today()
    while True:
        try:
            t = time.time()
            if t - last_exp >= 300:
                expire_pending()
                last_exp = t
            if t - last_clean >= 1800:
                archive_old_keys()
                last_clean = t
            if today() != last_bk:
                last_bk = today()
                if sint('auto_backup') and ADMIN_ID:
                    send_backup(ADMIN_ID)
        except Exception:
            traceback.print_exc()
        time.sleep(30)


if __name__ == '__main__':
    init_db()
    threading.Thread(target=register_webhook, daemon=True).start()
    threading.Thread(target=maintenance_loop, daemon=True).start()
    print(f'[start] PAWNOVA bot chạy cổng {PORT}')
    app.run(host='0.0.0.0', port=PORT, threaded=True)
