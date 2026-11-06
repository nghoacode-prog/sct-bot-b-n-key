#!/usr/bin/env python3
"""Pawnova Telegram bot and Flask admin API; Flask and requests only."""
from __future__ import annotations
import os,re,json,time,hmac,hashlib,secrets,sqlite3,threading,logging,html,random
from datetime import datetime,timezone,timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import quote
import requests
from flask import Flask,request,jsonify,redirect,make_response,session,abort

logging.basicConfig(level=os.getenv('LOG_LEVEL','INFO'),format='%(asctime)s %(levelname)s %(message)s'); log=logging.getLogger('pawnova')
TOKEN=os.getenv('TELEGRAM_BOT_TOKEN','').strip(); ADMIN_IDS={x.strip() for x in os.getenv('ADMIN_ID','').split(',') if x.strip()}; PUBLIC_URL=os.getenv('PUBLIC_URL','').rstrip('/'); BOT_USERNAME=os.getenv('BOT_USERNAME','').lstrip('@'); GROUP_LINK=os.getenv('GROUP_LINK',''); DB_PATH=os.getenv('DB_PATH','bot.db'); PORT=int(os.getenv('PORT') or '10000'); WEBHOOK_SECRET=os.getenv('TELEGRAM_WEBHOOK_SECRET','').strip(); APP_SECRET=os.getenv('APP_SECRET','').strip() or secrets.token_hex(32); PAYMENT_QR_URL=os.getenv('PAYMENT_QR_URL','').strip(); PAYMENT_QR_FILE_ID=os.getenv('PAYMENT_QR_FILE_ID','').strip(); PAYMENT_BANK_INFO=os.getenv('PAYMENT_BANK_INFO','BIDV · NGUYEN VAN HOA · 8970267100').strip(); TG_API=f'https://api.telegram.org/bot{TOKEN}' if TOKEN else ''
CHANNELS=[x.strip() for x in os.getenv('REQUIRED_CHANNELS','@PawTool789,@PAW8386,@pawtoolvip').split(',') if x.strip()]; GROUP_ID=os.getenv('REQUIRED_GROUP_ID','-1004376361739')
app=Flask(__name__); app.secret_key=APP_SECRET; app.config.update(SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE='Lax',SESSION_COOKIE_SECURE=PUBLIC_URL.startswith('https://'))
TIERS={'15m':('Miễn phí 15 phút',900,0),'30m':('Miễn phí 30 phút',1800,0),'1h':('Miễn phí 1 giờ',3600,0),'1d':('1 ngày',86400,22000),'3d':('3 ngày',259200,55000),'7d':('7 ngày',604800,105000),'life':('Vĩnh viễn',None,270000)}
TOPUP_BONUSES={100000:10000,500000:100000,1000000:250000}

def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')
def day7(): return (datetime.now(timezone.utc)+timedelta(hours=7)).date().isoformat()
def db():
 c=sqlite3.connect(DB_PATH,timeout=20,isolation_level=None); c.row_factory=sqlite3.Row; c.execute('PRAGMA foreign_keys=ON'); c.execute('PRAGMA busy_timeout=20000'); return c
def run_tx(fn):
 c=db()
 try:c.execute('BEGIN IMMEDIATE'); r=fn(c); c.commit(); return r
 except Exception:c.rollback(); raise
 finally:c.close()
def tg(method,payload=None,timeout=12):
 if not TOKEN:return {'ok':False,'description':'Bot API disabled'}
 try:
  r=requests.post(f'{TG_API}/{method}',json=payload or {},timeout=timeout); return r.json() if 'json' in r.headers.get('content-type','') else {'ok':False}
 except requests.RequestException as e:log.warning('Telegram API error: %s',type(e).__name__); return {'ok':False}
def send(uid,text,markup=None):
 p={'chat_id':uid,'text':text,'parse_mode':'HTML','disable_web_page_preview':True}
 if markup:p['reply_markup']=markup
 return tg('sendMessage',p)
def send_photo(uid,photo,caption,markup=None):
 p={'chat_id':uid,'photo':photo,'caption':caption,'parse_mode':'HTML'}
 if markup:p['reply_markup']=markup
 return tg('sendPhoto',p)
def answer_cb(cid,text=''):
 if cid:tg('answerCallbackQuery',{'callback_query_id':cid,'text':text[:180]})
