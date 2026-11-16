import hashlib, os, secrets, json
from contextlib import contextmanager, closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from decimal import Decimal
from urllib.parse import urlparse
import psycopg2
import httpx
from psycopg2.extras import RealDictCursor
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from .database_config import connection_settings, DatabaseConfig
from .database_pool import connection, close_pool
from . import payments
from cryptography.exceptions import InvalidTag

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
DATABASE_URL = os.getenv("TAAS_DATABASE_URL", "postgresql://huitian:SET_LOCALLY_BEFORE_USE@192.168.17.127:5432/qionghai")
DATABASE_SETTINGS = connection_settings()
app = FastAPI(title="Qionghai TaaS", version="v0.261115")
PAYMENT_PERMISSIONS = {
    'payments.configure': '配置微信与支付宝',
    'payments.create': '创建支付订单',
    'payments.wechat': '使用微信支付',
    'payments.alipay': '使用支付宝支付',
    'orders.view': '查看个人订单',
    'orders.manage': '查看全部订单与充值统计',
    'orders.confirm': '人工确认订单入账',
    'recharges.manage': '执行人工充值',
}
UPLOADS = ROOT / 'uploads'
UPLOADS.mkdir(parents=True, exist_ok=True)
app.mount('/taas-assets', StaticFiles(directory=UPLOADS), name='taas-assets')

@contextmanager
def db():
    with connection(DATABASE_SETTINGS) as conn:
        yield conn

def query(sql, params=(), one=False):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if cur.description is None:
                return None
            return cur.fetchone() if one else cur.fetchall()

def hash_password(value): return hashlib.sha256(value.encode()).hexdigest()

def init_db(settings=None):
    schema = (ROOT / "backend" / "schema.sql").read_text(encoding="utf-8")
    password = os.getenv("TAAS_ADMIN_PASSWORD", "SET_LOCALLY_BEFORE_USE")
    target = closing(psycopg2.connect(**settings, connect_timeout=8, options='-c search_path=taas')) if settings is not None else db()
    with target as conn, conn:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS taas")
            cur.execute('SELECT pg_advisory_xact_lock(810001)')
            cur.execute(schema)
            cur.execute("ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS quota_used BIGINT NOT NULL DEFAULT 0")
            cur.execute("ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS unlimited_quota BOOLEAN NOT NULL DEFAULT false")
            cur.execute("ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS model_scope TEXT NOT NULL DEFAULT ''")
            cur.execute("ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ")
            cur.execute("ALTER TABLE payment_orders ADD COLUMN IF NOT EXISTS payment_trade_no TEXT")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS payment_orders_trade_unique ON payment_orders(channel,payment_trade_no) WHERE payment_trade_no IS NOT NULL")
            cur.execute("""UPDATE recharge_transactions t SET
                channel=o.channel,payment_trade_no=coalesce(o.payment_trade_no,''),
                amount=CASE WHEN t.kind='package_payment' AND t.amount=0 THEN o.amount ELSE t.amount END
                FROM payment_orders o WHERE t.reference=o.order_no AND t.user_id=o.user_id
                AND t.kind IN ('payment','package_payment','balance_payment') AND o.status='paid'
                AND (t.channel='' OR (t.kind='package_payment' AND t.amount=0))""")
            cur.execute("UPDATE recharge_transactions SET channel=CASE kind WHEN 'admin' THEN 'manual' WHEN 'redemption' THEN 'redemption' ELSE channel END WHERE channel='' AND kind IN ('admin','redemption')")
            cur.execute("INSERT INTO users(username,password_hash,display_name,role,status) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(username) DO NOTHING", ("admin",hash_password(password),"TaaS 管理员","admin","active"))
            cur.execute("INSERT INTO permissions(code,name) VALUES(%s,%s),(%s,%s),(%s,%s),(%s,%s) ON CONFLICT(code) DO NOTHING", ("portal.view","访问门户","users.manage","管理用户","keys.manage","管理 API Key","packages.manage","管理套餐"))
            for code in ("portal.view","users.manage","keys.manage","packages.manage"): cur.execute("INSERT INTO role_permissions(role,permission_code) VALUES(%s,%s) ON CONFLICT DO NOTHING", ("admin",code))
            cur.execute("INSERT INTO role_permissions(role,permission_code) VALUES(%s,%s) ON CONFLICT DO NOTHING", ("customer","portal.view"))
            for code, name in PAYMENT_PERMISSIONS.items():
                cur.execute('INSERT INTO permissions(code,name) VALUES(%s,%s) ON CONFLICT DO NOTHING RETURNING code', (code,name))
                if cur.fetchone():
                    cur.execute('INSERT INTO role_permissions(role,permission_code) VALUES(%s,%s) ON CONFLICT DO NOTHING', ('admin',code))
                    if code in ('payments.create','payments.wechat','payments.alipay','orders.view'):
                        cur.execute('INSERT INTO role_permissions(role,permission_code) VALUES(%s,%s) ON CONFLICT DO NOTHING', ('customer',code))

@app.on_event("startup")
def startup(): init_db()

@app.on_event("shutdown")
def shutdown(): close_pool()

