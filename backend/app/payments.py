# ============================================================
# 文件名称：payments.py
# 生成日期：2026-10-11
# 修改日期：2026-11-20
# 版本信息：v0.261115
# 作    者：JUN
# 备    注：本程序由AI辅助完成。
#           文件功能：实现 payments 模块相关功能。
#           文件用途：供TAAS的 taas_api/backend/app/payments.py 模块使用。
#           注意事项：运行前检查依赖与配置；涉及数据或服务变更时先备份并验证。
# ============================================================
import base64
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel

CONFIG_FILE = Path(__file__).resolve().parents[2] / 'config' / 'payments.json'
SECRET_FIELDS = {'wechat_private_key', 'wechat_api_v3_key', 'alipay_private_key'}


class PaymentConfig(BaseModel):
    public_base_url: str = ''
    wechat_enabled: bool = False
    wechat_app_id: str = ''
    wechat_mch_id: str = ''
    wechat_serial_no: str = ''
    wechat_private_key: str = ''
    wechat_api_v3_key: str = ''
    wechat_platform_serial: str = ''
    wechat_platform_public_key: str = ''
    alipay_enabled: bool = False
    alipay_app_id: str = ''
    alipay_seller_id: str = ''
    alipay_private_key: str = ''
    alipay_public_key: str = ''
    alipay_sandbox: bool = False


def load_config():
    if not CONFIG_FILE.exists():
        return PaymentConfig()
    return PaymentConfig.model_validate_json(CONFIG_FILE.read_text(encoding='utf-8'))


def public_config():
    config = load_config().model_dump()
    for field in SECRET_FIELDS:
        config[field] = ''
    return config


def readiness(config, channel):
    if channel == 'wechat':
        fields = (config.public_base_url, config.wechat_app_id, config.wechat_mch_id,
                  config.wechat_serial_no, config.wechat_private_key, config.wechat_api_v3_key,
                  config.wechat_platform_serial, config.wechat_platform_public_key)
        valid_secret = len(config.wechat_api_v3_key.encode()) == 32 if config.wechat_api_v3_key else False
    else:
        fields = (config.public_base_url, config.alipay_app_id, config.alipay_seller_id,
                  config.alipay_private_key, config.alipay_public_key)
        valid_secret = True
    parsed = urlparse(config.public_base_url)
    if parsed.scheme != 'https' or not parsed.netloc:
        return False, '管理员需配置公网 HTTPS 回调地址'
    if not all(fields):
        return False, '管理员尚未填写完整商户凭据'
    if not valid_secret:
        return False, '微信 APIv3 密钥必须为 32 字节'
    if not getattr(config, channel + '_enabled'):
        return False, '管理员尚未启用此支付方式'
    return True, ''