def safe(x):return html.escape(str(x or ''),quote=True)
def button(text,data=None,url=None):return {'text':text,**({'url':url} if url else {'callback_data':data})}
def kb(rows):return {'inline_keyboard':rows}
def schema_init():
 c=db()
 try:
  c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,telegram_id INTEGER UNIQUE NOT NULL,username TEXT,first_name TEXT,points INTEGER NOT NULL DEFAULT 0,wallet INTEGER NOT NULL DEFAULT 0,checkin_date TEXT,streak INTEGER NOT NULL DEFAULT 0,referred_by INTEGER,banned INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS admins(telegram_id INTEGER PRIMARY KEY,role TEXT NOT NULL DEFAULT 'admin',created_at TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS keys_pool(id INTEGER PRIMARY KEY,key_text TEXT NOT NULL UNIQUE,tier TEXT NOT NULL DEFAULT '1d',owner_id INTEGER,sold_to INTEGER,status TEXT NOT NULL DEFAULT 'free',sold_at TEXT,expires_at TEXT,archived_at TEXT,order_id INTEGER,created_at TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL,tier_id TEXT,tier TEXT,amount INTEGER NOT NULL,points INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'pending',key_id INTEGER,reference TEXT,created_at TEXT NOT NULL,paid_at TEXT,expires_at TEXT);
 CREATE TABLE IF NOT EXISTS topups(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL,amount INTEGER NOT NULL,points INTEGER NOT NULL DEFAULT 0,proof TEXT,status TEXT NOT NULL DEFAULT 'pending',created_at TEXT NOT NULL,expires_at TEXT,reviewed_at TEXT,reviewer INTEGER);
 CREATE TABLE IF NOT EXISTS coupons(id INTEGER PRIMARY KEY,code TEXT UNIQUE NOT NULL COLLATE NOCASE,kind TEXT NOT NULL,value INTEGER NOT NULL,max_uses INTEGER NOT NULL DEFAULT 0,uses INTEGER NOT NULL DEFAULT 0,expires_at TEXT,active INTEGER NOT NULL DEFAULT 1);
 CREATE TABLE IF NOT EXISTS coupon_uses(id INTEGER PRIMARY KEY,coupon_id INTEGER NOT NULL,user_id INTEGER NOT NULL,used_at TEXT NOT NULL,UNIQUE(coupon_id,user_id));
 CREATE TABLE IF NOT EXISTS pending_state(user_id INTEGER PRIMARY KEY,state TEXT NOT NULL,data TEXT NOT NULL DEFAULT '{}',updated_at TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS pending_discount(user_id INTEGER PRIMARY KEY,code TEXT NOT NULL,discount INTEGER NOT NULL,created_at TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS invite_log(id INTEGER PRIMARY KEY,inviter_id INTEGER NOT NULL,invitee_id INTEGER NOT NULL UNIQUE,rewarded_at TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS wallet_ledger(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL,delta INTEGER NOT NULL,reason TEXT NOT NULL,reference TEXT UNIQUE,created_at TEXT NOT NULL);
 CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);CREATE INDEX IF NOT EXISTS idx_keys_status ON keys_pool(status);CREATE INDEX IF NOT EXISTS idx_topups_status ON topups(status);''')
  # Add columns safely to databases from earlier bot versions.
  for table,col,decl in [('keys_pool','tier',"TEXT NOT NULL DEFAULT '1d'"),('keys_pool','owner_id','INTEGER'),('keys_pool','expires_at','TEXT'),('keys_pool','archived_at','TEXT'),('keys_pool','order_id','INTEGER'),('orders','tier','TEXT'),('orders','reference','TEXT'),('orders','expires_at','TEXT'),('orders','proof','TEXT'),('topups','expires_at','TEXT')]:
   cols={r['name'] for r in c.execute(f'PRAGMA table_info({table})')}
   if col not in cols:c.execute(f'ALTER TABLE {table} ADD COLUMN {col} {decl}')
  # Normalize old inventory and map legacy package tiers where possible.
  c.execute("UPDATE keys_pool SET owner_id=sold_to WHERE owner_id IS NULL AND sold_to IS NOT NULL")
  c.execute("UPDATE keys_pool SET tier=CASE tier WHEN 'free15m' THEN '15m' WHEN 'free30m' THEN '30m' WHEN 'free1h' THEN '1h' ELSE tier END")
  c.execute("UPDATE keys_pool SET status=CASE WHEN sold_to IS NULL THEN 'free' ELSE 'sold' END WHERE status IS NULL OR status NOT IN ('free','sold')")
  for aid in ADMIN_IDS:
   if aid.isdigit():c.execute("INSERT OR IGNORE INTO admins(telegram_id,role,created_at) VALUES(?,'super',?)",(int(aid),now()))
  c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('group_link',?)",(GROUP_LINK,))
 finally:c.close()
schema_init()

def user_row(c,tid,info=None):
 info=info or {}; c.execute('INSERT OR IGNORE INTO users(telegram_id,username,first_name,created_at) VALUES(?,?,?,?)',(tid,info.get('username'),info.get('first_name',''),now()))
 if info:c.execute('UPDATE users SET username=?,first_name=? WHERE telegram_id=?',(info.get('username'),info.get('first_name',''),tid))
 return c.execute('SELECT * FROM users WHERE telegram_id=?',(tid,)).fetchone()
def role_for(tid):
 c=db()
 try:r=c.execute('SELECT role FROM admins WHERE telegram_id=?',(tid,)).fetchone();return r['role'] if r else None
 finally:c.close()
def has_role(tid,allowed=('super','admin')):return role_for(tid) in allowed
def member_gate(uid):
 checks=CHANNELS+[GROUP_ID]
 for chat in checks:
  r=tg('getChatMember',{'chat_id':chat,'user_id':uid})
  if r.get('ok'):
   if (r.get('result') or {}).get('status') not in ('member','administrator','creator'):return False
  else:log.warning('Membership API error; failing open for %s',chat)
 return True
def join_markup():
 rows=[]
 for name in CHANNELS:rows.append([button('Tham gia '+name,url='https://t.me/'+name.lstrip('@'))])
 link=GROUP_LINK
 if link:rows.append([button('Tham gia nhóm',url=link)])
 rows.append([button('✅ Kiểm tra lại','recheck')]); return kb(rows)
def require_membership(uid,chat):
 if member_gate(uid):return True
 send(chat,'Để dùng chức năng này, hãy tham gia đủ các kênh/nhóm bắt buộc rồi bấm Kiểm tra lại.',join_markup());return False
def menu(uid):
 return send(uid,'<b>Pawnova</b> — chọn chức năng:',kb([[button('✅ Điểm danh','checkin'),button('🛒 Mua key','buy')],[button('💳 Ví / nạp tiền','wallet'),button('🔑 Key của tôi','mykeys')],[button('📋 Nhiệm vụ','tasks'),button('🎟 Coupon','coupon')],[button('🏆 BXH tháng','leaderboard'),button('👥 Giới thiệu','referral')],[button('🆘 Hỗ trợ','support')]]))
def set_state(uid,state,data=None):run_tx(lambda c:c.execute('INSERT INTO pending_state(user_id,state,data,updated_at) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET state=excluded.state,data=excluded.data,updated_at=excluded.updated_at',(uid,state,json.dumps(data or {}),now())))
def clear_state(uid):run_tx(lambda c:c.execute('DELETE FROM pending_state WHERE user_id=?',(uid,)))
def get_state(uid):
 c=db()
 try:
  r=c.execute('SELECT * FROM pending_state WHERE user_id=?',(uid,)).fetchone()
  if r and datetime.fromisoformat(r['updated_at'])<datetime.now(timezone.utc)-timedelta(minutes=15):c.execute('DELETE FROM pending_state WHERE user_id=?',(uid,));return (None,{})
  return (r['state'],json.loads(r['data'] or '{}')) if r else (None,{})
 finally:c.close()
def claim_key(c,tid,tier,order_id=None):
 row=c.execute('SELECT id,key_text FROM keys_pool WHERE tier=? AND status="free" ORDER BY id LIMIT 1',(tier,)).fetchone()
 if not row:return None
 duration=TIERS[tier][1]; exp=(datetime.now(timezone.utc)+timedelta(seconds=duration)).isoformat(timespec='seconds') if duration else None
 n=c.execute("UPDATE keys_pool SET status='sold',sold_to=?,owner_id=?,sold_at=?,expires_at=?,order_id=? WHERE id=? AND status='free'",(tid,tid,now(),exp,order_id,row['id'])).rowcount
 return (row['key_text'],exp) if n else None
def do_checkin(tid,info):
 if not require_membership(tid,tid):return
 def op(c):
  u=user_row(c,tid,info); d=day7()
  if u['banned']:return 'banned',None
  if u['checkin_date']==d:return 'done',None
  yesterday=(datetime.now(timezone.utc)+timedelta(hours=7)-timedelta(days=1)).date().isoformat(); streak=u['streak']+1 if u['checkin_date']==yesterday else 1
  if random.random()<.55:reward=('point',None);c.execute('UPDATE users SET points=points+1 WHERE id=?',(u['id'],))
  else:
   tiers=[t for t in ('15m','30m','1h') if c.execute('SELECT 1 FROM keys_pool WHERE tier=? AND status="free" LIMIT 1',(t,)).fetchone()]
   picked=claim_key(c,tid,random.choice(tiers)) if tiers else None
   if picked:reward=('key',picked[0])
   else:reward=('point',None);c.execute('UPDATE users SET points=points+1 WHERE id=?',(u['id'],))
  reset=streak>=7
  c.execute('UPDATE users SET checkin_date=?,streak=? WHERE id=?',(d,0 if reset else streak,u['id']))
  inviter_notify=None
  if reward[0]=='key' and u['referred_by']:
   inv=c.execute('SELECT id,telegram_id,banned,points FROM users WHERE telegram_id=?',(u['referred_by'],)).fetchone()
   if inv and not inv['banned'] and c.execute('INSERT OR IGNORE INTO invite_log(inviter_id,invitee_id,rewarded_at) VALUES(?,?,?)',(inv['id'],u['id'],now())).rowcount:
    c.execute('UPDATE users SET points=points+3 WHERE id=?',(inv['id'],));inviter_notify=(inv['telegram_id'],inv['points']+3)
  if reset:c.execute('UPDATE users SET points=points+5 WHERE id=?',(u['id'],))
  return 'ok',(reward,streak,reset,inviter_notify)
 status,data=run_tx(op)
 if status=='banned':return send(tid,'Tài khoản của bạn đang bị hạn chế.')
 if status=='done':return send(tid,'Hôm nay bạn đã điểm danh rồi.')
 (kind,value),streak,reset,inviter_notify=data; text='Điểm danh: +1 điểm.' if kind=='point' else 'Nhận key miễn phí: <code>'+safe(value)+'</code>'
 if reset:text+='\nChuỗi đủ 7 ngày: +5 điểm, chuỗi được đặt lại.'
 if inviter_notify:send(inviter_notify[0],f'👥 Người bạn mời vừa điểm danh nhận key. Bạn được +3 điểm; tổng hiện tại: {inviter_notify[1]}.')
 return send(tid,text)
def create_referral(tid,ref):
 try:rid=int(ref)
 except:return
 if rid==tid:return
 run_tx(lambda c:(user_row(c,tid),user_row(c,rid),c.execute('UPDATE users SET referred_by=? WHERE telegram_id=? AND referred_by IS NULL',(rid,tid))))
def show_tiers(tid):
 rows=[[button(f'{TIERS[t][0]} · {TIERS[t][2]:,}đ',f'buy:{t}')] for t in ('1d','3d','7d','life')]
 send(tid,'<b>Chọn gói key:</b>',kb(rows))
def show_wallet(tid):
 c=db()
 try:u=c.execute('SELECT wallet FROM users WHERE telegram_id=?',(tid,)).fetchone()
 finally:c.close()
 send(tid,f'<b>Số dư ví:</b> {u[0]:,}đ',kb([[button('100.000đ +10.000','topup:100000'),button('500.000đ +100.000','topup:500000')],[button('1.000.000đ +250.000','topup:1000000'),button('Số tiền khác','topup:custom')],[button('Mua key bằng ví','walletbuy')],[button('Đổi điểm lấy key','redeem_points')]]))
def send_order_prompt(chat,oid,tier,amount):
 ref=f'PNV{oid:08d}'; caption=f'<b>ĐƠN #{oid} · {safe(TIERS[tier][0])}</b>\nSố tiền: <b>{amount:,}đ</b>\nNội dung CK: <code>{ref}</code>\n{safe(PAYMENT_BANK_INFO)}\n\nThanh toán và gửi ảnh biên lai tại đây trong 15 phút.'
 qr=PAYMENT_QR_FILE_ID or PAYMENT_QR_URL
 bin_id=os.getenv('BANK_BIN','').strip();account=os.getenv('BANK_ACCOUNT','').strip()
 if not PAYMENT_QR_FILE_ID and bin_id and account:qr=f'https://img.vietqr.io/image/{quote(bin_id)}-{quote(account)}-compact2.png?amount={amount}&addInfo={quote(ref)}&accountName={quote(os.getenv("BANK_ACCOUNT_NAME", ""))}'
 if qr:send_photo(chat,qr,caption)
 else:send(chat,caption+'\n\n⚠️ QR chưa cấu hình; vui lòng chuyển khoản theo thông tin trên.')
def notify_admins(text,markup=None,photo=None):
 c=db()
 try:ids=[r[0] for r in c.execute("SELECT telegram_id FROM admins WHERE role IN ('super','admin')")]
 finally:c.close()
 for aid in ids:
  if photo:send_photo(aid,photo,text,markup)
  else:send(aid,text,markup)
def submit_proof(tid,chat,msg,state,data):
 photo=(msg.get('photo') or [])
 if not photo:return send(chat,'Vui lòng gửi ảnh biên lai dạng ảnh Telegram.')
 fid=photo[-1]['file_id']
 if state=='order_proof':
  oid=int(data.get('order_id',0)); ok=run_tx(lambda c:c.execute("UPDATE orders SET status='proof',proof=? WHERE id=? AND user_id=(SELECT id FROM users WHERE telegram_id=?) AND status='pending' AND expires_at>?",(fid,oid,tid,now())).rowcount)
  if ok:
   c=db();o=c.execute('SELECT o.*,u.telegram_id FROM orders o JOIN users u ON u.id=o.user_id WHERE o.id=?',(oid,)).fetchone();c.close();markup=kb([[button('✅ Duyệt',f'ordok:{oid}'),button('❌ Từ chối',f'ordno:{oid}')]])
   notify_admins(f'<b>Ảnh thanh toán đơn #{oid}</b> · {o["amount"]:,}đ · user <code>{tid}</code> · {safe(o["reference"])}',markup,fid);clear_state(tid);return send(chat,'Đã chuyển biên lai cho admin xét duyệt.')
  return send(chat,'Đơn không còn hiệu lực hoặc đã được xử lý.')
 amount=int(data.get('amount',0));bonus=TOPUP_BONUSES.get(amount,0);expiry=(datetime.now(timezone.utc)+timedelta(minutes=15)).isoformat(timespec='seconds');row=run_tx(lambda c:(lambda u:c.execute('INSERT INTO topups(user_id,amount,points,proof,created_at,expires_at) VALUES(?,?,?,?,?,?)',(u['id'],amount,bonus,fid,now(),expiry)).lastrowid)(user_row(c,tid)))
 clear_state(tid);notify_admins(f'<b>Yêu cầu nạp #{row}</b> · {amount:,}đ · thưởng {bonus:,}đ · user <code>{tid}</code>',kb([[button('✅ Duyệt',f'topupok:{row}'),button('❌ Từ chối',f'topupno:{row}')]]),fid);return send(chat,'Đã gửi yêu cầu nạp'+(f', thưởng thêm {bonus:,}đ' if bonus else '')+', vui lòng chờ admin duyệt.')
def handle_text(msg):
 fr=msg.get('from') or {};tid=fr.get('id');chat=(msg.get('chat') or {}).get('id');text=(msg.get('text') or '').strip()
 if not tid or not chat:return
 u=run_tx(lambda c:user_row(c,tid,fr))
 if u['banned']:return send(chat,'Tài khoản của bạn đang bị hạn chế.')
 if text.startswith('/start'):
  p=text.split(maxsplit=1)
  if len(p)>1:create_referral(tid,p[1][4:] if p[1].startswith('ref_') else p[1])
  return menu(chat)
 cmd=text.split()[0].split('@')[0].lower() if text else ''
 if cmd=='/help':return menu(chat)
 if cmd=='/webadmin' and has_role(tid):
  exp=str(int(time.time())+300);sig=hmac.new(APP_SECRET.encode(),f'{tid}:{exp}'.encode(),hashlib.sha256).hexdigest();return send(chat,('Đặt PUBLIC_URL để tạo liên kết.' if not PUBLIC_URL else '<a href="'+safe(f'{PUBLIC_URL}/admin/login?uid={tid}&exp={exp}&sig={sig}')+'">Mở trang quản trị (5 phút)</a>'))
 if cmd=='/admin':return send(chat,'Quản trị: /webadmin /addkey TIER KEY /setadmin /deladmin /cleanup' if has_role(tid) else 'Không có quyền.')
 if cmd in ('/addkey',) and has_role(tid):
  parts=text.split(maxsplit=2)
  if len(parts)<3 or parts[1] not in TIERS:return send(chat,'Dùng: /addkey 15m|30m|1h|1d|3d|7d|life KEY (có thể nhiều dòng)')
  tier=parts[1]; vals=[x.strip() for x in parts[2].splitlines() if x.strip()]
  def op(c):
   n=0
   for value in vals:
    try:c.execute('INSERT INTO keys_pool(key_text,tier,owner_id,created_at) VALUES(?,?,NULL,?)',(value[:2000],tier,now()));n+=1
    except sqlite3.IntegrityError:pass
   return n
  return send(chat,f'Đã thêm {run_tx(op)} key vào {tier}.')
 if cmd in ('/setadmin','/deladmin') and has_role(tid,('super',)):
  p=text.split()
  if len(p)<2:return send(chat,'Dùng: /setadmin ID [admin|viewer] hoặc /deladmin ID')
  try:aid=int(p[1])
  except:return send(chat,'ID không hợp lệ.')
  if cmd=='/deladmin':run_tx(lambda c:c.execute('DELETE FROM admins WHERE telegram_id=?',(aid,)));return send(chat,'Đã xóa admin.')
  role=p[2] if len(p)>2 and p[2] in ('admin','viewer') else 'admin';run_tx(lambda c:c.execute('INSERT INTO admins(telegram_id,role,created_at) VALUES(?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET role=excluded.role',(aid,role,now())));return send(chat,'Đã cập nhật quyền.')
 state,data=get_state(tid)
 if state=='coupon':return redeem_coupon(tid,chat,text)
 if state=='topup_amount':
  try:amount=int(text.replace(',','').replace('.',''))
  except:return send(chat,'Nhập số tiền VND hợp lệ.')
  if not 1000<=amount<=100000000:return send(chat,'Số tiền hợp lệ từ 1.000đ đến 100.000.000đ.')
  set_state(tid,'topup_proof',{'amount':amount});return send(chat,f'Nạp {amount:,}đ'+(f' · thưởng thêm {TOPUP_BONUSES[amount]:,}đ' if amount in TOPUP_BONUSES else '')+f' · nội dung CK: NAP{tid}. Sau đó gửi ảnh biên lai.')
 if state in ('topup_proof','order_proof'):return submit_proof(tid,chat,msg,state,data)
 # Text document uploads accepted as bulk keys for admins.
 if has_role(tid) and msg.get('document') and data.get('key_tier'):
  return send(chat,'Hãy gửi trực tiếp các key mỗi dòng sau /addkey TIER.')
 if msg.get('photo') and state is None:return send(chat,'Chưa có yêu cầu thanh toán đang chờ. Chọn Nạp ví hoặc Mua key trước.')
 return menu(chat)
def redeem_coupon(tid,chat,code):
 def op(c):
  u=user_row(c,tid);cp=c.execute('SELECT * FROM coupons WHERE code=? COLLATE NOCASE AND active=1',(code.strip(),)).fetchone()
  if not cp:return 'invalid'
  if cp['kind'] not in ('points','percent') or cp['value']<=0 or (cp['kind']=='percent' and cp['value']>100):return 'invalid'
  if cp['expires_at'] and cp['expires_at']<now():return 'expired'
  if cp['max_uses'] and cp['uses']>=cp['max_uses']:return 'limit'
  try:c.execute('INSERT INTO coupon_uses(coupon_id,user_id,used_at) VALUES(?,?,?)',(cp['id'],u['id'],now()))
  except sqlite3.IntegrityError:return 'used'
  if cp['kind']=='points':c.execute('UPDATE users SET points=points+? WHERE id=?',(cp['value'],u['id']))
  else:c.execute('INSERT INTO pending_discount(user_id,code,discount,created_at) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET code=excluded.code,discount=excluded.discount,created_at=excluded.created_at',(u['id'],cp['code'],cp['value'],now()))
  c.execute('UPDATE coupons SET uses=uses+1 WHERE id=?',(cp['id'],));return 'ok' if cp['kind']=='points' else 'discount'
 r=run_tx(op);clear_state(tid);return send(chat,{'invalid':'Coupon không hợp lệ.','expired':'Coupon hết hạn.','limit':'Coupon đã hết lượt.','used':'Bạn đã dùng coupon này.','ok':'Đã cộng điểm.','discount':'Đã lưu ưu đãi cho đơn mua tiếp theo.'}[r])
def handle_callback(q):
 fr=q.get('from') or {};tid=fr.get('id');chat=(q.get('message') or {}).get('chat',{}).get('id');data=q.get('data','');cid=q.get('id')
 if not tid:return answer_cb(cid)
 u=run_tx(lambda c:user_row(c,tid,fr))
 if u['banned']:return answer_cb(cid,'Tài khoản bị hạn chế.')
 if data.startswith(('ordok:','ordno:','topupok:','topupno:')):
  if not has_role(tid):return answer_cb(cid,'Không có quyền.')
  action,idstr=data.split(':',1)
  try:rid=int(idstr)
  except:return answer_cb(cid,'Yêu cầu sai.')
  def review(c):
   if action.startswith('ord'):
    o=c.execute('SELECT * FROM orders WHERE id=?',(rid,)).fetchone()
    if not o or o['status'] not in ('pending','proof'):return None
    if action=='ordno':c.execute("UPDATE orders SET status='rejected' WHERE id=?",(rid,));return ('order_reject',o['user_id'])
    if o['expires_at'] and o['expires_at']<now():c.execute("UPDATE orders SET status='expired' WHERE id=?",(rid,));return ('expired',o['user_id'])
    key=claim_key(c,o['user_id'] and c.execute('SELECT telegram_id FROM users WHERE id=?',(o['user_id'],)).fetchone()[0],o['tier'],rid)
    if not key:return ('no_key',o['user_id'])
    c.execute("UPDATE orders SET status='approved',paid_at=?,key_id=(SELECT id FROM keys_pool WHERE order_id=? AND status='sold') WHERE id=?",(now(),rid,rid));return ('order_ok',o['user_id'],key[0],rid)
   o=c.execute('SELECT * FROM topups WHERE id=? AND status="pending"',(rid,)).fetchone()
   if not o:return None
   if o['expires_at'] and o['expires_at']<now():c.execute("UPDATE topups SET status='expired' WHERE id=?",(rid,));return ('expired_topup',o['user_id'])
   if action=='topupno':c.execute("UPDATE topups SET status='rejected',reviewed_at=?,reviewer=? WHERE id=?",(now(),tid,rid));return ('topup_reject',o['user_id'])
   credit=o['amount']+(o['points'] or 0);c.execute("UPDATE topups SET status='approved',reviewed_at=?,reviewer=? WHERE id=?",(now(),tid,rid));c.execute('UPDATE users SET wallet=wallet+? WHERE id=?',(credit,o['user_id']));c.execute('INSERT OR IGNORE INTO wallet_ledger(user_id,delta,reason,reference,created_at) VALUES(?,?,?,?,?)',(o['user_id'],credit,'topup',f'topup:{rid}',now()));return ('topup_ok',o['user_id'],credit)
  result=run_tx(review);answer_cb(cid,'Đã xử lý.' if result else 'Đã xử lý trước đó hoặc không tồn tại.')
  if result:
   target=db();r=target.execute('SELECT telegram_id FROM users WHERE id=?',(result[1],)).fetchone();target.close();uid=r[0]
   if result[0]=='order_ok':send(uid,f'Đơn #{result[3]} đã duyệt. Key: <code>{safe(result[2])}</code>')
   elif result[0]=='topup_ok':send(uid,f'Đã cộng {result[2]:,}đ vào ví.')
   elif result[0]=='no_key':send(uid,'Đơn chưa thể duyệt: kho key hiện hết hàng.')
   elif result[0]=='expired':send(uid,'Đơn đã hết hạn (15 phút), vui lòng tạo đơn mới.')
   elif result[0]=='order_reject':send(uid,'Biên lai/đơn hàng bị từ chối. Liên hệ hỗ trợ nếu cần.')
   elif result[0]=='expired_topup':send(uid,'Yêu cầu nạp tiền đã quá thời hạn. Vui lòng tạo yêu cầu mới.')
   else:send(uid,'Yêu cầu nạp tiền bị từ chối.')
  return
 if data=='recheck':
  joined=member_gate(tid);answer_cb(cid);return send(chat,'✅ Đã tham gia đủ.' if joined else 'Còn thiếu thành viên; hãy tham gia và kiểm tra lại.',None if joined else join_markup())
 if data in ('tasks','checkin','redeem'):
  if not require_membership(tid,chat):return answer_cb(cid,'Cần tham gia kênh/nhóm.')
  answer_cb(cid)
  if data=='tasks':return send(chat,'<b>Nhiệm vụ</b>\nTham gia đủ 3 kênh và nhóm bắt buộc, sau đó điểm danh hằng ngày. Mỗi ngày: 55% +1 điểm hoặc 45% key miễn phí; chuỗi 7 ngày thưởng +5 điểm.',join_markup())
  if data=='checkin':return do_checkin(tid,fr)
  return send(chat,'Đổi điểm từ Ví → Đổi điểm.')
 if data=='buy':answer_cb(cid);return show_tiers(chat)
 if data=='wallet':answer_cb(cid);return show_wallet(chat)
 if data=='topup':set_state(tid,'topup_amount');answer_cb(cid);return send(chat,'Nhập số tiền muốn nạp (VND):')
 if data.startswith('topup:'):
  amount=data.split(':')[1]
  if amount=='custom':set_state(tid,'topup_amount');answer_cb(cid);return send(chat,'Nhập số tiền muốn nạp (VND):')
  set_state(tid,'topup_proof',{'amount':int(amount)});answer_cb(cid);return send(chat,f'Nạp {int(amount):,}đ · gửi ảnh biên lai để admin xác nhận.')
 if data=='coupon':set_state(tid,'coupon');answer_cb(cid);return send(chat,'Nhập mã coupon:')
 if data=='mykeys':
  c=db();rows=c.execute("SELECT id,key_text,tier,expires_at,archived_at FROM keys_pool WHERE sold_to=? AND status='sold' AND archived_at IS NULL ORDER BY sold_at DESC LIMIT 50",(tid,)).fetchall();c.close();answer_cb(cid)
  if not rows:return send(chat,'Bạn chưa có key hoạt động/lưu trữ gần đây.')
  lines=[]
  for r in rows:lines.append(f'#{r["id"]} · {safe(TIERS.get(r["tier"],(r["tier"],))[0])} · <code>{safe(r["key_text"][:3]+"••••••")}</code> '+('Vĩnh viễn' if not r['expires_at'] else safe(r['expires_at'])))
  return send(chat,'<b>Key của bạn</b>\n'+'\n'.join(lines),kb([[button('Hiện key #'+str(r['id']),f'reveal:{r["id"]}')] for r in rows]))
 if data.startswith('reveal:'):
  try:kid=int(data.split(':')[1])
  except:return answer_cb(cid,'Key không hợp lệ.')
  c=db();r=c.execute('SELECT key_text FROM keys_pool WHERE id=? AND sold_to=? AND archived_at IS NULL',(kid,tid)).fetchone();c.close();answer_cb(cid);return send(chat,'Key: <code>'+safe(r[0])+'</code>' if r else 'Không tìm thấy key.')
 if data=='walletbuy':answer_cb(cid);return send(chat,'Chọn gói mua:',kb([[button(TIERS[t][0]+f' · {TIERS[t][2]:,}đ',f'walletbuy:{t}')] for t in ('1d','3d','7d','life')]))
 if data.startswith('buy:') or data.startswith('walletbuy:'):
  tier=data.split(':',1)[1]
  if tier not in ('1d','3d','7d','life'):return answer_cb(cid,'Gói không hợp lệ.')
  if not require_membership(tid,chat):return answer_cb(cid,'Cần tham gia kênh/nhóm.')
  from_wallet=data.startswith('walletbuy:')
  def op(c):
   if not c.execute('SELECT 1 FROM keys_pool WHERE tier=? AND status="free" LIMIT 1',(tier,)).fetchone():return ('stock',None)
   user=user_row(c,tid,fr);amount=TIERS[tier][2];disc=c.execute('SELECT * FROM pending_discount WHERE user_id=?',(user['id'],)).fetchone()
   if disc:amount=max(0,amount*(100-disc['discount'])//100)
   if from_wallet:
    if user['wallet']<amount:return ('funds',None)
    c.execute('UPDATE users SET wallet=wallet-? WHERE id=?',(amount,user['id']));c.execute('INSERT INTO wallet_ledger(user_id,delta,reason,reference,created_at) VALUES(?,?,?,?,?)',(user['id'],-amount,'purchase',f'purchase:{secrets.token_hex(12)}',now()))
    if disc:c.execute('DELETE FROM pending_discount WHERE user_id=?',(user['id'],))
    key=claim_key(c,tid,tier)
    return ('wallet',(key[0],amount))
   ref='PNV'+secrets.token_hex(4).upper();expiry=(datetime.now(timezone.utc)+timedelta(minutes=15)).isoformat(timespec='seconds')
   oid=c.execute("INSERT INTO orders(user_id,tier_id,tier,amount,status,reference,created_at,expires_at) VALUES(?,?,?,?, 'pending',?,?,?)",(user['id'],tier,tier,amount,ref,now(),expiry)).lastrowid
   c.execute('UPDATE orders SET reference=? WHERE id=?',(f'PNV{oid:08d}',oid))
   if disc:c.execute('DELETE FROM pending_discount WHERE user_id=?',(user['id'],))
   return ('order',(oid,amount,tier))
  status,val=run_tx(op);answer_cb(cid)
  if status=='stock':return send(chat,'Kho gói này hiện hết hàng; chưa tạo đơn.')
  if status=='funds':return send(chat,'Số dư ví chưa đủ.',kb([[button('Nạp ví','wallet')]]))
  if status=='wallet':return send(chat,'Mua thành công · key: <code>'+safe(val[0])+'</code>')
  set_state(tid,'order_proof',{'order_id':val[0]});return send_order_prompt(chat,val[0],val[2],val[1])
 if data=='referral':answer_cb(cid);return send(chat,f'Link giới thiệu: <code>https://t.me/{safe(BOT_USERNAME)}?start={tid}</code>\n+3 điểm sau lần điểm danh đầu tiên của người được mời (người giới thiệu không được bị cấm).')
 if data=='leaderboard':
  answer_cb(cid);month=day7()[:7];c=db();rows=c.execute("SELECT telegram_id,username,points FROM users WHERE points>0 AND banned=0 ORDER BY points DESC,id ASC LIMIT 10").fetchall();c.close();return send(chat,'<b>Top điểm tháng '+safe(month)+'</b>\n'+'\n'.join(f'{i+1}. {safe(r["username"] or r["telegram_id"])} — {r["points"]} điểm' for i,r in enumerate(rows)) or 'Chưa có dữ liệu tháng này.')
 if data=='support':answer_cb(cid);return send(chat,'Hỗ trợ: '+(f'<a href="{safe(GROUP_LINK)}">mở nhóm hỗ trợ</a>' if GROUP_LINK else 'GROUP_LINK chưa được cấu hình.'))
 if data=='redeem_points':answer_cb(cid);return send(chat,'Dùng nút đổi điểm bên dưới.',kb([[button('6 điểm → 1 giờ','redeem:1h'),button('15 điểm → 1 ngày','redeem:1d')],[button('40 điểm → 3 ngày','redeem:3d'),button('100 điểm → 7 ngày','redeem:7d')]]))
 if data.startswith('redeem:'):
  tier=data.split(':',1)[1];cost={'1h':6,'1d':15,'3d':40,'7d':100}.get(tier)
  if not cost:return answer_cb(cid,'Gói sai.')
  if not require_membership(tid,chat):return answer_cb(cid,'Cần tham gia kênh/nhóm.')
  def op(c):
   user=user_row(c,tid,fr)
   if user['points']<cost:return ('points',None)
   k=claim_key(c,tid,tier)
   if not k:return ('stock',None)
   c.execute('UPDATE users SET points=points-? WHERE id=?',(cost,user['id']));return ('ok',k[0])
  status,key=run_tx(op);answer_cb(cid);return send(chat,'Không đủ điểm.' if status=='points' else 'Kho hết gói này.' if status=='stock' else 'Đổi thành công: <code>'+safe(key)+'</code>')
 answer_cb(cid)

# Admin command compatibility.
def admin_command(cmd,text,chat,tid):
 c=db()
 try:
  if cmd=='/orders':return send(chat,'\n'.join(f'#{r["id"]} {r["tier"]} {r["amount"]}đ — {r["status"]}' for r in c.execute('SELECT * FROM orders ORDER BY id DESC LIMIT 20')) or 'Chưa có đơn.')
  if cmd=='/revenue':return send(chat,f'Doanh thu: {c.execute("SELECT COALESCE(SUM(amount),0) FROM orders WHERE status=\'approved\'").fetchone()[0]:,}đ')
 finally:c.close()

LOGIN_TOKENS=set();LOGIN_LOCK=threading.Lock()
def login_signature(uid,exp):return hmac.new(APP_SECRET.encode(),f'{uid}:{exp}'.encode(),hashlib.sha256).hexdigest()
@app.get('/admin/login')
def admin_login():
 uid=request.args.get('uid','');exp=request.args.get('exp','');sig=request.args.get('sig','')
 try:valid=uid.isdigit() and exp.isdigit() and int(exp)>=int(time.time()) and int(exp)<=int(time.time())+300 and hmac.compare_digest(sig,login_signature(uid,exp))
 except Exception:valid=False
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
  uid=session.get('uid')
  if not uid or int(time.time())-session.get('issued',0)>43200 or not has_role(uid):session.clear();return jsonify(error='unauthorized'),401
  return f(*a,**kw)
 return wrap
@app.get('/admin/static/<path:name>')
def admin_static(name):
 root=Path(__file__).resolve().parent;target=(root/name).resolve()
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
ADMIN_HTML='''<!doctype html><html lang="vi"><meta charset="utf-8"><title>Pawnova Admin</title><main><h1>Pawnova Admin</h1><p>Admin API hoạt động.</p></main></html>'''
@app.get('/admin')
def admin_page():
 if not session.get('uid'):return redirect('/admin/login-error')
 template=Path(__file__).resolve().parent/'admin.html'
 if template.is_file():
  from flask import send_file
  return send_file(template)
 return ADMIN_HTML
def api_data():return request.get_json(silent=True) or {}
@app.get('/api/keys/summary')
@logged
def api_keys_summary():
 c=db()
 try:
  result={t:{'total':0,'free':0,'sold':0} for t in TIERS}
  for r in c.execute('SELECT tier,status,COUNT(*) n FROM keys_pool GROUP BY tier,status'):
   if r['tier'] in result:result[r['tier']][r['status']]=r['n'];result[r['tier']]['total']+=r['n']
  return jsonify(result)
 finally:c.close()
@app.get('/api/<name>')
@logged
def api_get(name):
 c=db()
 try:
  if name=='me':
   uid=session['uid'];return jsonify(uid=uid,name=str(uid),role=role_for(uid))
  if name=='keys' and request.args.get('tier'):
   tier=request.args.get('tier');status=request.args.get('status','all')
   if tier not in TIERS or status not in ('all','free','sold'):return jsonify(error='invalid filter'),400
   sql='SELECT * FROM keys_pool WHERE tier=?'+('' if status=='all' else ' AND status=?')+' ORDER BY id DESC LIMIT 5000';params=(tier,) if status=='all' else (tier,status)
   return jsonify([dict(r) for r in c.execute(sql,params).fetchall()])
  if name=='stats':return jsonify(users=c.execute('SELECT COUNT(*) FROM users').fetchone()[0],free_keys=c.execute("SELECT COUNT(*) FROM keys_pool WHERE status='free'").fetchone()[0],pending_orders=c.execute("SELECT COUNT(*) FROM orders WHERE status IN ('pending','proof')").fetchone()[0],pending_topups=c.execute("SELECT COUNT(*) FROM topups WHERE status='pending'").fetchone()[0],role=role_for(session['uid']))
  tables={'users':'SELECT telegram_id,username,first_name,points,wallet,streak,banned,created_at FROM users ORDER BY id DESC LIMIT 200','keys':'SELECT * FROM keys_pool ORDER BY id DESC LIMIT 300','orders':'SELECT * FROM orders ORDER BY id DESC LIMIT 200','topups':'SELECT * FROM topups ORDER BY id DESC LIMIT 200','coupons':'SELECT * FROM coupons ORDER BY id DESC LIMIT 200','admins':'SELECT telegram_id,role,created_at FROM admins ORDER BY telegram_id','revenue':"SELECT status,COUNT(*) count,COALESCE(SUM(amount),0) amount FROM orders GROUP BY status",'settings':'SELECT key,value FROM settings ORDER BY key'}
  if name=='backup':return jsonify(error='use GET backup endpoint'),405
  if name not in tables:return jsonify(error='not found'),404
  return jsonify([dict(r) for r in c.execute(tables[name]).fetchall()])
 finally:c.close()
@app.get('/api/keys/export')
@logged
def export_keys_txt():
 tier=request.args.get('tier','')
 status=request.args.get('status','all')
 if tier not in TIERS:return jsonify(error='invalid tier'),400
 if status not in ('all','free','sold'):return jsonify(error='invalid status'),400
 c=db()
 try:
  rows=c.execute('SELECT key_text FROM keys_pool WHERE tier=?'+('' if status=='all' else ' AND status=?')+' ORDER BY id',(tier,) if status=='all' else (tier,status)).fetchall()
 finally:c.close()
 payload='\n'.join(r['key_text'] for r in rows)+ ('\n' if rows else '')
 response=make_response(payload);response.mimetype='text/plain; charset=utf-8';response.headers['Content-Disposition']=f'attachment; filename="keys-{tier}-{status}.txt"';return response
@app.post('/api/<name>')
@logged
def api_post(name):
 role=role_for(session['uid']);data=api_data()
 if role=='viewer':return jsonify(error='forbidden'),403
 try:
  if name=='keys':
   vals=data.get('keys',data.get('key',''));vals=vals if isinstance(vals,list) else str(vals).splitlines();tier=data.get('tier','1d')
   if tier not in TIERS:return jsonify(error='invalid tier'),400
   def op(c):
    n=duplicate=invalid=0
    for v in vals:
     value=str(v).strip()
     if not value or len(value)>2000:invalid+=1;continue
     try:
      cur=c.execute('INSERT OR IGNORE INTO keys_pool(key_text,tier,created_at) VALUES(?,?,?)',(value,tier,now()))
      if cur.rowcount:n+=1
      else:duplicate+=1
     except sqlite3.IntegrityError:duplicate+=1
    return n,duplicate,invalid
   inserted,duplicate,invalid=run_tx(op)
   return jsonify(inserted=inserted,duplicate=duplicate,invalid=invalid)
  if name=='orders':
   oid=int(data['id']);action=data.get('action')
   def review_order(c):
    o=c.execute("SELECT * FROM orders WHERE id=? AND status IN ('pending','proof')",(oid,)).fetchone()
    if not o:return False
    if action=='reject':c.execute("UPDATE orders SET status='rejected' WHERE id=?",(oid,));return True
    if action!='approve' or (o['expires_at'] and o['expires_at']<now()):return False
    uid=c.execute('SELECT telegram_id FROM users WHERE id=?',(o['user_id'],)).fetchone()[0];key=claim_key(c,uid,o['tier'],oid)
    if not key:return False
    c.execute("UPDATE orders SET status='approved',paid_at=?,key_id=(SELECT id FROM keys_pool WHERE order_id=?) WHERE id=?",(now(),oid,oid));return key[0]
   result=run_tx(review_order)
   if result and result is not True:
    c=db();u=c.execute('SELECT telegram_id FROM users WHERE id=(SELECT user_id FROM orders WHERE id=?)',(oid,)).fetchone();c.close();send(u[0],f'Đơn #{oid} đã duyệt. Key: <code>{safe(result)}</code>')
   return jsonify(updated=bool(result))
  if name=='topups':
   oid=int(data['id']);action=data.get('action')
   def review_topup(c):
    r=c.execute("SELECT * FROM topups WHERE id=? AND status='pending'",(oid,)).fetchone()
    if not r:return False
    if r['expires_at'] and r['expires_at']<now():c.execute("UPDATE topups SET status='expired' WHERE id=?",(oid,));return False
    if action=='reject':c.execute("UPDATE topups SET status='rejected',reviewed_at=?,reviewer=? WHERE id=?",(now(),session['uid'],oid));return True
    if action!='approve':return False
    credit=r['amount']+(r['points'] or 0);c.execute("UPDATE topups SET status='approved',reviewed_at=?,reviewer=? WHERE id=?",(now(),session['uid'],oid));c.execute('UPDATE users SET wallet=wallet+? WHERE id=?',(credit,r['user_id']));c.execute('INSERT OR IGNORE INTO wallet_ledger(user_id,delta,reason,reference,created_at) VALUES(?,?,?,?,?)',(r['user_id'],credit,'topup',f'topup:{oid}',now()));return credit
   result=run_tx(review_topup)
   if result and result is not True:
    c=db();u=c.execute('SELECT telegram_id FROM users WHERE id=(SELECT user_id FROM topups WHERE id=?)',(oid,)).fetchone();c.close();send(u[0],f'Đã cộng {result:,}đ vào ví.')
   return jsonify(updated=bool(result))
  if name=='coupons':
   if data.get('action')=='delete':run_tx(lambda c:c.execute('UPDATE coupons SET active=0 WHERE id=?',(int(data['id']),)));return jsonify(ok=True)
   run_tx(lambda c:c.execute('INSERT INTO coupons(code,kind,value,max_uses,expires_at) VALUES(?,?,?,?,?)',(str(data['code']),data.get('kind','points'),int(data['value']),int(data.get('max_uses',0)),data.get('expires_at'))));return jsonify(ok=True)
  if name=='admins':
   if role!='super':return jsonify(error='super admin required'),403
   aid=int(data['telegram_id']);act=data.get('action','set')
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
   text=str(data.get('text','')).strip();c=db();ids=[r[0] for r in c.execute('SELECT telegram_id FROM users WHERE banned=0')];c.close();threading.Thread(target=lambda:[send(i,safe(text[:3500])) for i in ids],daemon=True).start();return jsonify(count=len(ids))
  if name=='settings':
   key=str(data['key']);value=str(data.get('value',''))
   if not re.fullmatch(r'(channel:[\w@-]+|qr_mode|qr_file_id|qr_template|payment_info|group_link|welcome_text)',key):return jsonify(error='invalid setting'),400
   run_tx(lambda c:c.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,value[:2000])));return jsonify(ok=True)
  return jsonify(error='not found'),404
 except (ValueError,KeyError,sqlite3.IntegrityError):return jsonify(error='invalid request'),400
 except Exception:log.exception('Admin API error');return jsonify(error='internal error'),500
@app.get('/api/backup')
@logged
def api_backup():
 if role_for(session['uid'])=='viewer':return jsonify(error='forbidden'),403
 from flask import send_file
 import tempfile
 fd,path=tempfile.mkstemp(prefix='pawnova-',suffix='.db');os.close(fd);src=db();dst=sqlite3.connect(path)
 try:src.backup(dst)
 finally:src.close();dst.close()
 resp=send_file(path,as_attachment=True,download_name='pawnova-backup.db')
 @resp.call_on_close
 def cleanup():
  try:os.unlink(path)
  except OSError:pass
 return resp
@app.post('/webhook')
def webhook():
 if WEBHOOK_SECRET and not hmac.compare_digest(request.headers.get('X-Telegram-Bot-Api-Secret-Token',''),WEBHOOK_SECRET):return jsonify(ok=False),403
 if not TOKEN:return jsonify(ok=False,reason='bot disabled'),200
 upd=request.get_json(silent=True)
 if not isinstance(upd,dict):return jsonify(ok=False),400
 threading.Thread(target=process_update,args=(upd,),daemon=True).start();return jsonify(ok=True),200
def process_update(update):
 try:
  if 'message' in update:handle_text(update['message'])
  elif 'callback_query' in update:handle_callback(update['callback_query'])
 except Exception:log.exception('Update processing failed')
@app.get('/health')
def health():return jsonify(status='ok',bot_api_enabled=bool(TOKEN))
@app.get('/')
def index():return redirect('/admin')
def background_maintenance():
 while True:
  try:
   cutoff=(datetime.now(timezone.utc)-timedelta(minutes=15)).isoformat();arch=(datetime.now(timezone.utc)-timedelta(hours=3)).isoformat()
   run_tx(lambda c:(c.execute('DELETE FROM pending_state WHERE updated_at<?',(cutoff,)),c.execute("UPDATE keys_pool SET archived_at=? WHERE status='sold' AND expires_at IS NOT NULL AND expires_at<=? AND archived_at IS NULL",(now(),arch)),c.execute("UPDATE orders SET status='expired' WHERE status IN ('pending','proof') AND expires_at IS NOT NULL AND expires_at<=?",(now(),)),c.execute("UPDATE topups SET status='expired' WHERE status='pending' AND expires_at IS NOT NULL AND expires_at<=?",(now(),))))
  except Exception:log.exception('Maintenance failed')
  time.sleep(300)
def setup_webhook():
 if not TOKEN or not PUBLIC_URL:return
 payload={'url':PUBLIC_URL+'/webhook','allowed_updates':['message','callback_query']}
 if WEBHOOK_SECRET:payload['secret_token']=WEBHOOK_SECRET
 for delay in (3,6,12,24,48,60,60,60):
  if tg('setWebhook',payload,timeout=20).get('ok'):log.info('Webhook configured');return
  time.sleep(delay)
if __name__=='__main__':
 threading.Thread(target=background_maintenance,daemon=True).start();threading.Thread(target=setup_webhook,daemon=True).start();app.run(host='0.0.0.0',port=PORT,threaded=True)