class Credentials(BaseModel): username: str; password: str
class Register(Credentials): display_name: str = ""; email: str = ""
class KeyPayload(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    quota: int = Field(default=0, ge=0)
    model_scope: str = ''
    expires_at: datetime | None = None
    order_no: str | None = None
class UserPayload(BaseModel): username: str = Field(min_length=1); password: str = Field(min_length=8); display_name: str = ""; email: str = ""
class PackagePayload(BaseModel):
    name: str = Field(min_length=1)
    price: float = Field(ge=0)
    quota: int = Field(gt=0)
    valid_days: int = Field(default=30, ge=1)
    enabled: bool = True
    model_routes: dict[str, str | list[str]] = Field(default_factory=dict)
class ChannelPayload(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    base_url: str
    api_key: str = ''
    model_scope: str = ''
    enabled: bool = True
    weight: int = Field(default=1, ge=1, le=1000000)
class OrderPayload(BaseModel): package_id: int
class RedeemPayload(BaseModel): code: str
class RechargePayload(BaseModel): user_id: int; amount: float = Field(gt=0); reference: str = ""
class CodePayload(BaseModel): count: int = Field(default=1, ge=1, le=100); amount: float = Field(default=0, ge=0); quota: int = Field(default=0, ge=0)
class RolePayload(BaseModel): role: str = Field(min_length=1); permission_codes: list[str] = []
class SettingPayload(BaseModel): value: str

def user_from_request(request: Request):
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not token: raise HTTPException(401,"请先登录")
    row=query("SELECT u.id,u.username,u.display_name,u.email,u.role,u.status,u.balance,u.created_at FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=%s AND s.expires_at>CURRENT_TIMESTAMP AND u.status='active'",(token,),True)
    if not row: raise HTTPException(401,"会话已失效")
    return dict(row)

def require(permission):
    def check(user=Depends(user_from_request)):
        check_permission(user, permission)
        return user
    return check

@app.post('/api/admin/database/initialize')
def initialize_database_endpoint(config:DatabaseConfig,user=Depends(require('users.manage'))):
    from .database_config import initialize_database
    if not config.password:
        current=DATABASE_SETTINGS
        if (config.host,config.port,config.dbname,config.user)!=(current['host'],int(current.get('port',5432)),current['dbname'],current['user']):
            raise HTTPException(422,'更换连接时请填写目标数据库密码')
        config=config.model_copy(update={'password':current.get('password','')})
    try:
        return initialize_database(config)
    except ValueError as error:
        raise HTTPException(422,str(error)) from None

def effective_permissions(user):
    rows = query('''SELECT p.code FROM permissions p JOIN users u ON u.id=%s
        LEFT JOIN role_permissions r ON r.role=u.role AND r.permission_code=p.code
        LEFT JOIN user_permissions override ON override.user_id=u.id AND override.permission_code=p.code
        WHERE u.status='active' AND COALESCE(override.allowed,r.permission_code IS NOT NULL)''', (user['id'],))
    return {row['code'] for row in rows}

def check_permission(user, permission):
    if permission not in effective_permissions(user):
        raise HTTPException(403, '没有权限：' + PAYMENT_PERMISSIONS.get(permission, permission))

class UserPermissionPayload(BaseModel):
    overrides: dict[str, bool] = {}

@app.get('/api/admin/users/{user_id}/permissions')
def user_permission_details(user_id: int, user=Depends(require('users.manage'))):
    check_permission(user, 'users.manage')
    target = query('SELECT id,role,status FROM users WHERE id=%s', (user_id,), True)
    if not target: raise HTTPException(404, '用户不存在')
    overrides = query('SELECT permission_code,allowed FROM user_permissions WHERE user_id=%s', (user_id,))
    return {'effective': sorted(effective_permissions(target)), 'overrides': {row['permission_code']:row['allowed'] for row in overrides}}

@app.put('/api/admin/users/{user_id}/permissions')
def update_user_permissions(user_id: int, payload: UserPermissionPayload, user=Depends(require('users.manage'))):
    check_permission(user, 'users.manage')
    if not set(payload.overrides).issubset(PAYMENT_PERMISSIONS):
        raise HTTPException(400, '仅支持设置支付与订单相关的用户授权')
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id FROM users WHERE id=%s FOR UPDATE', (user_id,))
            if not cur.fetchone(): raise HTTPException(404, '用户不存在')
            cur.execute('DELETE FROM user_permissions WHERE user_id=%s', (user_id,))
            for code, allowed in payload.overrides.items():
                cur.execute('INSERT INTO user_permissions(user_id,permission_code,allowed) VALUES(%s,%s,%s)', (user_id,code,allowed))
    return {'message': '用户授权已保存'}

@app.get("/")
def home(): return RedirectResponse('/taas', status_code=303, headers={'Cache-Control': 'no-store'})
@app.get("/health")
def health(): query("SELECT 1",one=True); return {"status":"ok","database":"postgresql"}

@app.post("/api/auth/register")
def register(payload:Register):
    if len(payload.password)<8: raise HTTPException(400,"密码至少 8 位")
    try: row=query("INSERT INTO users(username,password_hash,display_name,email) VALUES(%s,%s,%s,%s) RETURNING id",(payload.username.strip(),hash_password(payload.password),payload.display_name.strip() or payload.username,payload.email.strip()),True); return {"id":row["id"],"message":"注册成功"}
    except psycopg2.errors.UniqueViolation: raise HTTPException(409,"用户名已存在")

@app.post("/api/auth/login")
def login(payload:Credentials):
    row=query("SELECT * FROM users WHERE username=%s AND password_hash=%s AND status='active'",(payload.username.strip(),hash_password(payload.password)),True)
    if not row: raise HTTPException(401,"账号或密码错误")
    token=secrets.token_urlsafe(40); query("INSERT INTO sessions(token,user_id,expires_at) VALUES(%s,%s,%s)",(token,row["id"],datetime.now(timezone.utc)+timedelta(hours=12)))
    return {"token":token,"user":{"id":row["id"],"username":row["username"],"display_name":row["display_name"],"role":row["role"]}}

@app.get("/api/auth/me")
def me(user=Depends(user_from_request)): return user
@app.get("/api/portal")
def portal(user=Depends(user_from_request)):
    can_view = 'orders.view' in effective_permissions(user)
    stats = portal_statistics(user)
    return {"user":user,"stats":stats,"api_keys":query("SELECT id,name,quota,quota_used,enabled,created_at FROM api_keys WHERE user_id=%s ORDER BY id DESC",(user["id"],)),"packages":query("SELECT * FROM packages WHERE enabled=TRUE"),"orders":query("SELECT order_no,amount,quota,status,channel,created_at,paid_at FROM payment_orders WHERE user_id=%s ORDER BY id DESC LIMIT 50",(user["id"],)) if can_view else [],"transactions":query("SELECT amount,balance_before,balance_after,kind,channel,payment_trade_no,reference,created_at FROM recharge_transactions WHERE user_id=%s ORDER BY id DESC LIMIT 50",(user["id"],)) if can_view else []}

def portal_statistics(user):
    return query('''SELECT
        (SELECT count(*) FROM api_keys WHERE user_id=%s) AS key_count,
        (SELECT count(*) FROM payment_orders WHERE user_id=%s) AS order_count,
        (SELECT count(*) FROM payment_orders WHERE user_id=%s AND status='pending') AS pending_order_count,
        (SELECT coalesce(sum(amount),0) FROM payment_orders WHERE user_id=%s AND status='paid') AS paid_amount,
        (SELECT count(*) FROM usage_logs WHERE user_id=%s) AS request_count,
        (SELECT coalesce(sum(tokens_in),0) FROM usage_logs WHERE user_id=%s) AS tokens_in,
        (SELECT coalesce(sum(tokens_out),0) FROM usage_logs WHERE user_id=%s) AS tokens_out,
        (SELECT coalesce(sum(cost),0) FROM usage_logs WHERE user_id=%s) AS usage_cost''', tuple([user['id']] * 8), one=True)
@app.post("/api/keys")
def create_key(payload:KeyPayload,user=Depends(require("keys.manage")),request:Request=None):
    return issue_key(payload, user, request)

def api_call_addresses(request=None, user_id=None):
    configured = query("SELECT setting_value FROM platform_settings WHERE setting_key='api_base_url'", one=True)
    base_url = (configured['setting_value'].strip() if configured else '') or (str(request.base_url).rstrip('/') + '/v1' if request else 'http://127.0.0.1:8100/v1')
    base_url = base_url.rstrip('/')
    if user_id is not None:
        base_url = base_url.removesuffix('/v1') + f'/users/{int(user_id)}/v1'
    return {'base_url': base_url, 'chat_completions_url': base_url + '/chat/completions', 'models_url': base_url + '/models'}

def issue_key(payload, user, request=None):
    addresses = api_call_addresses(request, user['id'])
    payload.name = payload.name.strip()
    payload.model_scope = ','.join(dict.fromkeys(item.strip() for item in payload.model_scope.split(',') if item.strip()))
    if payload.expires_at and payload.expires_at.tzinfo is None:
        payload.expires_at = payload.expires_at.replace(tzinfo=timezone(timedelta(hours=8)))
    if payload.expires_at and payload.expires_at <= datetime.now(timezone.utc):
        raise HTTPException(400, 'API Key 有效期必须晚于当前时间')
    token = 'sk-taas-' + secrets.token_urlsafe(28)
    order = usable_order(user['id'], payload.order_no) if payload.order_no else None
    row = query('INSERT INTO api_keys(user_id,name,token,quota,model_scope,expires_at,consumption_order_id) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id', (user['id'],payload.name,token,payload.quota,payload.model_scope,payload.expires_at,order['id'] if order else None), True)
    owner = query('SELECT username FROM users WHERE id=%s', (user['id'],), True)
    return {'id':row['id'],'user_id':user['id'],'username':owner['username'],'name':payload.name,'token':token,'quota':payload.quota,'model_scope':payload.model_scope,'expires_at':payload.expires_at,**addresses}

@app.post("/api/portal/keys")
def create_portal_key(payload:KeyPayload, user=Depends(user_from_request),request:Request=None):
    return issue_key(payload, user, request)

@app.delete("/api/portal/keys/{key_id}")
def delete_portal_key(key_id:int, user=Depends(user_from_request)):
    return remove_key(key_id, user, own_only=True)


class KeyStatusPayload(BaseModel):
    enabled: bool


def authorize_key_owner(row, user, own_only=False):
    if not row: raise HTTPException(404, 'API Key 不存在')
    if row['user_id'] != user['id']:
        if own_only: raise HTTPException(404, 'API Key 不存在')
        check_permission(user, 'keys.manage')


@app.patch('/api/keys/{key_id}/status')
def set_key_status(key_id: int, payload: KeyStatusPayload, user=Depends(user_from_request)):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT user_id FROM api_keys WHERE id=%s FOR UPDATE', (key_id,))
            authorize_key_owner(cur.fetchone(), user)
            cur.execute('UPDATE api_keys SET enabled=%s WHERE id=%s RETURNING id,enabled', (payload.enabled,key_id))
            return dict(cur.fetchone())


@app.delete('/api/keys/{key_id}')
def delete_key(key_id: int, user=Depends(user_from_request)):
    return remove_key(key_id, user)


def remove_key(key_id, user, own_only=False):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT user_id FROM api_keys WHERE id=%s FOR UPDATE', (key_id,))
            authorize_key_owner(cur.fetchone(), user, own_only)
            cur.execute('SELECT 1 FROM usage_reservations WHERE api_key_id=%s AND expires_at>CURRENT_TIMESTAMP LIMIT 1', (key_id,))
            if cur.fetchone():
                raise HTTPException(409, '此 API Key 有正在处理的调用，请先禁用，等待调用结束后再删除')
            cur.execute('DELETE FROM api_keys WHERE id=%s', (key_id,))
    return {'id':key_id,'message':'API Key 已删除'}

@app.get('/api/keys/{key_id}')
def key_details(key_id: int, user=Depends(user_from_request), request: Request=None):
    row = query('SELECT k.id,k.user_id,u.username,k.name,k.token,k.quota,k.quota_used,k.enabled,k.model_scope,k.expires_at,k.created_at,o.order_no FROM api_keys k JOIN users u ON u.id=k.user_id LEFT JOIN payment_orders o ON o.id=k.consumption_order_id WHERE k.id=%s', (key_id,), True)
    if not row: raise HTTPException(404, 'API Key 不存在')
    if row['user_id'] != user['id']:
        check_permission(user, 'keys.manage')
    return {**dict(row), **api_call_addresses(request, row['user_id'])}

@app.put('/api/keys/{key_id}')
def update_key(key_id: int, payload: KeyPayload, user=Depends(user_from_request)):
    row = query('SELECT user_id FROM api_keys WHERE id=%s', (key_id,), True)
    if not row: raise HTTPException(404, 'API Key 不存在')
    if row['user_id'] != user['id']:
        check_permission(user, 'keys.manage')
    payload.name = payload.name.strip()
    payload.model_scope = ','.join(dict.fromkeys(item.strip() for item in payload.model_scope.split(',') if item.strip()))
    if payload.expires_at and payload.expires_at.tzinfo is None:
        payload.expires_at = payload.expires_at.replace(tzinfo=timezone(timedelta(hours=8)))
    if payload.expires_at and payload.expires_at <= datetime.now(timezone.utc): raise HTTPException(400, 'API Key 有效期必须晚于当前时间')
    order = usable_order(row['user_id'], payload.order_no) if payload.order_no else None
    return query('UPDATE api_keys SET name=%s,quota=%s,model_scope=%s,expires_at=%s,consumption_order_id=%s WHERE id=%s RETURNING id,name,quota,quota_used,model_scope,expires_at,enabled', (payload.name,payload.quota,payload.model_scope,payload.expires_at,order['id'] if order else None,key_id), True)
@app.get("/api/admin/users")
def users(user=Depends(require("users.manage"))): return query("SELECT id,username,display_name,email,role,status,created_at FROM users WHERE status<>'deleted' ORDER BY id")
@app.post("/api/admin/users")
def create_user(payload:UserPayload,user=Depends(require("users.manage"))):
    try:
        row=query("INSERT INTO users(username,password_hash,display_name,email,role) VALUES(%s,%s,%s,%s,%s) RETURNING id,username,display_name,email,role,status",(payload.username.strip(),hash_password(payload.password),payload.display_name.strip() or payload.username,payload.email.strip(),"customer"),True); return row
    except psycopg2.errors.UniqueViolation: raise HTTPException(409,"用户名已存在")

class UserStatusPayload(BaseModel):
    status: str = Field(pattern='^(active|disabled)$')

def change_customer(user_id, operator, status):
    check_permission(operator, 'users.manage')
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id,username,role,status FROM users WHERE id=%s FOR UPDATE', (user_id,))
            target = cur.fetchone()
            if not target or target['status'] == 'deleted':
                raise HTTPException(404, '用户不存在或已删除')
            if target['role'] == 'admin' or target['username'].lower() == 'admin':
                raise HTTPException(403, 'ADMIN 用户不可停用或删除')
            if target['id'] == operator['id']:
                raise HTTPException(400, '不能对当前登录用户执行此操作')
            cur.execute('UPDATE users SET status=%s WHERE id=%s', (status,user_id))
            if status != 'active':
                cur.execute('DELETE FROM sessions WHERE user_id=%s', (user_id,))
            if status == 'deleted':
                cur.execute('UPDATE api_keys SET enabled=false WHERE user_id=%s', (user_id,))
    return {'user_id':user_id,'status':status,'message':'用户已删除' if status == 'deleted' else ('用户已启用' if status == 'active' else '用户已停用')}

@app.put('/api/admin/users/{user_id}/status')
def update_user_status(user_id: int, payload: UserStatusPayload, user=Depends(require('users.manage'))):
    return change_customer(user_id, user, payload.status)

@app.delete('/api/admin/users/{user_id}')
def delete_user(user_id: int, user=Depends(require('users.manage'))):
    return change_customer(user_id, user, 'deleted')
@app.get("/api/admin/packages")
def admin_packages(user=Depends(require("packages.manage"))): return query("SELECT * FROM packages ORDER BY id")
@app.put("/api/admin/users/{user_id}/role")
def update_user_role(user_id:int,payload:RolePayload,user=Depends(require("users.manage"))):
    if payload.role not in ("admin","customer"): raise HTTPException(400,"角色无效")
    query("UPDATE users SET role=%s WHERE id=%s",(payload.role,user_id)); return {"user_id":user_id,"role":payload.role}
@app.get("/api/admin/roles")
def list_roles(user=Depends(require("users.manage"))): return query("SELECT role,permission_code FROM role_permissions ORDER BY role,permission_code")
@app.put("/api/admin/roles/{role}")
def update_role(role:str,payload:RolePayload,user=Depends(require("users.manage"))):
    check_permission(user, 'users.manage')
    if role not in ("admin","customer"): raise HTTPException(400,"角色无效")
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM role_permissions WHERE role=%s",(role,))
            for code in payload.permission_codes: cur.execute("INSERT INTO role_permissions(role,permission_code) VALUES(%s,%s) ON CONFLICT DO NOTHING",(role,code))
    return {"role":role,"permission_codes":payload.permission_codes}
@app.get("/api/admin/settings")
def list_settings(user=Depends(require("users.manage"))): return query("SELECT setting_key,setting_value,updated_at FROM platform_settings ORDER BY setting_key")
@app.put("/api/admin/settings/{setting_key}")
def update_setting(setting_key:str,payload:SettingPayload,user=Depends(require("users.manage"))):
    if setting_key == 'api_base_url' and payload.value.strip():
        parsed = urlparse(payload.value.strip())
        if parsed.scheme not in ('http','https') or not parsed.netloc or parsed.query or parsed.fragment or not parsed.path.rstrip('/').endswith('/v1'):
            raise HTTPException(400, 'API 调用基础地址必须为完整 HTTP/HTTPS 地址并以 /v1 结尾')
        payload.value = payload.value.strip().rstrip('/')
    return query("INSERT INTO platform_settings(setting_key,setting_value) VALUES(%s,%s) ON CONFLICT(setting_key) DO UPDATE SET setting_value=EXCLUDED.setting_value,updated_at=CURRENT_TIMESTAMP RETURNING setting_key,setting_value,updated_at",(setting_key,payload.value),True)
@app.post("/api/admin/packages")
def create_package(payload:PackagePayload,user=Depends(require("packages.manage"))):
    validate_model_routes(payload.model_routes)
    return query("INSERT INTO packages(name,price,quota,valid_days,enabled,model_routes) VALUES(%s,%s,%s,%s,%s,%s::jsonb) RETURNING *",(payload.name,payload.price,payload.quota,payload.valid_days,payload.enabled,json.dumps(payload.model_routes)),True)
@app.put("/api/admin/packages/{package_id}")
def update_package(package_id:int,payload:PackagePayload,user=Depends(require("packages.manage"))):
    validate_model_routes(payload.model_routes)
    row = query("UPDATE packages SET name=%s,price=%s,quota=%s,valid_days=%s,enabled=%s,model_routes=%s::jsonb WHERE id=%s RETURNING *", (payload.name,payload.price,payload.quota,payload.valid_days,payload.enabled,json.dumps(payload.model_routes),package_id),True)
    if not row: raise HTTPException(404,"套餐不存在")
    return row
@app.delete("/api/admin/packages/{package_id}")
def delete_package(package_id:int,user=Depends(require("packages.manage"))):
    row = query("DELETE FROM packages WHERE id=%s RETURNING id",(package_id,),True)
    if not row: raise HTTPException(404,"套餐不存在")
    return {"message":"套餐已删除"}

@app.post("/api/portal/orders")
def create_order(payload:OrderPayload,user=Depends(user_from_request)):
    check_permission(user, 'payments.create')
    package=query("SELECT * FROM packages WHERE id=%s AND enabled=TRUE",(payload.package_id,),True)
    if not package: raise HTTPException(404,"套餐不存在")
    order="TAAS-"+datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')+secrets.token_hex(4).upper()
    if not package['model_routes'] or package['quota'] <= 0:
        raise HTTPException(400, '此套餐尚未配置模型组合或 Token 额度，请联系管理员')
    validate_model_routes(package['model_routes'])
    return query("INSERT INTO payment_orders(order_no,user_id,package_id,amount,quota,entitlement,valid_days,model_routes) VALUES(%s,%s,%s,%s,%s,true,%s,%s::jsonb) RETURNING *",(order,user["id"],package["id"],package["price"],package["quota"],package['valid_days'],json.dumps(package['model_routes'])),True)
@app.post("/api/portal/orders/{order_no}/confirm")
def confirm_order(order_no:str,user=Depends(require("orders.confirm"))):
    check_permission(user, 'orders.confirm')
    return settle_order(order_no)

def settle_order(order_no, channel=None, amount=None, trade_no=None):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM payment_orders WHERE order_no=%s FOR UPDATE",(order_no,)); order=cur.fetchone()
            if not order: raise HTTPException(404,"订单不存在")
            if channel and (order['channel'] != channel or order['amount'] != amount):
                raise HTTPException(400, '支付渠道或金额与订单不一致')
            if not channel and order['channel'] != 'manual':
                raise HTTPException(400, '在线支付订单请等待支付平台通知')
            if trade_no and order.get('payment_trade_no') and order['payment_trade_no'] != trade_no:
                raise HTTPException(400, '支付交易号与订单不一致')
            if order["status"]!="pending": return dict(order)
            credit = Decimal(0) if order['entitlement'] else order['amount']
            cur.execute("UPDATE users SET balance=balance+%s WHERE id=%s RETURNING balance",(credit,order["user_id"])); balance=cur.fetchone()["balance"]
            cur.execute("UPDATE payment_orders SET status='paid',paid_at=CURRENT_TIMESTAMP,payment_trade_no=%s,expires_at=CASE WHEN entitlement THEN CURRENT_TIMESTAMP + valid_days * INTERVAL '1 day' ELSE NULL END WHERE id=%s",(trade_no,order["id"]))
            transaction_kind = 'package_payment' if order['entitlement'] else 'payment'
            cur.execute("INSERT INTO recharge_transactions(user_id,amount,balance_before,balance_after,kind,channel,reference,payment_trade_no) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",(order["user_id"],order["amount"],balance-credit,balance,transaction_kind,order['channel'],order_no,trade_no or ''))
            return {"order_no":order_no,"status":"paid","balance":balance}

class PaymentPayload(BaseModel):
    channel: str = Field(pattern='^(wechat|alipay)$')

@app.get('/api/portal/payment-methods')
def payment_methods(user=Depends(user_from_request)):
    config = payments.load_config()
    permissions = effective_permissions(user)
    balance = query('SELECT balance FROM users WHERE id=%s', (user['id'],), True)['balance']
    methods = [{'channel':'balance','name':'余额支付','available':balance > 0 and 'payments.create' in permissions,'reason':('余额不足，请先充值' if balance <= 0 else ('当前账号未获支付权限' if 'payments.create' not in permissions else ''))}]
    for channel, name in [('wechat','微信支付'),('alipay','支付宝支付')]:
        authorized = 'payments.' + channel in permissions
        ready, readiness_reason = payments.readiness(config, channel)
        reason = '当前账号未获此支付方式授权' if not authorized else readiness_reason
        methods.append({'channel':channel,'name':name,'available':authorized and ready,'reason':reason})
    return {'can_create_order':'payments.create' in permissions,'methods':methods}

@app.post('/api/portal/orders/{order_no}/pay-balance')
def pay_order_balance(order_no: str, user=Depends(user_from_request)):
    check_permission(user, 'payments.create')
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM payment_orders WHERE order_no=%s AND user_id=%s FOR UPDATE', (order_no,user['id']))
            order=cur.fetchone()
            if not order: raise HTTPException(404,'订单不存在')
            if order['status'] != 'pending': raise HTTPException(400,'订单已支付或不可支付')
            if order['channel'] != 'manual':
                raise HTTPException(400, '订单已发起其他渠道付款，请继续原方式支付，避免重复付款')
            cur.execute('SELECT balance FROM users WHERE id=%s FOR UPDATE',(user['id'],))
            balance=cur.fetchone()['balance']
            if balance < order['amount']: raise HTTPException(402,f'余额不足，订单需要 ¥{order["amount"]}，当前余额 ¥{balance}')
            after=balance-order['amount']
            cur.execute("UPDATE users SET balance=%s WHERE id=%s",(after,user['id']))
            cur.execute("UPDATE payment_orders SET status='paid',channel='balance',paid_at=CURRENT_TIMESTAMP,expires_at=CASE WHEN entitlement THEN CURRENT_TIMESTAMP + valid_days * INTERVAL '1 day' ELSE NULL END WHERE id=%s",(order['id'],))
            cur.execute("INSERT INTO recharge_transactions(user_id,amount,balance_before,balance_after,kind,channel,reference) VALUES(%s,%s,%s,%s,'balance_payment','balance',%s)",(user['id'],-order['amount'],balance,after,order_no))
            return {'order_no':order_no,'status':'paid','channel':'balance','balance':after}

@app.get('/api/admin/payments/config')
def payment_config(user=Depends(require('payments.configure'))):
    check_permission(user, 'payments.configure')
    return payments.public_config()

@app.put('/api/admin/payments/config')
def save_payment_config(payload: payments.PaymentConfig, user=Depends(require('payments.configure'))):
    check_permission(user, 'payments.configure')
    try:
        payments.save_config(payload)
    except (ValueError, TypeError):
        raise HTTPException(400, '支付配置无效，请检查 HTTPS 地址、商户信息、PEM 密钥和 APIv3 密钥') from None
    except OSError:
        raise HTTPException(500, '支付配置保存失败') from None
    return {'message': '支付配置已保存'}

@app.post('/api/portal/orders/{order_no}/pay')
def pay_order(order_no: str, payload: PaymentPayload, user=Depends(user_from_request)):
    check_permission(user, 'payments.' + payload.channel)
    config = payments.load_config()
    ready, reason = payments.readiness(config, payload.channel)
    if not ready:
        raise HTTPException(400, reason)
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM payment_orders WHERE order_no=%s AND user_id=%s FOR UPDATE', (order_no, user['id']))
            order = cur.fetchone()
            if not order: raise HTTPException(404, '订单不存在')
            if order['status'] != 'pending': raise HTTPException(400, '订单已支付或不可支付')
            if order['channel'] not in ('manual', payload.channel):
                raise HTTPException(400, '此订单已选择其他支付方式，请创建新订单')
            try:
                payments.cents(order['amount'])
            except ValueError as error:
                raise HTTPException(400, str(error)) from None
            cur.execute('UPDATE payment_orders SET channel=%s WHERE id=%s', (payload.channel, order['id']))
    try:
        result = payments.create_payment(order, payload.channel, config)
    except (ValueError, KeyError, TypeError):
        raise HTTPException(502, '支付下单失败，请检查支付配置或稍后重试') from None
    return {'order_no': order_no, 'channel': payload.channel, **result}

@app.get('/api/portal/orders/{order_no}')
def get_order(order_no: str, user=Depends(user_from_request)):
    check_permission(user, 'orders.view')
    order = query('SELECT order_no,amount,status,channel,paid_at FROM payment_orders WHERE order_no=%s AND user_id=%s', (order_no, user['id']), True)
    if not order: raise HTTPException(404, '订单不存在')
    return order

@app.post('/api/payments/wechat/notify')
async def wechat_notify(request: Request):
    try:
        order_no, amount, trade_no = payments.wechat_notification(request.headers, await request.body(), payments.load_config())
        settle_order(order_no, 'wechat', amount, trade_no)
    except (ValueError, KeyError, TypeError, InvalidTag, HTTPException, psycopg2.Error):
        return JSONResponse({'code': 'FAIL', 'message': '支付通知校验或入账失败'}, status_code=400)
    return JSONResponse({'code': 'SUCCESS', 'message': '成功'})

@app.post('/api/payments/alipay/notify')
async def alipay_notify(request: Request):
    try:
        form = await request.form()
        if len(form.multi_items()) != len(form):
            return PlainTextResponse('failure', status_code=400)
        trade = payments.alipay_notification(dict(form), payments.load_config())
        if trade:
            settle_order(trade[0], 'alipay', trade[1], trade[2])
    except (ValueError, KeyError, TypeError, HTTPException, psycopg2.Error):
        return PlainTextResponse('failure', status_code=400)
    return PlainTextResponse('success')
@app.post("/api/portal/redeem")
def redeem(payload:RedeemPayload,user=Depends(user_from_request)):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM redemption_codes WHERE code=%s AND status='active' FOR UPDATE",(payload.code.strip(),)); code=cur.fetchone()
            if not code: raise HTTPException(404,"兑换码无效或已使用")
            cur.execute("UPDATE users SET balance=balance+%s WHERE id=%s RETURNING balance",(code["amount"],user["id"])); balance=cur.fetchone()["balance"]
            cur.execute("UPDATE redemption_codes SET status='used',used_by=%s,used_at=CURRENT_TIMESTAMP WHERE id=%s",(user["id"],code["id"])); cur.execute("INSERT INTO recharge_transactions(user_id,amount,balance_before,balance_after,kind,channel,reference) VALUES(%s,%s,%s,%s,%s,%s,%s)",(user["id"],code["amount"],balance-code["amount"],balance,"redemption","redemption",code["code"]))
            return {"message":"兑换成功","amount":code["amount"],"quota":code["quota"],"balance":balance}
@app.post("/api/admin/recharge")
def recharge(payload:RechargePayload,user=Depends(require("recharges.manage"))):
    check_permission(user, 'recharges.manage')
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT balance FROM users WHERE id=%s FOR UPDATE",(payload.user_id,)); row=cur.fetchone()
            if not row: raise HTTPException(404,"用户不存在")
            after=row["balance"]+Decimal(str(payload.amount)); cur.execute("UPDATE users SET balance=%s WHERE id=%s",(after,payload.user_id)); cur.execute("INSERT INTO recharge_transactions(user_id,amount,balance_before,balance_after,kind,channel,reference,operator_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",(payload.user_id,payload.amount,row["balance"],after,"admin","manual",payload.reference,user['id'])); return {"user_id":payload.user_id,"balance":after}
@app.post("/api/admin/redemption-codes")
def create_codes(payload:CodePayload,user=Depends(require("packages.manage"))):
    rows=[]
    for _ in range(payload.count):
        code="RDM-"+secrets.token_hex(8).upper(); rows.append(query("INSERT INTO redemption_codes(code,amount,quota) VALUES(%s,%s,%s) RETURNING code,amount,quota,status",(code,payload.amount,payload.quota),True))
    return rows
@app.get("/api/admin/redemption-codes")
def list_codes(user=Depends(require("packages.manage"))): return query("SELECT * FROM redemption_codes ORDER BY id DESC LIMIT 500")

def api_key_from_request(request: Request, user_id=None):
    token=request.headers.get("authorization","").removeprefix("Bearer ").strip()
    row=query("SELECT t.*,u.status AS user_status FROM api_keys t JOIN users u ON u.id=t.user_id WHERE t.token=%s AND t.enabled=TRUE AND (t.expires_at IS NULL OR t.expires_at>CURRENT_TIMESTAMP)",(token,),True)
    if not row or row["user_status"]!="active": raise HTTPException(401,"无效 API Key")
    if user_id is not None and row['user_id'] != user_id:
        raise HTTPException(403, 'API Key 与调用地址所属用户不一致')
    if not row["unlimited_quota"] and row["quota"] and row["quota_used"]>=row["quota"]: raise HTTPException(429,"API Key 额度已用完")
    return row

@app.get("/v1/models")
@app.get("/api/v1/models")
def openai_models():
    rows=query("SELECT DISTINCT trim(model) AS id FROM channels, unnest(string_to_array(model_scope, ',')) model WHERE enabled=TRUE AND trim(model)<>'' ORDER BY id")
    return {"object":"list","data":[{"id":r["id"],"object":"model","owned_by":"taas"} for r in rows]}

class ModelPricePayload(BaseModel):
    input_price: Decimal = Field(ge=0, max_digits=16, decimal_places=6)
    output_price: Decimal = Field(ge=0, max_digits=16, decimal_places=6)

@app.get('/api/admin/model-prices')
def model_prices(user=Depends(require('keys.manage'))):
    check_permission(user, 'keys.manage')
    return query('SELECT * FROM model_prices ORDER BY model')

@app.put('/api/admin/model-prices/{model:path}')
def set_model_price(model: str, payload: ModelPricePayload, user=Depends(require('keys.manage'))):
    check_permission(user, 'keys.manage')
    if not model.strip() or len(model)>160: raise HTTPException(400, '模型名称无效')
    return query('INSERT INTO model_prices(model,input_price,output_price) VALUES(%s,%s,%s) ON CONFLICT(model) DO UPDATE SET input_price=EXCLUDED.input_price,output_price=EXCLUDED.output_price RETURNING *', (model.strip(),payload.input_price,payload.output_price),True)

TASK_TYPES = {'chat', 'short_text', 'coding', 'science'}

def package_models(routes):
    if 'models' in routes:
        return routes['models']
    return list(dict.fromkeys(routes.values()))

def validate_model_routes(routes):
    if 'models' in routes:
        models = routes['models']
        if set(routes) != {'models'} or not isinstance(models,list) or not models or any(not isinstance(model,str) or not model.strip() for model in models):
            raise HTTPException(400, '请至少添加一个套餐模型')
    elif not routes or 'chat' not in routes or any(task not in TASK_TYPES | {'synthesis'} or not isinstance(model, str) or not model.strip() for task, model in routes.items()):
        raise HTTPException(400, '套餐模型配置无效')
    for model in set(package_models(routes)):
        if not query('SELECT model FROM model_prices WHERE model=%s', (model,), True):
            raise HTTPException(400, f'模型 {model} 尚未配置价格')
        if not query("SELECT id FROM channels WHERE enabled AND (model_scope='' OR %s=ANY(string_to_array(model_scope, ','))) LIMIT 1", (model,), True):
            raise HTTPException(400, f'模型 {model} 没有可用渠道')

def usable_order(user_id, order_no=None, order_id=None):
    row = query('SELECT * FROM payment_orders WHERE user_id=%s AND (order_no=%s OR id=%s)', (user_id,order_no,order_id), True)
    if not row: raise HTTPException(404, '消费订单不存在或不属于当前用户')
    if not row['entitlement']: raise HTTPException(400, '旧订单属于余额入账记录，请购买模型组合套餐')
    if row['status'] != 'paid': raise HTTPException(402, '消费订单尚未支付')
    if not row['expires_at'] or row['expires_at'] <= datetime.now(timezone.utc): raise HTTPException(402, '消费订单已过期')
    if row['quota_used'] >= row['quota']: raise HTTPException(402, '消费订单额度已用完，请选择其他订单')
    return row

@app.get('/api/portal/consumption-orders')
def consumption_orders(user=Depends(user_from_request)):
    return query("SELECT order_no,amount,quota,quota_used,quota-quota_used AS remaining_tokens,expires_at,model_routes FROM payment_orders WHERE user_id=%s AND entitlement AND status='paid' AND expires_at>CURRENT_TIMESTAMP AND quota_used<quota ORDER BY paid_at,id", (user['id'],))

class ConsumptionOrderPayload(BaseModel):
    order_no: str | None = None

@app.put('/api/portal/keys/{key_id}/consumption-order')
def select_consumption_order(key_id: int, payload: ConsumptionOrderPayload, user=Depends(user_from_request)):
    order = usable_order(user['id'], payload.order_no) if payload.order_no else None
    row = query('UPDATE api_keys SET consumption_order_id=%s WHERE id=%s AND user_id=%s RETURNING id', (order['id'] if order else None,key_id,user['id']), True)
    if not row: raise HTTPException(404, 'API Key 不属于当前用户')
    return {'id':key_id,'order_no':payload.order_no,'selection':'selected' if order else 'oldest_paid'}

def classify_task(body):
    task = body.pop('task_type', None)
    if task is not None:
        if task not in TASK_TYPES: raise HTTPException(400, 'task_type 必须是 chat、short_text、coding 或 science')
        return task
    messages = body.get('messages') or []
    text = '\n'.join(str(message.get('content', '')) for message in messages if message.get('role') == 'user').lower()
    if any(term in text for term in ('代码','编程','开发','debug','python','javascript','sql','```')): return 'coding'
    if any(term in text for term in ('科学','微积分','方程','物理','数学','证明','积分','scientific','calculus')): return 'science'
    return 'short_text' if len(text) <= 200 else 'chat'

def reserve_order_usage(key, body, requested_order):
    with db() as conn:
        with conn.cursor() as cur:
            if requested_order:
                order = usable_order(key['user_id'], requested_order)
            elif key.get('consumption_order_id'):
                order = usable_order(key['user_id'], order_id=key['consumption_order_id'])
            else:
                cur.execute("SELECT * FROM payment_orders WHERE user_id=%s AND entitlement AND status='paid' AND expires_at>CURRENT_TIMESTAMP AND quota_used<quota ORDER BY paid_at,id LIMIT 1", (key['user_id'],))
                order = cur.fetchone()
                if not order: raise HTTPException(402, '没有可消费的已支付套餐订单，请先购买套餐')
            cur.execute('SELECT * FROM payment_orders WHERE id=%s FOR UPDATE', (order['id'],))
            order = cur.fetchone()
            if order['status'] != 'paid' or order['expires_at'] <= datetime.now(timezone.utc): raise HTTPException(402, '消费订单不可用')
            cur.execute('SELECT * FROM api_keys WHERE id=%s FOR UPDATE', (key['id'],))
            current_key = cur.fetchone()
            if not current_key or not current_key['enabled']: raise HTTPException(401, 'API Key 已停用')
            task = classify_task(body)
            model = body.get('model') or 'auto'
            routes = order['model_routes']
            models = package_models(routes)
            if model == 'auto':
                if 'models' in routes:
                    scope = {item.strip() for item in current_key['model_scope'].split(',') if item.strip()}
                    candidates = [candidate for candidate in models if not scope or candidate in scope]
                    ranked = query('SELECT model FROM model_prices WHERE model=ANY(%s) ORDER BY input_price+output_price,model', (candidates,))
                    model = ranked[-1 if task in ('coding','science') else 0]['model'] if ranked else None
                else:
                    model = routes.get(task)
                if not model: raise HTTPException(400, f'此套餐没有配置 {task} 模型，请选择其他任务或套餐')
            if model not in models: raise HTTPException(403, '消费订单未授权此模型')
            scope = {item.strip() for item in current_key['model_scope'].split(',') if item.strip()}
            if scope and model not in scope: raise HTTPException(403, 'API Key 未授权此模型')
            cur.execute('SELECT coalesce(sum(tokens),0) AS tokens FROM usage_reservations WHERE order_id=%s AND expires_at>CURRENT_TIMESTAMP', (order['id'],))
            remaining = order['quota'] - order['quota_used'] - cur.fetchone()['tokens']
            if current_key['quota'] and not current_key['unlimited_quota']:
                cur.execute('SELECT coalesce(sum(tokens),0) AS tokens FROM usage_reservations WHERE api_key_id=%s AND expires_at>CURRENT_TIMESTAMP', (key['id'],))
                remaining = min(remaining,current_key['quota']-current_key['quota_used']-cur.fetchone()['tokens'])
            if any(not isinstance(message.get('content'), str) for message in body.get('messages', [])):
                raise HTTPException(400, '订单计费目前支持纯文本对话')
            estimate = len(json.dumps({field:value for field,value in body.items() if field not in ('model','max_tokens','max_completion_tokens')},ensure_ascii=False).encode('utf-8')) + 256
            available = int(remaining) - estimate
            if available < 1: raise HTTPException(402, '所选订单或 API Key 的可用额度不足（包含正在调用的预留额度），请选择其他订单')
            output_field = 'max_completion_tokens' if 'max_completion_tokens' in body else 'max_tokens'
            requested = body.get(output_field, 1024)
            if not isinstance(requested, int) or isinstance(requested, bool) or requested < 1: raise HTTPException(400, '输出 Token 上限必须为正整数')
            body[output_field] = min(requested,available)
            if output_field == 'max_completion_tokens': body.pop('max_tokens',None)
            body['model'] = model
            reservation = secrets.token_hex(24)
            cur.execute("DELETE FROM usage_reservations WHERE expires_at<CURRENT_TIMESTAMP")
            cur.execute("INSERT INTO usage_reservations(id,order_id,api_key_id,tokens,expires_at) VALUES(%s,%s,%s,%s,CURRENT_TIMESTAMP + INTERVAL '30 minutes')", (reservation,order['id'],key['id'],estimate+body[output_field]))
            key.update(order_id=order['id'],order_no=order['order_no'],task_type=task,reservation_id=reservation,synthesis_model=routes.get('synthesis'))
            return key

def release_order_usage(key):
    query('DELETE FROM usage_reservations WHERE id=%s', (key['reservation_id'],))

async def relay_chat(request: Request, user_id=None):
    key = api_key_from_request(request,user_id)
    body = await request.json()
    if not isinstance(body, dict) or not isinstance(body.get('messages'), list) or not body['messages'] or any(not isinstance(message,dict) for message in body['messages']):
        raise HTTPException(400, '请提供非空 messages 列表')
    if body.get('n',1) != 1:
        raise HTTPException(400, '订单额度计费仅支持 n=1')
    requested_order = body.pop('order_no',None) or request.headers.get('x-taas-order-no')
    key = reserve_order_usage(key,body,requested_order)
    transferred = False
    try:
        composition_body = None
        if key.get('synthesis_model'):
            if body.get('tools') or body.get('functions') or body.get('n',1) != 1:
                raise HTTPException(400, '组合输出目前仅支持单候选纯文本对话，不支持工具调用')
            composition_body = {**body,'model':key['synthesis_model'],'task_type':key['task_type']}
            body['stream'] = False
        result = await relay_order_chat(key,body)
        if composition_body and result.status_code < 400:
            first_payload = json.loads(result.body)
            choices = first_payload.get('choices') or []
            draft = choices[0].get('message',{}).get('content') if choices else None
            if not isinstance(draft,str): raise HTTPException(502, '首个模型未返回可组合的文本')
            composition_body['messages'] = [*composition_body['messages'],{'role':'assistant','content':draft},{'role':'user','content':'请核查并完善以上草稿，结合原始问题给出最终回答，纠正错误；不要描述内部模型组合过程。'}]
            release_order_usage(key)
            key = reserve_order_usage(dict(key),composition_body,key['order_no'])
            key['task_type'] = 'synthesis'
            result = await relay_order_chat(key,composition_body)
            body = composition_body
        result.headers['X-TaaS-Order-No'] = key['order_no']
        result.headers['X-TaaS-Model'] = body['model']
        result.headers['X-TaaS-Task-Type'] = key['task_type']
        if isinstance(result,StreamingResponse):
            original = result.body_iterator
            async def tracked_stream():
                try:
                    async for chunk in original: yield chunk
                finally:
                    release_order_usage(key)
            result.body_iterator = tracked_stream()
            transferred = True
        return result
    finally:
        if not transferred: release_order_usage(key)

def settle_usage(key, channel, model, usage, price, started, status):
    tin = max(0,int(usage.get('prompt_tokens') or 0))
    tout = max(0,int(usage.get('completion_tokens') or 0))
    cost = ((Decimal(tin)*price['input_price']+Decimal(tout)*price['output_price'])/Decimal(1000000)).quantize(Decimal('0.00000001')) if status<400 else Decimal(0)
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id FROM payment_orders WHERE id=%s FOR UPDATE',(key['order_id'],))
            cur.execute('SELECT id FROM api_keys WHERE id=%s FOR UPDATE',(key['id'],))
            cur.execute('DELETE FROM usage_reservations WHERE id=%s RETURNING tokens',(key['reservation_id'],))
            reservation = cur.fetchone()
            if not reservation: raise HTTPException(409, '本次调用预留已失效或已结算，请联系管理员核查')
            consumed = tin+tout if status<400 else 0
            if consumed > reservation['tokens']:
                raise HTTPException(502, '上游用量超过预留上限，请联系管理员核查渠道；本次未扣除订单额度')
            cur.execute('UPDATE payment_orders SET quota_used=quota_used+%s WHERE id=%s',(consumed,key['order_id']))
            cur.execute('UPDATE api_keys SET quota_used=quota_used+%s WHERE id=%s',(consumed,key['id']))
            cur.execute('INSERT INTO usage_logs(user_id,api_key_id,channel_id,model,tokens_in,tokens_out,cost,status_code,latency_ms,order_id,task_type) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',(key['user_id'],key['id'],channel['id'],model,tin,tout,cost,status,int((datetime.now(timezone.utc)-started).total_seconds()*1000),key['order_id'],key['task_type']))

async def relay_order_chat(key, body):
    model=body['model']
    allowed_models = {item.strip() for item in (key.get('model_scope') or '').split(',') if item.strip()}
    if allowed_models and model not in allowed_models:
        raise HTTPException(403, 'API Key 未授权此模型')
    price=query('SELECT input_price,output_price FROM model_prices WHERE model=%s',(model,),True)
    if not price: raise HTTPException(400, '此模型尚未配置价格，请联系管理员（免费模型也需配置零价格）')
    channels=query("SELECT * FROM channels WHERE enabled=TRUE AND (%s='' OR model_scope='' OR %s=ANY(string_to_array(model_scope, ','))) ORDER BY weight DESC,id",(model,model))
    if not channels: raise HTTPException(503,"没有可用渠道")
    started=datetime.now(timezone.utc)
    if body.get("stream"):
        body['stream_options'] = {**(body.get('stream_options') or {}), 'include_usage':True}
        async def stream():
            usage = {}
            async with httpx.AsyncClient(timeout=120) as client:
                response = None
                channel = None
                for candidate in channels:
                    try:
                        candidate_request = client.build_request("POST", candidate["base_url"].rstrip('/')+"/chat/completions", json=body, headers={"Authorization":"Bearer "+candidate["api_key"],"Content-Type":"application/json"})
                        candidate_response = await client.send(candidate_request, stream=True)
                        if candidate_response.status_code < 500:
                            response, channel = candidate_response, candidate
                            break
                        await candidate_response.aclose()
                    except httpx.HTTPError:
                        continue
                if response is None:
                    yield 'data: '+json.dumps({'error':{'message':'所有上游渠道均不可用','code':503}},ensure_ascii=False)+'\n\n'
                    yield 'data: [DONE]\n\n'
                    return
                try:
                    if response.status_code >= 400:
                        settle_usage(key,channel,model,{},price,started,response.status_code)
                        yield "data: " + json.dumps({"error": {"message": "Upstream error", "code": response.status_code}}) + "\n\n"
                        return
                    async for line in response.aiter_lines():
                        if line.startswith('data:') and line[5:].strip() != '[DONE]':
                            try:
                                event=json.loads(line[5:].strip())
                                if event.get('usage'): usage=event['usage']
                            except (ValueError,AttributeError):
                                pass
                        if line.startswith('data:') and line[5:].strip() == '[DONE]':
                            continue
                        yield line+'\n'
                    try:
                        settle_usage(key,channel,model,usage,price,started,response.status_code)
                    except HTTPException as error:
                        yield 'data: '+json.dumps({'error':{'message':error.detail,'code':error.status_code}},ensure_ascii=False)+'\n\n'
                    if not usage:
                        yield 'data: '+json.dumps({'error':{'message':'上游未返回流式用量，本次未计费，请管理员核查渠道'}} ,ensure_ascii=False)+'\n\n'
                    yield 'data: [DONE]\n\n'
                finally:
                    await response.aclose()
        return StreamingResponse(stream(), media_type="text/event-stream")
    upstream_response = None
    channel = None
    async with httpx.AsyncClient(timeout=120) as client:
        for candidate in channels:
            try:
                response = await client.post(candidate["base_url"].rstrip('/')+"/chat/completions", json=body, headers={"Authorization":"Bearer "+candidate["api_key"],"Content-Type":"application/json"})
                if response.status_code < 500:
                    upstream_response, channel = response, candidate
                    break
            except httpx.HTTPError:
                continue
    if upstream_response is None:
        raise HTTPException(503, '所有上游渠道均不可用，请检查渠道配置') from None
    try:
        payload=upstream_response.json()
    except ValueError:
        raise HTTPException(502, '上游返回了无效 JSON') from None
    usage=payload.get("usage") or {}
    if upstream_response.status_code < 400 and not usage:
        settle_usage(key,channel,model,{},price,started,502)
        raise HTTPException(502, '上游未返回用量，无法进行订单结算，请联系管理员检查渠道')
    settle_usage(key,channel,model,usage,price,started,upstream_response.status_code)
    return JSONResponse(status_code=upstream_response.status_code,content=payload)

@app.post("/v1/chat/completions")
@app.post("/api/v1/chat/completions")
async def chat_completions(request: Request): return await relay_chat(request)

@app.get('/users/{user_id}/v1/models')
@app.get('/api/users/{user_id}/v1/models')
def user_models(user_id: int, request: Request):
    api_key_from_request(request, user_id)
    return openai_models()

@app.post('/users/{user_id}/v1/chat/completions')
@app.post('/api/users/{user_id}/v1/chat/completions')
async def user_chat_completions(user_id: int, request: Request):
    return await relay_chat(request, user_id)

@app.get("/api/admin/channels")
def list_channels(user=Depends(require("keys.manage"))):
    check_permission(user, 'keys.manage')
    return query("SELECT id,name,base_url,model_scope,enabled,weight,created_at FROM channels ORDER BY id DESC")
@app.get('/api/admin/channels/{channel_id}/details')
def channel_details(channel_id: int, user=Depends(require('keys.manage'))):
    check_permission(user, 'keys.manage')
    channel = query('SELECT * FROM channels WHERE id=%s', (channel_id,), True)
    if not channel: raise HTTPException(404, '渠道不存在')
    public = {key: value for key, value in channel.items() if key != 'api_key'}
    public['key_configured'] = bool(channel['api_key'])
    stats = query('''SELECT count(*) AS requests,
        count(*) FILTER (WHERE status_code<400) AS successes,
        count(*) FILTER (WHERE status_code>=400) AS failures,
        coalesce(sum(tokens_in),0) AS tokens_in, coalesce(sum(tokens_out),0) AS tokens_out,
        coalesce(sum(tokens_in+tokens_out),0) AS total_tokens,
        coalesce(sum(cost),0) AS cost, coalesce(avg(latency_ms),0) AS latency_ms,
        max(created_at) AS last_request_at FROM usage_logs WHERE channel_id=%s''', (channel_id,), True)
    models = query('''SELECT model,count(*) AS requests,sum(tokens_in) AS tokens_in,
        sum(tokens_out) AS tokens_out,sum(cost) AS cost FROM usage_logs
        WHERE channel_id=%s GROUP BY model ORDER BY model''', (channel_id,))
    upstream = {'status': 'unsupported', 'message': '此上游暂未适配余额接口，请到上游控制台查看。',
                'account_message': '上游未提供账户身份查询接口，无法读取用户名、邮箱等信息。', 'balances': []}
    parsed = urlparse(channel['base_url'])
    if parsed.scheme == 'https' and parsed.netloc == 'api.deepseek.com':
        try:
            response = httpx.get('https://api.deepseek.com/user/balance',
                                headers={'Authorization': 'Bearer ' + channel['api_key']}, timeout=15)
            if response.status_code == 200:
                data = response.json()
                balances = data.get('balance_infos')
                if not isinstance(balances, list): raise ValueError('Invalid balance response')
                upstream.update(status='ok', message='余额来自上游实时查询。',
                                is_available=data.get('is_available'), balances=[
                                    {key: row.get(key) for key in ('currency', 'total_balance', 'granted_balance', 'topped_up_balance')}
                                    for row in balances if isinstance(row, dict)])
            else:
                upstream.update(status='error', message=f'上游余额查询失败（HTTP {response.status_code}），请检查密钥或上游服务。')
        except (httpx.HTTPError, ValueError, AttributeError):
            upstream.update(status='error', message='上游余额查询失败或超时，请稍后刷新。')
    return {'channel': public, 'stats': stats, 'models': models, 'upstream': upstream,
            'queried_at': datetime.now(timezone.utc)}


def validate_channel(payload):
    parsed = urlparse(payload.base_url.strip())
    if parsed.scheme not in ('http','https') or not parsed.netloc or parsed.query or parsed.fragment or parsed.username:
        raise HTTPException(400, '请填写完整 HTTP/HTTPS 上游 API 地址')
    payload.base_url = payload.base_url.strip().rstrip('/')
    payload.name = payload.name.strip()
    if not payload.name: raise HTTPException(400, '渠道名称不能为空')
    payload.model_scope = ','.join(dict.fromkeys(model.strip() for model in payload.model_scope.split(',') if model.strip()))

@app.post("/api/admin/channels")
def create_channel(payload:ChannelPayload,user=Depends(require("keys.manage"))):
    check_permission(user, 'keys.manage')
    validate_channel(payload)
    if not payload.api_key.strip(): raise HTTPException(400, '新增渠道必须填写上游密钥')
    return query("INSERT INTO channels(name,base_url,api_key,model_scope,enabled,weight) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id,name,base_url,model_scope,enabled,weight,created_at",(payload.name,payload.base_url,payload.api_key.strip(),payload.model_scope,payload.enabled,payload.weight),True)

@app.put('/api/admin/channels/{channel_id}')
def update_channel(channel_id: int, payload: ChannelPayload, user=Depends(require('keys.manage'))):
    check_permission(user, 'keys.manage')
    validate_channel(payload)
    row=query("UPDATE channels SET name=%s,base_url=%s,api_key=COALESCE(NULLIF(%s,''),api_key),model_scope=%s,enabled=%s,weight=%s WHERE id=%s RETURNING id,name,base_url,model_scope,enabled,weight",(payload.name,payload.base_url,payload.api_key.strip(),payload.model_scope,payload.enabled,payload.weight,channel_id),True)
    if not row: raise HTTPException(404, '渠道不存在')
    return row

@app.post('/api/admin/channels/{channel_id}/test')
def test_channel(channel_id: int, user=Depends(require('keys.manage'))):
    check_permission(user, 'keys.manage')
    channel=query('SELECT base_url,api_key FROM channels WHERE id=%s',(channel_id,),True)
    if not channel: raise HTTPException(404, '渠道不存在')
    started=datetime.now(timezone.utc)
    try:
        response=httpx.get(channel['base_url'].rstrip('/')+'/models',headers={'Authorization':'Bearer '+channel['api_key']},timeout=15)
        response.raise_for_status()
        data=response.json().get('data')
        if not isinstance(data,list): raise ValueError('invalid model list')
    except (httpx.HTTPError,ValueError,AttributeError):
        raise HTTPException(502, '渠道测试失败，请检查地址、密钥及模型列表接口') from None
    return {'message':'连接成功','model_count':len(data),'latency_ms':int((datetime.now(timezone.utc)-started).total_seconds()*1000)}
@app.delete("/api/admin/channels/{channel_id}")
def delete_channel(channel_id:int,user=Depends(require("keys.manage"))):
    check_permission(user, 'keys.manage')
    if not query('DELETE FROM channels WHERE id=%s RETURNING id',(channel_id,),True): raise HTTPException(404, '渠道不存在')
    return {"message":"渠道已删除"}
@app.get("/api/admin/usage")
def list_usage(user=Depends(require("users.manage"))): return query("SELECT l.*,u.username,c.name AS channel_name FROM usage_logs l LEFT JOIN users u ON u.id=l.user_id LEFT JOIN channels c ON c.id=l.channel_id ORDER BY l.id DESC LIMIT 500")

@app.get('/api/admin/billing/statistics')
def billing_statistics(user=Depends(require('orders.manage')), start_date: date | None = None, end_date: date | None = None):
    check_permission(user, 'orders.manage')
    if start_date and end_date and start_date > end_date:
        raise HTTPException(400, '开始日期不能晚于结束日期')
    bounds = (start_date, start_date, end_date, end_date)
    date_filter = "(%s::date IS NULL OR (created_at AT TIME ZONE 'Asia/Shanghai')::date >= %s::date) AND (%s::date IS NULL OR (created_at AT TIME ZONE 'Asia/Shanghai')::date <= %s::date)"
    orders = query("""
        SELECT COUNT(*) AS total_orders,
               COUNT(*) FILTER (WHERE status='pending') AS pending_orders,
               COUNT(*) FILTER (WHERE status='paid') AS paid_orders,
               COALESCE(SUM(amount) FILTER (WHERE status='paid'),0) AS paid_amount,
               COALESCE(SUM(amount) FILTER (WHERE status='pending'),0) AS pending_amount
        FROM payment_orders WHERE
    """ + date_filter, bounds, one=True)
    recharges = query("""
        SELECT COUNT(*) FILTER (WHERE kind<>'balance_payment') AS transaction_count,
               COALESCE(SUM(amount) FILTER (WHERE kind<>'balance_payment'),0) AS credited_amount,
               COALESCE(SUM(amount) FILTER (WHERE kind='admin'),0) AS manual_amount,
               COALESCE(SUM(amount) FILTER (WHERE kind IN ('payment','package_payment')),0) AS payment_amount,
               COALESCE(SUM(amount) FILTER (WHERE kind='redemption'),0) AS redemption_amount,
               COALESCE(SUM(amount) FILTER (WHERE channel='wechat'),0) AS wechat_amount,
               COALESCE(SUM(amount) FILTER (WHERE channel='alipay'),0) AS alipay_amount,
               COALESCE(SUM(balance_after-balance_before) FILTER (WHERE kind<>'balance_payment'),0) AS wallet_credited_amount
        FROM recharge_transactions WHERE
    """ + date_filter, bounds, one=True)
    daily = query("SELECT (created_at AT TIME ZONE 'Asia/Shanghai')::date AS day, COUNT(*) AS transaction_count, SUM(amount) AS amount FROM recharge_transactions WHERE kind<>'balance_payment' AND " + date_filter + " GROUP BY day ORDER BY day", bounds)
    transactions = query("SELECT t.id,t.user_id,u.username,u.display_name,t.amount,t.balance_before,t.balance_after,t.kind,t.channel,t.payment_trade_no,t.reference,t.operator_id,operator.username AS operator_name,t.created_at FROM recharge_transactions t JOIN users u ON u.id=t.user_id LEFT JOIN users operator ON operator.id=t.operator_id WHERE " + date_filter.replace('created_at', 't.created_at') + " ORDER BY t.id DESC LIMIT 200", bounds)
    return {'orders': orders, 'recharges': recharges, 'daily': daily, 'transactions': transactions}
