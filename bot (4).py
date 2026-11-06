#!/usr/bin/env python3
"""Pawnova Telegram bot and admin app. Python 3.11; Flask and requests only."""
from __future__ import annotations

import os, re, json, time, hmac, hashlib, secrets, sqlite3, threading, logging, html, random, shutil
from datetime import datetime, timezone, timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import quote

import requests
from flask import Flask, request, jsonify, redirect, make_response, session, abort

logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'), format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('pawnova')
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
ADMIN_IDS = {s.strip() for s in os.getenv('ADMIN_ID', '').split(',') if s.strip()}
PUBLIC_URL = os.getenv('PUBLIC_URL', '').rstrip('/')
BOT_USERNAME = os.getenv('BOT_USERNAME', '').lstrip('@')
GROUP_LINK = os.getenv('GROUP_LINK', '')
DB_PATH = os.getenv('DB_PATH', 'bot.db')
PORT = int(os.getenv('PORT') or '10000')
WEBHOOK_SECRET = os.getenv('TELEGRAM_WEBHOOK_SECRET', '').strip()
APP_SECRET = os.getenv('APP_SECRET', '').strip() or secrets.token_hex(32)
PAYMENT_QR_URL = os.getenv('PAYMENT_QR_URL', '').strip()
PAYMENT_QR_FILE_ID = os.getenv('PAYMENT_QR_FILE_ID', '').strip()
PAYMENT_BANK_INFO = os.getenv('PAYMENT_BANK_INFO', 'BIDV · NGUYEN VAN HOA · 8970267100').strip()
TG_API = f'https://api.telegram.org/bot{TOKEN}' if TOKEN else ''
app = Flask(__name__)
app.secret_key = APP_SECRET
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=bool(PUBLIC_URL.startswith('https://')))