def private_key(value):
    key = serialization.load_pem_private_key(value.encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
        raise ValueError('请使用至少 2048 位的 RSA 私钥')
    return key


def public_key(value):
    key = serialization.load_pem_public_key(value.encode())
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
        raise ValueError('请使用至少 2048 位的 RSA 公钥')
    return key


def save_config(config):
    previous = load_config()
    for field in SECRET_FIELDS:
        if not getattr(config, field):
            setattr(config, field, getattr(previous, field))
    if config.wechat_enabled or config.alipay_enabled:
        parsed = urlparse(config.public_base_url)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError('请填写可公网访问的 HTTPS 平台地址')
        config.public_base_url = config.public_base_url.rstrip('/')
    if config.wechat_enabled:
        if not all((config.wechat_app_id, config.wechat_mch_id, config.wechat_serial_no, config.wechat_platform_serial)):
            raise ValueError('请填写完整微信商户信息和平台公钥 ID / 证书序列号')
        if len(config.wechat_api_v3_key.encode()) != 32:
            raise ValueError('微信 APIv3 密钥必须为 32 字节')
        private_key(config.wechat_private_key)
        public_key(config.wechat_platform_public_key)
    if config.alipay_enabled:
        if not config.alipay_app_id or not config.alipay_seller_id:
            raise ValueError('请填写支付宝应用 ID 和收款账户 UID')
        private_key(config.alipay_private_key)
        public_key(config.alipay_public_key)
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_FILE.with_suffix('.tmp')
    temporary.write_text(config.model_dump_json(indent=2), encoding='utf-8')
    temporary.replace(CONFIG_FILE)


def sign(message, pem):
    signature = private_key(pem).sign(message.encode(), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode()


def verify(message, signature, pem):
    try:
        public_key(pem).verify(base64.b64decode(signature, validate=True), message, padding.PKCS1v15(), hashes.SHA256())
    except (InvalidSignature, ValueError, TypeError):
        raise ValueError('支付签名校验失败') from None


def cents(amount):
    amount = Decimal(str(amount))
    if amount <= 0 or amount != amount.quantize(Decimal('0.01')):
        raise ValueError('在线支付金额必须大于零且最多两位小数')
    return int(amount * 100)


def verify_wechat_headers(headers, body, config):
    timestamp = headers.get('wechatpay-timestamp', '')
    nonce = headers.get('wechatpay-nonce', '')
    if abs(time.time() - int(timestamp)) > 300:
        raise ValueError('微信支付通知时间无效')
    if headers.get('wechatpay-serial') != config.wechat_platform_serial:
        raise ValueError('微信平台公钥 ID / 证书序列号不匹配')
    verify(timestamp.encode() + b'\n' + nonce.encode() + b'\n' + body + b'\n',
           headers.get('wechatpay-signature', ''), config.wechat_platform_public_key)


def create_payment(order, channel, config):
    amount = cents(order['amount'])
    if channel == 'alipay':
        if not config.alipay_enabled:
            raise ValueError('支付宝支付未启用')
        parameters = {
            'app_id': config.alipay_app_id, 'method': 'alipay.trade.page.pay',
            'format': 'JSON', 'charset': 'utf-8', 'sign_type': 'RSA2',
            'timestamp': datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S'), 'version': '1.0',
            'notify_url': config.public_base_url + '/api/payments/alipay/notify',
            'return_url': config.public_base_url + '/taas',
            'biz_content': json.dumps({'out_trade_no': order['order_no'], 'total_amount': f'{Decimal(amount) / 100:.2f}',
                                       'subject': 'TaaS 套餐充值', 'product_code': 'FAST_INSTANT_TRADE_PAY'}, ensure_ascii=False, separators=(',', ':')),
        }
        parameters['sign'] = sign('&'.join(f'{key}={parameters[key]}' for key in sorted(parameters)), config.alipay_private_key)
        gateway = 'https://openapi-sandbox.dl.alipaydev.com/gateway.do' if config.alipay_sandbox else 'https://openapi.alipay.com/gateway.do'
        return {'payment_url': gateway + '?' + urlencode(parameters)}
    if channel != 'wechat' or not config.wechat_enabled:
        raise ValueError('微信支付未启用')
    path = '/v3/pay/transactions/native'
    body = json.dumps({'appid': config.wechat_app_id, 'mchid': config.wechat_mch_id,
                       'description': 'TaaS 套餐充值', 'out_trade_no': order['order_no'],
                       'notify_url': config.public_base_url + '/api/payments/wechat/notify',
                       'amount': {'total': amount, 'currency': 'CNY'}}, ensure_ascii=False, separators=(',', ':'))
    timestamp, nonce = str(int(time.time())), secrets.token_hex(16)
    signature = sign(f'POST\n{path}\n{timestamp}\n{nonce}\n{body}\n', config.wechat_private_key)
    authorization = (f'WECHATPAY2-SHA256-RSA2048 mchid="{config.wechat_mch_id}",nonce_str="{nonce}",'
                     f'timestamp="{timestamp}",serial_no="{config.wechat_serial_no}",signature="{signature}"')
    try:
        response = httpx.post('https://api.mch.weixin.qq.com' + path, content=body.encode(),
                              headers={'Authorization': authorization, 'Content-Type': 'application/json',
                                       'Accept': 'application/json', 'Wechatpay-Serial': config.wechat_platform_serial}, timeout=15)
        response.raise_for_status()
    except httpx.HTTPError:
        raise ValueError('微信下单失败，请检查商户配置或稍后重试') from None
    verify_wechat_headers(response.headers, response.content, config)
    return {'code_url': response.json()['code_url']}


def wechat_notification(headers, body, config):
    if not config.wechat_enabled:
        raise ValueError('微信支付未启用')
    verify_wechat_headers(headers, body, config)
    envelope = json.loads(body)
    if envelope['event_type'] != 'TRANSACTION.SUCCESS':
        raise ValueError('非成功支付通知')
    resource = envelope['resource']
    if resource['algorithm'] != 'AEAD_AES_256_GCM':
        raise ValueError('微信通知加密方式无效')
    decrypted = AESGCM(config.wechat_api_v3_key.encode()).decrypt(
        resource['nonce'].encode(), base64.b64decode(resource['ciphertext']), resource.get('associated_data', '').encode())
    trade = json.loads(decrypted)
    if trade['appid'] != config.wechat_app_id or trade['mchid'] != config.wechat_mch_id or trade['trade_state'] != 'SUCCESS':
        raise ValueError('微信支付商户或状态不匹配')
    if trade['amount']['currency'] != 'CNY':
        raise ValueError('微信支付币种不匹配')
    return trade['out_trade_no'], Decimal(trade['amount']['total']) / 100, trade['transaction_id']


def alipay_notification(parameters, config):
    if not config.alipay_enabled or parameters.get('sign_type') != 'RSA2':
        raise ValueError('支付宝支付未启用或签名类型无效')
    message = '&'.join(f'{key}={parameters[key]}' for key in sorted(parameters)
                       if key not in ('sign', 'sign_type') and parameters[key] != '')
    verify(message.encode(), parameters.get('sign', ''), config.alipay_public_key)
    if parameters.get('app_id') != config.alipay_app_id or parameters.get('seller_id') != config.alipay_seller_id:
        raise ValueError('支付宝应用或收款账户不匹配')
    if parameters.get('trade_status') not in ('TRADE_SUCCESS', 'TRADE_FINISHED'):
        return None
    return parameters['out_trade_no'], Decimal(parameters['total_amount']), parameters['trade_no']