SCHEMA = '''
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, telegram_id INTEGER UNIQUE NOT NULL, username TEXT, first_name TEXT, points INTEGER NOT NULL DEFAULT 0, wallet INTEGER NOT NULL DEFAULT 0, checkin_date TEXT, streak INTEGER NOT NULL DEFAULT 0, referred_by INTEGER, banned INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS admins(telegram_id INTEGER PRIMARY KEY, role TEXT NOT NULL DEFAULT 'admin' CHECK(role IN ('super','admin','viewer')), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS keys_pool(id INTEGER PRIMARY KEY, key_text TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'free' CHECK(status IN ('free','sold')), sold_to INTEGER, sold_at TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS topup_tiers(id INTEGER PRIMARY KEY, title TEXT NOT NULL, price INTEGER NOT NULL, points INTEGER NOT NULL, duration_days INTEGER NOT NULL DEFAULT 30, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, tier_id INTEGER, amount INTEGER NOT NULL, points INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', key_id INTEGER, created_at TEXT NOT NULL, paid_at TEXT, FOREIGN KEY(user_id) REFERENCES users(id));
CREATE TABLE IF NOT EXISTS topups(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, amount INTEGER NOT NULL, points INTEGER NOT NULL DEFAULT 0, proof TEXT, status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, reviewed_at TEXT, reviewer INTEGER);
CREATE TABLE IF NOT EXISTS coupons(id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL COLLATE NOCASE, kind TEXT NOT NULL CHECK(kind IN ('points','percent')), value INTEGER NOT NULL, max_uses INTEGER NOT NULL DEFAULT 0, uses INTEGER NOT NULL DEFAULT 0, expires_at TEXT, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS coupon_uses(id INTEGER PRIMARY KEY, coupon_id INTEGER NOT NULL, user_id INTEGER NOT NULL, used_at TEXT NOT NULL, UNIQUE(coupon_id,user_id));
CREATE TABLE IF NOT EXISTS pending_state(user_id INTEGER PRIMARY KEY, state TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pending_discount(user_id INTEGER PRIMARY KEY, code TEXT NOT NULL, discount INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS invite_log(id INTEGER PRIMARY KEY, inviter_id INTEGER NOT NULL, invitee_id INTEGER NOT NULL UNIQUE, rewarded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status); CREATE INDEX IF NOT EXISTS idx_keys_status ON keys_pool(status); CREATE INDEX IF NOT EXISTS idx_topups_status ON topups(status);
'''
def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')
def db():
    con = sqlite3.connect(DB_PATH, timeout=20, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON'); con.execute('PRAGMA busy_timeout=20000')
    return con

def init_db():
    con=db()
    try:
        con.executescript(SCHEMA)
        for tid, title, price, points, days in [(1,'Gói 1 tháng',50000,100,30),(2,'Gói 3 tháng',135000,300,90),(3,'Gói 12 tháng',480000,1200,365)]:
            con.execute('INSERT OR IGNORE INTO topup_tiers(id,title,price,points,duration_days) VALUES(?,?,?,?,?)',(tid,title,price,points,days))
        for aid in ADMIN_IDS:
            if aid.isdigit(): con.execute("INSERT OR IGNORE INTO admins(telegram_id,role,created_at) VALUES(?,'super',?)",(int(aid),now()))
    finally: con.close()

init_db()

def run_tx(fn):
    con=db()
    try:
        con.execute('BEGIN IMMEDIATE'); result=fn(con); con.commit(); return result
    except Exception:
        con.rollback(); raise
    finally: con.close()

def tg(method, payload=None, timeout=12):
    if not TOKEN: return {'ok':False,'description':'Bot API disabled'}
    try:
        r=requests.post(f'{TG_API}/{method}', json=payload or {}, timeout=timeout)
        return r.json() if r.headers.get('content-type','').startswith('application/json') else {'ok':False,'description':'Invalid Telegram response'}
    except requests.RequestException as e:
        log.warning('Telegram API request failed (%s)', type(e).__name__); return {'ok':False,'description':'Telegram API unavailable'}
def send(chat_id, text, markup=None):
    p={'chat_id':chat_id,'text':text,'parse_mode':'HTML','disable_web_page_preview':True}
    if markup: p['reply_markup']=markup
    return tg('sendMessage',p)
def send_payment_qr(chat_id, caption):
    """Gửi QR thanh toán bằng Telegram file_id hoặc URL ảnh trực tiếp."""
    photo=PAYMENT_QR_FILE_ID or PAYMENT_QR_URL
    if not photo:
        return send(chat_id,caption+'\n\n⚠️ Admin chưa cấu hình ảnh QR (PAYMENT_QR_URL hoặc PAYMENT_QR_FILE_ID).')
    return tg('sendPhoto',{'chat_id':chat_id,'photo':photo,'caption':caption,'parse_mode':'HTML'})
def answer_cb(cid,text=''):
    if cid: tg('answerCallbackQuery',{'callback_query_id':cid,'text':text[:180]})
def safe(s): return html.escape(str(s or ''), quote=True)
def keyboard(rows): return {'inline_keyboard':rows}
def button(text,data=None,url=None):
    x={'text':text}
    if url: x['url']=url
    else: x['callback_data']=data
    return x

def user_row(con, tid, info=None):
    info=info or {}; con.execute('INSERT OR IGNORE INTO users(telegram_id,username,first_name,created_at) VALUES(?,?,?,?)',(tid,info.get('username'),info.get('first_name',''),now()))
    if info: con.execute('UPDATE users SET username=?,first_name=? WHERE telegram_id=?',(info.get('username'),info.get('first_name',''),tid))
    return con.execute('SELECT * FROM users WHERE telegram_id=?',(tid,)).fetchone()
def role_for(tid):
    con=db()
    try:
        r=con.execute('SELECT role FROM admins WHERE telegram_id=?',(tid,)).fetchone()
        return r['role'] if r else None
    finally: con.close()
def has_role(tid, allowed=('super','admin')): return role_for(tid) in allowed

def member_gate(uid):
    con=db()
    try: channels=[r['value'] for r in con.execute("SELECT value FROM settings WHERE key LIKE 'channel:%'")]
    finally: con.close()
    for chat in channels:
        try:
            r=tg('getChatMember',{'chat_id':chat,'user_id':uid})
            if r.get('ok'):
                status=(r.get('result') or {}).get('status')
                if status not in ('member','administrator','creator'): return False
            # Fail open only for API errors; a successful non-member check is denied.
            else: log.warning('Membership check API error; failing open')
        except Exception: log.exception('Membership check failed; failing open')
    return True

def menu(uid):
    return send(uid,'<b>Pawnova</b> — chọn chức năng:', keyboard([[button('✅ Điểm danh','checkin'),button('🛒 Mua key','buy')],[button('💳 Nạp ví','topup'),button('🎟 Coupon','coupon')],[button('🔑 Key của tôi','mykeys'),button('👥 Giới thiệu','referral')]]))
def set_state(uid,state,data=None):
    run_tx(lambda c:c.execute('INSERT INTO pending_state(user_id,state,data,updated_at) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET state=excluded.state,data=excluded.data,updated_at=excluded.updated_at',(uid,state,json.dumps(data or {}),now())))
def clear_state(uid): run_tx(lambda c:c.execute('DELETE FROM pending_state WHERE user_id=?',(uid,)))
def get_state(uid):
    con=db()
    try:
        r=con.execute('SELECT * FROM pending_state WHERE user_id=?',(uid,)).fetchone(); return (r['state'],json.loads(r['data'] or '{}')) if r else (None,{})
    finally: con.close()

def do_checkin(tid, info):
    if not member_gate(tid): return send(tid,'Bạn cần tham gia các kênh bắt buộc trước khi điểm danh.')
    def op(c):
        u=user_row(c,tid,info); day=datetime.now(timezone.utc).date().isoformat()
        if u['banned']: return ('banned',None)
        if u['checkin_date']==day:return ('done',None)
        yesterday=(datetime.now(timezone.utc).date()-timedelta(days=1)).isoformat()
        streak=(u['streak']+1 if u['checkin_date']==yesterday else 1)
        if random.random()<.55:
            c.execute('UPDATE users SET points=points+1,checkin_date=?,streak=? WHERE telegram_id=?',(day,streak,tid)); reward='point'
        else:
            key=c.execute("SELECT id,key_text FROM keys_pool WHERE status='free' ORDER BY RANDOM() LIMIT 1").fetchone()
            if key:
                c.execute("UPDATE keys_pool SET status='sold',sold_to=?,sold_at=? WHERE id=? AND status='free'",(tid,now(),key['id']))
                c.execute('UPDATE users SET checkin_date=?,streak=? WHERE telegram_id=?',(day,streak,tid)); reward=('key',key['key_text'])
            else:
                c.execute('UPDATE users SET points=points+1,checkin_date=?,streak=? WHERE telegram_id=?',(day,streak,tid)); reward='point'
        if streak==7:
            c.execute('UPDATE users SET points=points+5 WHERE telegram_id=?',(tid,))
        # Invite reward is deliberately issued only after the invitee's first successful check-in.
        if u['checkin_date'] is None and u['referred_by']:
            changed=c.execute('INSERT OR IGNORE INTO invite_log(inviter_id,invitee_id,rewarded_at) VALUES(?,?,?)',(u['referred_by'],tid,now())).rowcount
            if changed: c.execute('UPDATE users SET points=points+3 WHERE telegram_id=?',(u['referred_by'],))
        return ('ok',(reward,streak))
    status,data=run_tx(op)
    if status=='banned': return send(tid,'Tài khoản của bạn đang bị hạn chế.')
    if status=='done': return send(tid,'Hôm nay bạn đã điểm danh rồi.')
    reward,streak=data
    msg='Điểm danh thành công: +1 điểm.' if reward=='point' else 'Điểm danh thành công! Key của bạn: <code>'+safe(reward[1])+'</code>'
    if streak==7: msg+='\nChuỗi 7 ngày: thưởng thêm +5 điểm!'
    return send(tid,msg)

def create_referral(tid, ref):
    try: rid=int(ref)
    except: return
    if rid==tid:return
    run_tx(lambda c:(user_row(c,tid),user_row(c,rid),c.execute('UPDATE users SET referred_by=? WHERE telegram_id=? AND referred_by IS NULL',(rid,tid))))

def show_tiers(tid):
    con=db()
    try: tiers=con.execute('SELECT * FROM topup_tiers WHERE active=1 ORDER BY price').fetchall()
    finally: con.close()
    rows=[[button(f"{safe(t['title'])} · {t['price']:,}đ · {t['points']} điểm",f"tier:{t['id']}")] for t in tiers]
    send(tid,'<b>Chọn gói mua:</b>',keyboard(rows or [[button('Chưa có gói','noop')]]))

def handle_text(msg):
    fr=msg.get('from') or {}; tid=fr.get('id'); chat=(msg.get('chat') or {}).get('id'); text=(msg.get('text') or '').strip()
    if not tid or not chat:return
    con=db()
    try: u=user_row(con,tid,fr); banned=u['banned']
    finally: con.close()
    if banned:return send(chat,'Tài khoản của bạn đang bị hạn chế.')
    if text.startswith('/start'):
        bits=text.split(maxsplit=1)
        if len(bits)>1:create_referral(tid,bits[1])
        return menu(chat)
    cmd=text.split()[0].split('@')[0].lower() if text else ''
    if cmd=='/help': return menu(chat)
    if cmd=='/admin':
        return send(chat,'Lệnh quản trị: /webadmin /addkey /cleanup /setadmin /deladmin /settier /deltier /newcp /delcp /listcp /revenue /orders /userinfo /invites /ban /unban /broadcast /backup' if has_role(tid) else 'Không có quyền.')
    if cmd=='/webadmin' and has_role(tid):
        expiry=int(time.time())+300; data=f'{tid}:{expiry}'; sig=hmac.new(APP_SECRET.encode(),data.encode(),hashlib.sha256).hexdigest(); url=f'{PUBLIC_URL}/admin/login?uid={tid}&exp={expiry}&sig={sig}' if PUBLIC_URL else 'PUBLIC_URL chưa cấu hình.'
        return send(chat, safe(url))
    if cmd=='/addkey' and has_role(tid):
        value=text.partition(' ')[2].strip()
        if not value:return send(chat,'Dùng: /addkey KEY')
        try: run_tx(lambda c:c.execute('INSERT INTO keys_pool(key_text,created_at) VALUES(?,?)',(value,now()))); return send(chat,'Đã thêm key.')
        except sqlite3.IntegrityError:return send(chat,'Key đã tồn tại.')
    if cmd=='/cleanup' and has_role(tid):
        n=run_tx(lambda c:c.execute("DELETE FROM pending_state WHERE updated_at<?",((datetime.now(timezone.utc)-timedelta(days=2)).isoformat(),)).rowcount); return send(chat,f'Đã dọn {n} trạng thái chờ cũ.')
    if cmd in ('/setadmin','/deladmin') and has_role(tid,('super',)):
        p=text.split()
        if len(p)<2:return send(chat,f'Dùng: {cmd} TELEGRAM_ID [admin|viewer]')
        try: aid=int(p[1])
        except:return send(chat,'ID không hợp lệ.')
        if cmd=='/deladmin':run_tx(lambda c:c.execute('DELETE FROM admins WHERE telegram_id=?',(aid,))); return send(chat,'Đã xóa admin.')
        role=p[2] if len(p)>2 and p[2] in ('admin','viewer') else 'admin'; run_tx(lambda c:c.execute('INSERT INTO admins(telegram_id,role,created_at) VALUES(?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET role=excluded.role',(aid,role,now()))); return send(chat,'Đã cập nhật quyền.')
    if cmd=='/settier' and has_role(tid):
        p=text.split(maxsplit=5)
        if len(p)<5:return send(chat,'Dùng: /settier ID GIÁ ĐIỂM NGÀY [TÊN]')
        try:i,price,points,days=map(int,p[1:5]); title=p[5] if len(p)>5 else f'Gói {i}'
        except:return send(chat,'Thông số không hợp lệ.')
        run_tx(lambda c:c.execute('INSERT INTO topup_tiers(id,title,price,points,duration_days,active) VALUES(?,?,?,?,?,1) ON CONFLICT(id) DO UPDATE SET title=excluded.title,price=excluded.price,points=excluded.points,duration_days=excluded.duration_days,active=1',(i,title,price,points,days))); return send(chat,'Đã lưu gói.')
    if cmd=='/deltier' and has_role(tid):
        try:i=int(text.split()[1]); run_tx(lambda c:c.execute('UPDATE topup_tiers SET active=0 WHERE id=?',(i,))); return send(chat,'Đã tắt gói.')
        except:return send(chat,'Dùng: /deltier ID')
    if cmd=='/newcp' and has_role(tid):
        p=text.split()
        if len(p)<4:return send(chat,'Dùng: /newcp CODE points|percent VALUE [MAX_USES]')
        try:run_tx(lambda c:c.execute('INSERT INTO coupons(code,kind,value,max_uses) VALUES(?,?,?,?)',(p[1],p[2],int(p[3]),int(p[4]) if len(p)>4 else 0))); return send(chat,'Đã tạo coupon.')
        except Exception:return send(chat,'Coupon không hợp lệ hoặc đã tồn tại.')
    if cmd=='/delcp' and has_role(tid):
        p=text.split(); run_tx(lambda c:c.execute('UPDATE coupons SET active=0 WHERE code=?',(p[1],))) if len(p)>1 else None; return send(chat,'Đã tắt coupon.')
    if cmd=='/listcp' and has_role(tid):
        con=db(); rows=con.execute('SELECT code,kind,value,uses,max_uses,active FROM coupons ORDER BY id DESC LIMIT 50').fetchall(); con.close(); return send(chat,'<b>Coupons</b>\n'+'\n'.join(f"{safe(r['code'])} — {r['kind']} {r['value']} — {r['uses']}/{r['max_uses'] or '∞'} — {'on' if r['active'] else 'off'}" for r in rows) or 'Trống')
    if cmd in ('/revenue','/orders','/userinfo','/invites','/ban','/unban','/broadcast','/backup') and has_role(tid):
        return admin_command(cmd,text,chat,tid)
    state,data=get_state(tid)
    if state=='coupon': return redeem_coupon(tid,chat,text)
    if state=='topup_amount':
        try: amount=int(text.replace(',','').replace('.',''))
        except:return send(chat,'Nhập số tiền hợp lệ (VND).')
        if amount<1000:return send(chat,'Số tiền tối thiểu 1.000đ.')
        set_state(tid,'topup_proof',{'amount':amount}); return send(chat,f'Chuyển khoản {amount:,}đ và gửi nội dung/ảnh biên lai tại đây. Admin sẽ duyệt thủ công.')
    if state=='topup_proof':
        amount=int(data.get('amount',0)); proof=msg.get('photo',[])[-1].get('file_id') if msg.get('photo') else text[:2000]
        con=db(); u=con.execute('SELECT id FROM users WHERE telegram_id=?',(tid,)).fetchone(); con.close()
        run_tx(lambda c:c.execute("INSERT INTO topups(user_id,amount,proof,created_at) VALUES(?,?,?,?)",(u['id'],amount,proof,now()))); clear_state(tid); return send(chat,'Đã gửi yêu cầu nạp, vui lòng chờ admin duyệt.')
    return menu(chat)

def redeem_coupon(tid,chat,code):
    def op(c):
        u=user_row(c,tid); cp=c.execute('SELECT * FROM coupons WHERE code=? COLLATE NOCASE AND active=1',(code.strip(),)).fetchone()
        if not cp:return 'invalid'
        if cp['expires_at'] and cp['expires_at']<now():return 'expired'
        if cp['max_uses'] and cp['uses']>=cp['max_uses']:return 'limit'
        try:c.execute('INSERT INTO coupon_uses(coupon_id,user_id,used_at) VALUES(?,?,?)',(cp['id'],u['id'],now()))
        except sqlite3.IntegrityError:return 'used'
        if cp['kind']=='points':c.execute('UPDATE users SET points=points+? WHERE id=?',(cp['value'],u['id']))
        else:c.execute('INSERT INTO pending_discount(user_id,code,discount,created_at) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET code=excluded.code,discount=excluded.discount,created_at=excluded.created_at',(u['id'],cp['code'],min(100,cp['value']),now()))
        c.execute('UPDATE coupons SET uses=uses+1 WHERE id=?',(cp['id'],)); return 'ok' if cp['kind']=='points' else 'discount'
    result=run_tx(op); clear_state(tid)
    return send(chat,{'invalid':'Coupon không tồn tại.','expired':'Coupon hết hạn.','limit':'Coupon đã hết lượt.','used':'Bạn đã dùng coupon này.','ok':'Áp dụng thành công, điểm đã được cộng.','discount':'Đã lưu ưu đãi cho đơn mua tiếp theo.'}.get(result,'Lỗi coupon.'))

def admin_command(cmd,text,chat,tid):
    p=text.split(maxsplit=2); con=db()
    try:
        if cmd=='/revenue':
            r=con.execute("SELECT COALESCE(SUM(amount),0) total,COUNT(*) n FROM topups WHERE status='approved'").fetchone(); return send(chat,f"Doanh thu nạp đã duyệt: <b>{r['total']:,}đ</b> ({r['n']} giao dịch).")
        if cmd=='/orders':
            rows=con.execute('SELECT o.*,u.telegram_id FROM orders o JOIN users u ON u.id=o.user_id ORDER BY o.id DESC LIMIT 15').fetchall(); return send(chat,'\n'.join(f"#{r['id']} user {r['telegram_id']} {r['amount']}đ — {r['status']}" for r in rows) or 'Chưa có đơn.')
        if cmd=='/userinfo' and len(p)>1:
            u=con.execute('SELECT * FROM users WHERE telegram_id=?',(p[1],)).fetchone(); return send(chat, json.dumps(dict(u),ensure_ascii=False)[:3500] if u else 'Không tìm thấy.')
        if cmd=='/invites':
            rows=con.execute('SELECT inviter_id,COUNT(*) n FROM invite_log GROUP BY inviter_id ORDER BY n DESC LIMIT 15').fetchall(); return send(chat,'\n'.join(f"{r['inviter_id']}: {r['n']} lượt thưởng" for r in rows) or 'Chưa có lượt mời.')
        if cmd in ('/ban','/unban') and len(p)>1:
            con.execute('UPDATE users SET banned=? WHERE telegram_id=?',(1 if cmd=='/ban' else 0,p[1])); return send(chat,'Đã cập nhật trạng thái.')
        if cmd=='/broadcast' and len(p)>1:
            textmsg=p[1][:3500]; ids=[r[0] for r in con.execute('SELECT telegram_id FROM users WHERE banned=0')]; threading.Thread(target=lambda:[send(i,safe(textmsg)) for i in ids],daemon=True).start(); return send(chat,f'Đã xếp gửi tới {len(ids)} người.')
        if cmd=='/backup':
            # Chat command cannot safely attach arbitrary DB contents; directs admin to protected web endpoint.
            return send(chat,'Dùng nút Sao lưu trong trang /admin đã đăng nhập để tải bản SQLite.')
    finally: con.close()

def handle_callback(q):
    fr=q.get('from') or {}; tid=fr.get('id'); chat=(q.get('message') or {}).get('chat',{}).get('id'); data=q.get('data',''); cid=q.get('id')
    if not tid:return answer_cb(cid)
    con=db()
    try:u=user_row(con,tid,fr); banned=u['banned']
    finally:con.close()
    if banned:answer_cb(cid,'Tài khoản bị hạn chế.'); return
    if data=='checkin': answer_cb(cid); return do_checkin(tid,fr)
    if data=='buy': answer_cb(cid); return show_tiers(chat)
    if data=='topup': set_state(tid,'topup_amount'); answer_cb(cid); return send(chat,'Nhập số tiền muốn nạp (VND):')
    if data=='coupon':set_state(tid,'coupon'); answer_cb(cid); return send(chat,'Nhập mã coupon:')
    if data=='mykeys':
        con=db(); rows=con.execute("SELECT key_text,sold_at FROM keys_pool WHERE sold_to=? AND status='sold' ORDER BY sold_at DESC LIMIT 20",(tid,)).fetchall(); con.close(); answer_cb(cid); return send(chat,'<b>Key của bạn</b>\n'+'\n'.join('<code>'+safe(r['key_text'])+'</code>' for r in rows) or 'Bạn chưa có key.')
    if data=='referral':answer_cb(cid); return send(chat,f"Link giới thiệu: <code>https://t.me/{safe(BOT_USERNAME)}?start={tid}</code>\nNhận +3 điểm khi người được mời điểm danh lần đầu.")
    if data.startswith('tier:'):
        try: tier_id=int(data.split(':',1)[1])
        except:answer_cb(cid,'Gói không hợp lệ.');return
        if not member_gate(tid):answer_cb(cid,'Chưa tham gia kênh bắt buộc.');return send(chat,'Vui lòng tham gia kênh trước khi mua.')
        def buyop(c):
            t=c.execute('SELECT * FROM topup_tiers WHERE id=? AND active=1',(tier_id,)).fetchone(); u=user_row(c,tid,fr)
            if not t:return ('missing',None)
            disc=c.execute('SELECT * FROM pending_discount WHERE user_id=?',(u['id'],)).fetchone(); amount=t['price'];
            if disc:amount=max(0,amount*(100-disc['discount'])//100); c.execute('DELETE FROM pending_discount WHERE user_id=?',(u['id'],))
            o=c.execute("INSERT INTO orders(user_id,tier_id,amount,points,status,created_at) VALUES(?,?,?,?,'pending',?)",(u['id'],tier_id,amount,t['points'],now())).lastrowid
            return ('ok',(o,amount,t['points']))
        status,result=run_tx(buyop); answer_cb(cid)
        if status!='ok':return send(chat,'Gói không còn khả dụng.')
        oid,amount,points=result
        caption=(f'<b>ĐƠN THANH TOÁN #{oid}</b>\n'
                 f'Số tiền: <b>{amount:,}đ</b>\n'
                 f'Nội dung chuyển khoản: <code>{oid}</code>\n'
                 f'{safe(PAYMENT_BANK_INFO)}\n\n'
                 'Chuyển khoản đúng số tiền và ghi đúng mã đơn. Sau đó gửi ảnh biên lai cho admin để duyệt.')
        send_payment_qr(chat,caption)
        return
    answer_cb(cid)

def process_update(update):
    try:
        if 'message' in update:handle_text(update['message'])
        elif 'callback_query' in update:handle_callback(update['callback_query'])
    except Exception:log.exception('Update processing failed')

@app.post('/webhook')
def webhook():
    if WEBHOOK_SECRET:
        supplied=request.headers.get('X-Telegram-Bot-Api-Secret-Token','')
        if not hmac.compare_digest(supplied,WEBHOOK_SECRET):return jsonify(ok=False),403
    if not TOKEN:return jsonify(ok=False,reason='bot disabled'),200
    upd=request.get_json(silent=True)
    if not isinstance(upd,dict):return jsonify(ok=False),400
    threading.Thread(target=process_update,args=(upd,),daemon=True).start()
    return jsonify(ok=True),200

LOGIN_TOKENS=set(); LOGIN_LOCK=threading.Lock()
def login_signature(uid,exp):return hmac.new(APP_SECRET.encode(),f'{uid}:{exp}'.encode(),hashlib.sha256).hexdigest()
@app.get('/admin/login')
def admin_login():
    uid=request.args.get('uid',''); exp=request.args.get('exp',''); sig=request.args.get('sig','')
    try: valid=uid.isdigit() and exp.isdigit() and int(exp)>=int(time.time()) and int(exp)<=int(time.time())+300 and hmac.compare_digest(sig,login_signature(uid,exp))
    except Exception: valid=False
    token=f'{uid}:{exp}:{sig}'
    with LOGIN_LOCK:
        if not valid or token in LOGIN_TOKENS:return redirect('/admin/login-error')
        LOGIN_TOKENS.add(token)
    if not has_role(int(uid)):return redirect('/admin/login-error')
    session.clear();session['uid']=int(uid);session['issued']=int(time.time());return redirect('/admin')
@app.get('/admin/login-error')
def login_error():return make_response('<h2>Đăng nhập không hợp lệ hoặc liên kết đã hết hạn. Hãy yêu cầu liên kết mới trong Telegram.</h2>',401)
def logged(f):
    @wraps(f)
    def wrap(*a,**kw):
        uid=session.get('uid');
        if not uid or int(time.time())-session.get('issued',0)>43200 or not has_role(uid):session.clear();return jsonify(error='unauthorized'),401
        return f(*a,**kw)
    return wrap

@app.get('/admin/static/<path:name>')
def admin_static(name):
    # Neighboring static assets are served only from the project directory.
    root=Path(__file__).resolve().parent; target=(root/name).resolve()
    if root not in target.parents or not target.is_file():abort(404)
    from flask import send_file
    return send_file(target)

@app.get('/<asset>.css')
@app.get('/<asset>.js')
def admin_asset(asset):
    name=asset+('.css' if request.path.endswith('.css') else '.js')
    if name not in ('admin.css','admin.js'):abort(404)
    target=Path(__file__).resolve().parent/name
    if not target.is_file():abort(404)
    from flask import send_file
    return send_file(target)

ADMIN_HTML='''<!doctype html><html lang="vi"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Pawnova Admin</title><style>body{font:15px system-ui;background:#f3f5f9;margin:0;color:#172033}header{background:#172554;color:white;padding:18px 5vw}main{max-width:1180px;margin:26px auto;padding:0 16px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}.card{background:white;border-radius:12px;padding:16px;box-shadow:0 2px 8px #10204412;margin-bottom:14px}button{padding:9px 13px;border:0;border-radius:7px;background:#315ee8;color:white;cursor:pointer}input,select,textarea{padding:9px;border:1px solid #ccd3df;border-radius:6px;margin:4px;max-width:100%}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:8px;border-bottom:1px solid #eee}pre{white-space:pre-wrap;word-break:break-word}.muted{color:#667}</style><header><b>Pawnova · Admin</b> <span id="who"></span></header><main><section class="grid" id="stats"></section><div class="card"><h3>Điều hướng</h3><button onclick="load('users')">Người dùng</button> <button onclick="load('keys')">Keys</button> <button onclick="load('orders')">Đơn hàng</button> <button onclick="load('topups')">Nạp tiền</button> <button onclick="load('coupons')">Coupons</button> <button onclick="load('admins')">Admins</button> <button onclick="load('revenue')">Doanh thu</button> <button onclick="load('settings')">Cài đặt</button> <button onclick="load('backup')">Sao lưu</button></div><div class="card"><h3 id="title">Bảng điều khiển</h3><div id="content" class="muted">Đang tải…</div></div><div class="card"><h3>Phát tin nhắn</h3><textarea id="broadcast" rows="3" style="width:90%" placeholder="Nội dung tin nhắn"></textarea><br><button onclick="broadcast()">Gửi broadcast</button></div></main><script>
async function api(p,o={}){let r=await fetch('/api/'+p,{headers:{'Content-Type':'application/json'},...o});let j=await r.json();if(!r.ok)throw Error(j.error||r.status);return j}function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}async function start(){try{let s=await api('stats');document.getElementById('stats').innerHTML=Object.entries(s).map(([k,v])=>`<div class="card"><b>${esc(k)}</b><h2>${esc(v)}</h2></div>`).join('');document.getElementById('who').textContent=' · '+esc(s.role);load('users')}catch(e){document.getElementById('content').textContent=e.message}}async function load(t){document.getElementById('title').textContent=t;let x=await api(t);if(t==='backup'){location.href='/api/backup';return}document.getElementById('content').innerHTML='<pre>'+esc(JSON.stringify(x,null,2))+'</pre>'}async function broadcast(){try{let r=await api('broadcast',{method:'POST',body:JSON.stringify({text:document.getElementById('broadcast').value})});alert('Đã xếp gửi: '+r.count)}catch(e){alert(e.message)}}start();</script></html>'''
@app.get('/admin')
def admin_page():
    if not session.get('uid'):return redirect('/admin/login-error')
    template=Path(__file__).resolve().parent/'admin.html'
    if template.is_file():
        from flask import send_file
        return send_file(template)
    return ADMIN_HTML

def api_role(roles=('admin','super','viewer')):
    uid=session.get('uid')
    if not uid or not has_role(uid,roles):return None
    return uid
def api_data():return request.get_json(silent=True) or {}
def require_api(roles=('admin','super','viewer')):
    uid=api_role(roles)
    return (uid,None) if uid else (None,(jsonify(error='unauthorized'),401))
@app.get('/api/<name>')
@logged
def api_get(name):
    uid=session['uid']; role=role_for(uid); con=db()
    try:
        if name=='stats':return jsonify(users=con.execute('SELECT COUNT(*) FROM users').fetchone()[0],free_keys=con.execute("SELECT COUNT(*) FROM keys_pool WHERE status='free'").fetchone()[0],pending_orders=con.execute("SELECT COUNT(*) FROM orders WHERE status='pending'").fetchone()[0],pending_topups=con.execute("SELECT COUNT(*) FROM topups WHERE status='pending'").fetchone()[0],role=role)
        tables={'users':'SELECT telegram_id,username,first_name,points,wallet,streak,banned,created_at FROM users ORDER BY id DESC LIMIT 200','keys':'SELECT id,key_text,status,sold_to,sold_at,created_at FROM keys_pool ORDER BY id DESC LIMIT 300','orders':'SELECT * FROM orders ORDER BY id DESC LIMIT 200','topups':'SELECT * FROM topups ORDER BY id DESC LIMIT 200','coupons':'SELECT * FROM coupons ORDER BY id DESC LIMIT 200','admins':'SELECT telegram_id,role,created_at FROM admins ORDER BY telegram_id','revenue':"SELECT status,COUNT(*) count,COALESCE(SUM(amount),0) amount FROM topups GROUP BY status",'settings':'SELECT key,value FROM settings ORDER BY key'}
        if name not in tables:return jsonify(error='not found'),404
        return jsonify([dict(r) for r in con.execute(tables[name]).fetchall()])
    finally:con.close()

@app.post('/api/<name>')
@logged
def api_post(name):
    uid=session['uid']; role=role_for(uid); data=api_data()
    if role=='viewer':return jsonify(error='forbidden'),403
    try:
        if name=='keys':
            vals=data.get('keys',data.get('key','')); vals=vals if isinstance(vals,list) else str(vals).splitlines(); vals=[v.strip() for v in vals if str(v).strip()]
            def op(c):
                n=0
                for v in vals:
                    try:c.execute('INSERT INTO keys_pool(key_text,created_at) VALUES(?,?)',(str(v)[:2000],now()));n+=1
                    except sqlite3.IntegrityError:pass
                return n
            return jsonify(added=run_tx(op))
        if name=='orders':
            oid=int(data['id']); action=data.get('action')
            def op(c):
                o=c.execute("SELECT * FROM orders WHERE id=? AND status='pending'",(oid,)).fetchone()
                if not o:return False
                if action=='approve':
                    c.execute("UPDATE orders SET status='approved',paid_at=? WHERE id=?",(now(),oid));c.execute('UPDATE users SET points=points+? WHERE id=?',(o['points'],o['user_id']))
                elif action=='reject':c.execute("UPDATE orders SET status='rejected' WHERE id=?",(oid,))
                else:return False
                return True
            return jsonify(updated=run_tx(op))
        if name=='topups':
            oid=int(data['id']); action=data.get('action')
            def op(c):
                r=c.execute("SELECT * FROM topups WHERE id=? AND status='pending'",(oid,)).fetchone()
                if not r:return False
                if action=='approve':c.execute("UPDATE topups SET status='approved',reviewed_at=?,reviewer=? WHERE id=?",(now(),uid,oid));c.execute('UPDATE users SET wallet=wallet+? WHERE id=?',(r['amount'],r['user_id']))
                elif action=='reject':c.execute("UPDATE topups SET status='rejected',reviewed_at=?,reviewer=? WHERE id=?",(now(),uid,oid))
                else:return False
                return True
            return jsonify(updated=run_tx(op))
        if name=='coupons':
            if data.get('action')=='delete':run_tx(lambda c:c.execute('UPDATE coupons SET active=0 WHERE id=?',(int(data['id']),)));return jsonify(ok=True)
            run_tx(lambda c:c.execute('INSERT INTO coupons(code,kind,value,max_uses,expires_at) VALUES(?,?,?,?,?)',(str(data['code']),data.get('kind','points'),int(data['value']),int(data.get('max_uses',0)),data.get('expires_at'))));return jsonify(ok=True)
        if name=='admins':
            if role!='super':return jsonify(error='super admin required'),403
            aid=int(data['telegram_id']);act=data.get('action','set');
            if act=='delete':run_tx(lambda c:c.execute('DELETE FROM admins WHERE telegram_id=?',(aid,)))
            else:
                r=data.get('role','admin')
                if r not in ('admin','viewer'):return jsonify(error='invalid role'),400
                run_tx(lambda c:c.execute('INSERT INTO admins(telegram_id,role,created_at) VALUES(?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET role=excluded.role',(aid,r,now())))
            return jsonify(ok=True)
        if name=='users':
            tid=int(data['telegram_id']);act=data.get('action')
            if act in ('ban','unban'):run_tx(lambda c:c.execute('UPDATE users SET banned=? WHERE telegram_id=?',(int(act=='ban'),tid)))
            elif act=='points':run_tx(lambda c:c.execute('UPDATE users SET points=points+? WHERE telegram_id=?',(int(data['delta']),tid)))
            return jsonify(ok=True)
        if name=='broadcast':
            text=str(data.get('text','')).strip()
            if not text:return jsonify(error='text required'),400
            con=db(); ids=[r[0] for r in con.execute('SELECT telegram_id FROM users WHERE banned=0')];con.close()
            threading.Thread(target=lambda:[send(i,safe(text[:3500])) for i in ids],daemon=True).start();return jsonify(count=len(ids))
        if name=='settings':
            key=str(data['key']);value=str(data.get('value',''))
            if not re.fullmatch(r'(channel:[\w@-]+|qr_mode|qr_file_id|qr_template|payment_info|group_link|welcome_text)',key):return jsonify(error='invalid setting'),400
            if key=='qr_mode' and value not in ('static','dynamic'):return jsonify(error='qr_mode must be static or dynamic'),400
            run_tx(lambda c:c.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,value[:2000])));return jsonify(ok=True)
        if name=='backup':return jsonify(error='use GET'),405
        return jsonify(error='not found'),404
    except (ValueError,KeyError,sqlite3.IntegrityError) as e:return jsonify(error='invalid request'),400
    except Exception:log.exception('Admin API error');return jsonify(error='internal error'),500

@app.get('/api/backup')
@logged
def api_backup():
    if role_for(session['uid'])=='viewer':return jsonify(error='forbidden'),403
    from flask import send_file
    # SQLite online backup avoids copying a live database file inconsistently.
    import tempfile
    fd,path=__import__('tempfile').mkstemp(prefix='pawnova-',suffix='.db');os.close(fd)
    src=db(); dst=sqlite3.connect(path)
    try:src.backup(dst)
    finally:src.close();dst.close()
    resp=send_file(path,as_attachment=True,download_name='pawnova-backup.db',mimetype='application/octet-stream')
    @resp.call_on_close
    def cleanup():
        try:os.unlink(path)
        except OSError:pass
    return resp

@app.get('/health')
def health():return jsonify(status='ok',bot_api_enabled=bool(TOKEN))
@app.get('/')
def index():return redirect('/admin')

def background_maintenance():
    while True:
        try:
            cutoff=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
            run_tx(lambda c:(c.execute('DELETE FROM pending_state WHERE updated_at<?',(cutoff,)),c.execute('DELETE FROM pending_discount WHERE created_at<?',(cutoff,))))
        except Exception:log.exception('Maintenance failed')
        time.sleep(3600)

def setup_webhook():
    if not TOKEN or not PUBLIC_URL:return
    payload={'url':PUBLIC_URL+'/webhook','allowed_updates':['message','callback_query']}
    if WEBHOOK_SECRET:payload['secret_token']=WEBHOOK_SECRET
    delay=3
    for _ in range(8):
        result=tg('setWebhook',payload,timeout=20)
        if result.get('ok'):log.info('Telegram webhook configured');return
        log.warning('Webhook setup failed; retrying (credentials omitted)');time.sleep(delay);delay=min(delay*2,60)

def cleanup_expired_sessions():
    while True:
        time.sleep(3600)
        # Keep the in-memory one-time token set bounded; each token is valid for five minutes.
        with LOGIN_LOCK:
            cutoff=int(time.time())-600
            LOGIN_TOKENS.intersection_update({t for t in LOGIN_TOKENS if int(t.split(':',1)[1])>=cutoff})

if __name__=='__main__':
    threading.Thread(target=background_maintenance,daemon=True).start()
    threading.Thread(target=cleanup_expired_sessions,daemon=True).start()
    threading.Thread(target=setup_webhook,daemon=True).start()
    app.run(host='0.0.0.0',port=PORT,threaded=True)
