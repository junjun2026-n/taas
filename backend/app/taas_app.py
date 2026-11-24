# ============================================================
# 文件名称：taas_app.py
# 生成日期：2026-10-11
# 修改日期：2026-11-20
# 版本信息：v0.261115
# 作    者：JUN
# 备    注：本程序由AI辅助完成。
#           文件功能：实现 taas_app 模块相关功能。
#           文件用途：供TAAS的 taas_api/backend/app/taas_app.py 模块使用。
#           注意事项：运行前检查依赖与配置；涉及数据或服务变更时先备份并验证。
# ============================================================
"""TaaS management console for the independent TaaS backend.

The REST/OpenAI API remains in ``main.py``; this module provides the Python UI.
"""
import os
import secrets
from datetime import date, datetime, timedelta, timezone
from nicegui import ui, app as gui_app, run as gui_run

from .main import app, hash_password, query
from . import main as api
import json
from pathlib import Path
from .database_config import DatabaseConfig, test_connection, save_config
from . import payments
import base64
import io
from urllib.parse import urlparse
import qrcode
from qrcode.image.svg import SvgPathImage

ASSET_DIR = Path(api.ROOT) / 'uploads'
ASSET_DIR.mkdir(parents=True, exist_ok=True)

LABELS = {
    'operator_id': '操作员 ID', 'operator_name': '操作员',
    'remaining_tokens': '剩余 Token', 'model_routes': '模型组合', 'task_type': '任务类型', 'order_id': '消费订单 ID', 'entitlement': '套餐额度订单',
    'id': '编号', 'username': '用户名', 'display_name': '显示名称',
    'email': '邮箱', 'role': '角色', 'status': '状态', 'balance': '余额',
    'name': '名称', 'quota': '总配额', 'quota_used': '已用配额',
    'enabled': '启用', 'created_at': '创建时间', 'base_url': '上游地址',
    'model_scope': '模型范围', 'weight': '权重', 'price': '价格',
    'valid_days': '有效天数', 'model': '模型', 'tokens_in': '输入 Token',
    'tokens_out': '输出 Token', 'status_code': '状态码', 'latency_ms': '延迟（毫秒）',
    'code': '兑换码', 'amount': '金额', 'used_by': '兑换用户', 'used_at': '兑换时间',
    'order_no': '订单号', 'user_id': '用户编号', 'permission_code': '权限代码',
    'object': '类型', 'owned_by': '提供方', 'platform_name': '平台名称',
    'platform_logo': '平台 Logo', 'default_currency': '默认币种',
    'default_input_price': '默认输入价格', 'default_output_price': '默认输出价格',
    'allow_register': '允许注册', 'gateway_timeout_seconds': '网关超时（秒）',
    'login_background': '登录背景图', 'portal_banner': '门户横幅图', 'favicon': '浏览器图标',
    'balance_before': '充值前余额', 'balance_after': '充值后余额',
    'kind': '充值来源', 'reference': '关联凭据',
    'channel': '支付方式', 'paid_at': '支付时间', 'payment_trade_no': '平台交易号',
    'api_base_url': 'API 调用基础地址（含 /v1，留空自动生成）',
    'input_price': '输入价格（元/百万 Token）', 'output_price': '输出价格（元/百万 Token）', 'cost': '调用费用（元）',
}
ROLE_LABELS = {'admin': '管理员', 'customer': '普通用户'}
STATUS_LABELS = {'active': '启用', 'disabled': '停用', 'deleted': '已删除', 'pending': '待支付', 'paid': '已支付', 'used': '已使用'}

def setting_value(key, default=''):
    row = query('SELECT setting_value FROM platform_settings WHERE setting_key=%s', (key,), one=True)
    return row['setting_value'] if row else default


def platform_name():
    return setting_value('platform_name').strip() or 'Qionghai TaaS'

def upload_asset(key, event):
    filename = Path(event.name).name
    suffix = Path(filename).suffix.lower()
    if suffix not in {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.ico', '.svg'}:
        ui.notify('仅支持 PNG、JPG、GIF、WEBP、ICO、SVG 图片', type='negative')
        return
    target = ASSET_DIR / f'{key}{suffix}'
    for old in ASSET_DIR.glob(f'{key}.*'):
        old.unlink(missing_ok=True)
    target.write_bytes(event.content.read())
    query('INSERT INTO platform_settings(setting_key,setting_value) VALUES(%s,%s) ON CONFLICT(setting_key) DO UPDATE SET setting_value=EXCLUDED.setting_value,updated_at=CURRENT_TIMESTAMP', (key, f'/taas-assets/{target.name}'))
    ui.notify('图片已上传', type='positive')

def remove_asset(key):
    for path in ASSET_DIR.glob(f'{key}.*'):
        path.unlink(missing_ok=True)
    query('DELETE FROM platform_settings WHERE setting_key=%s', (key,))
    ui.notify('图片已删除', type='positive')


def table(rows):
    rows = json.loads(json.dumps(rows, default=str))
    for row in rows:
        if 'role' in row:
            row['role'] = ROLE_LABELS.get(row['role'], row['role'])
        if 'status' in row:
            row['status'] = STATUS_LABELS.get(row['status'], row['status'])
        if 'kind' in row:
            row['kind'] = {'admin': '后台人工充值', 'payment': '订单入账', 'package_payment': '套餐支付', 'redemption': '兑换码入账', 'balance_payment': '余额支付支出'}.get(row['kind'], row['kind'])
        if 'channel' in row:
            row['channel'] = {'manual': '人工充值', 'balance': '余额支付', 'wechat': '微信支付', 'alipay': '支付宝', 'redemption': '兑换码'}.get(row['channel'], row['channel'])
    columns = [{'name': key, 'label': LABELS.get(key, key), 'field': key, 'sortable': True} for key in (rows[0] if rows else {})]
    return ui.table(columns=columns, rows=rows, pagination=15).classes('w-full')


def current_user():
    return query("SELECT u.* FROM users u JOIN sessions s ON s.user_id=u.id WHERE s.token=%s AND s.expires_at>CURRENT_TIMESTAMP AND u.status='active'", (gui_app.storage.user.get('token'),), one=True)


def key_details_dialog(result, title='API Key 详情'):
    with ui.dialog() as dialog, ui.card().classes('w-full max-w-xl'):
        ui.label(title).classes('text-xl font-bold')
        ui.label(f"用户：{result.get('username', '')}（ID：{result['user_id']}）").classes('font-bold')
        ui.label('以下为该用户的完整调用地址，可直接复制到客户端。')
        for key, label in [('name','名称'),('token','API Key'),('base_url','当前用户 Base URL（完整地址）'),('chat_completions_url','对话调用地址'),('models_url','模型列表地址'),('quota','总配额'),('quota_used','已用配额'),('model_scope','授权模型'),('expires_at','过期时间'),('enabled','启用'),('created_at','创建时间')]:
            if key not in result:
                continue
            value = str(result[key])
            if key in ('base_url','chat_completions_url','models_url'):
                ui.label(label).classes('font-medium')
                ui.label(value).classes('w-full font-mono').style('overflow-wrap: anywhere; user-select: text')
            else:
                ui.input(label, value=value).props('readonly').classes('w-full')
            if key in ('token','base_url','chat_completions_url','models_url'):
                ui.button('复制' + label, on_click=lambda value=value: ui.clipboard.write(value)).props('flat')
        ui.label('请求头：Authorization: Bearer 你的 API Key')
        ui.label('消费订单：' + (result.get('order_no') or '自动从最早付款的可用订单开始'))
        ui.label('model=auto 自动选择套餐模型；task_type 可指定 chat、short_text、coding、science；order_no 可指定本次消费订单。')
        viewer = current_user()
        if viewer and result['user_id'] == viewer['id']:
            available_orders = api.consumption_orders(current_user())
            choices = {'': '自动：最早付款的可用订单'}
            choices.update({order['order_no']: f"{order['order_no']} · 剩余 {order['remaining_tokens']} Token" for order in available_orders})
            selected = result.get('order_no') or ''
            if selected not in choices: choices[selected] = selected + '（已失效，请重新选择）'
            order_choice = ui.select(choices, value=selected, label='此 API Key 优先消费订单').classes('w-full')
            ui.button('保存消费订单', on_click=action(lambda: api.select_consumption_order(result['id'], api.ConsumptionOrderPayload(order_no=order_choice.value or None), current_user())))
        if urlparse(result['base_url']).hostname in ('127.0.0.1','localhost','::1'):
            ui.label('当前地址仅用于本机调用。其他设备调用时，请通过服务器 IP / 域名访问平台，或在系统配置中设置 API 调用基础地址。').classes('text-sm text-gray-600')
        ui.textarea('请求体示例（model 请替换为渠道中的模型名称）', value=json.dumps({'model':'模型名称','messages':[{'role':'user','content':'你好'}],'stream':False}, ensure_ascii=False, indent=2)).props('readonly').classes('w-full')
        ui.button('关闭', on_click=dialog.close)
        dialog.on('hide', lambda: ui.navigate.reload())
    dialog.open()


def api_key_table(rows, user):
    request = ui.context.client.request
    ui.label('双击 API Key 所在行查看密钥与调用地址').classes('text-sm text-gray-500')
    listing = table(rows)
    listing.columns.append({'name':'actions','label':'操作','field':'actions'})
    listing.add_slot('body', '''
        <q-tr :props="props" class="cursor-pointer" @dblclick="$parent.$emit('key-details', props.row.id)">
            <q-td v-for="col in props.cols" :key="col.name" :props="props">
                <template v-if="col.name === 'actions'">
                    <div class="row no-wrap q-gutter-xs" @dblclick.stop>
                        <q-btn flat dense :label="props.row.enabled ? '禁用' : '启用'" :color="props.row.enabled ? 'warning' : 'positive'"
                            @click.stop="$parent.$emit('key-status', props.row.id)" />
                        <q-btn flat dense label="删除" color="negative" @click.stop="$parent.$emit('key-delete', props.row.id)" />
                    </div>
                </template>
                <template v-else-if="col.name === 'enabled'">{{ props.row.enabled ? '启用' : '禁用' }}</template>
                <template v-else>{{ col.value }}</template>
            </q-td>
        </q-tr>
    ''')
    def show_details(event):
        try:
            key_details_dialog(api.key_details(int(event.args), current_user(), request))
        except Exception as error:
            ui.notify(str(getattr(error, 'detail', error)), type='negative')
    listing.on('key-details', show_details)
    def operate(event, delete=False):
        target = next((row for row in listing.rows if row['id'] == int(event.args)), None)
        if not target: return
        enabled = not target['enabled']
        label = '删除' if delete else ('启用' if enabled else '禁用')
        with ui.dialog() as dialog, ui.card().classes('w-full max-w-md'):
            ui.label(f"确认{label} API Key“{target['name']}”？").classes('text-lg font-bold')
            ui.label('删除后密钥永久失效，历史调用和消费记录保留。' if delete else
                     ('启用后可继续使用此密钥调用。' if enabled else '禁用后立即拒绝新调用，已开始的调用仍会结算。'))
            def confirm():
                confirm_button.disable()
                try:
                    viewer = current_user()
                    if not viewer: raise api.HTTPException(401, '会话已失效，请重新登录')
                    if delete:
                        api.delete_key(target['id'], viewer)
                        listing.rows = [row for row in listing.rows if row['id'] != target['id']]
                    else:
                        api.set_key_status(target['id'], api.KeyStatusPayload(enabled=enabled), viewer)
                        listing.rows = [{**row,'enabled':enabled} if row['id']==target['id'] else row for row in listing.rows]
                    listing.update()
                    dialog.close()
                    ui.notify(f'API Key 已{label}', type='positive')
                except Exception as error:
                    ui.notify(str(getattr(error, 'detail', error)), type='negative')
                finally:
                    confirm_button.enable()
            with ui.row().classes('w-full justify-end items-center gap-2 flex-nowrap'):
                ui.button('取消', on_click=dialog.close).props('flat')
                confirm_button = ui.button('确认' + label, on_click=confirm).props('color=negative' if delete else '')
        dialog.open()
    listing.on('key-status', operate)
    listing.on('key-delete', lambda event: operate(event, delete=True))


def action(fn):
    def perform():
        try:
            result = fn()
            ui.notify('操作成功', type='positive')
            if result:
                if isinstance(result, dict) and result.get('token') and result.get('base_url'):
                    key_details_dialog(result, 'API Key 创建成功')
                    return
                with ui.dialog() as dialog, ui.card():
                    ui.textarea('操作结果（请保存新密钥）', value=json.dumps(result, ensure_ascii=False, default=str, indent=2)).classes('w-96')
                    ui.button('关闭', on_click=dialog.close)
                dialog.on('hide', lambda: ui.navigate.reload())
                dialog.open()
            else:
                ui.navigate.reload()
        except Exception as error:
            ui.notify(str(getattr(error, 'detail', error)), type='negative')
    return perform


def user_manager(user):
    def operate(target, delete=False):
        status = 'disabled' if target['status'] == 'active' else 'active'
        label = '删除' if delete else ('停用' if status == 'disabled' else '启用')
        with ui.dialog() as dialog, ui.card():
            ui.label(f"确认{label}用户“{target['username']}”？")
            if delete:
                ui.label('删除后无法登录或调用 API，保留订单及充值记录，不可通过启用恢复。')
            def confirm():
                try:
                    if delete:
                        api.delete_user(target['id'], user)
                    else:
                        api.update_user_status(target['id'], api.UserStatusPayload(status=status), user)
                    dialog.close()
                    listing.refresh()
                    ui.notify(f'用户已{label}', type='positive')
                except Exception as error:
                    ui.notify(str(getattr(error, 'detail', error)), type='negative')
            with ui.row().classes('w-full items-center gap-3 flex-nowrap'):
                ui.button('确认' + label, on_click=confirm)
                ui.button('取消', on_click=dialog.close).props('outline')
        dialog.open()

    @ui.refreshable
    def listing():
        rows = query("SELECT id,username,display_name,email,role,status,balance FROM users WHERE status<>'deleted' ORDER BY id DESC")
        columns = [{'name':key,'label':LABELS.get(key,key),'field':key,'sortable':True} for key in ('id','username','display_name','email','role','status','balance')]
        columns.append({'name':'actions','label':'操作','field':'actions'})
        view = ui.table(columns=columns, rows=json.loads(json.dumps(rows, default=str)), row_key='id', pagination=15).classes('w-full')
        view.add_slot('body-cell-role', '<q-td :props="props">{{ props.value === "admin" ? "管理员" : "普通用户" }}</q-td>')
        view.add_slot('body-cell-status', '<q-td :props="props">{{ props.value === "active" ? "启用" : "停用" }}</q-td>')
        view.add_slot('body-cell-actions', '''
            <q-td :props="props">
                <template v-if="props.row.role !== 'admin' && props.row.username.toLowerCase() !== 'admin'">
                    <q-btn flat color="primary" :label="props.row.status === 'active' ? '停用' : '启用'" @click="$parent.$emit('user-status', props.row.id)" />
                    <q-btn flat color="negative" label="删除" @click="$parent.$emit('user-delete', props.row.id)" />
                </template>
                <span v-else>受保护</span>
            </q-td>
        ''')
        view.on('user-status', lambda event: operate(next(row for row in rows if row['id'] == int(event.args))))
        view.on('user-delete', lambda event: operate(next(row for row in rows if row['id'] == int(event.args)), True))
    listing()


def channel_details_dialog(channel_id):
    with ui.dialog() as dialog, ui.card().classes('w-full max-w-5xl'):
        ui.label('渠道详情').classes('text-xl font-bold')
        content = ui.column().classes('w-full')
        async def refresh():
            refresh_button.disable()
            try:
                viewer = current_user()
                if not viewer: raise api.HTTPException(401, '会话已失效，请重新登录')
                result = await gui_run.io_bound(api.channel_details, channel_id, viewer)
                content.clear()
                with content:
                    channel = result['channel']
                    ui.label(f"{channel['name']} · ID {channel['id']}").classes('text-lg font-bold')
                    for label, value in [('上游地址', channel['base_url']), ('状态', '启用' if channel['enabled'] else '停用'),
                                         ('路由权重', channel['weight']), ('模型范围', channel['model_scope'] or '不限'),
                                         ('上游密钥', '已配置' if channel['key_configured'] else '未配置'),
                                         ('创建时间', channel['created_at'])]:
                        ui.label(f'{label}：{value}')
                    ui.label('平台调用统计（本渠道全部历史记录）').classes('font-bold')
                    with ui.grid(columns=3).classes('w-full gap-3'):
                        for key, label in [('requests','调用次数'),('successes','成功次数'),('failures','失败次数'),
                                           ('tokens_in','输入 Token'),('tokens_out','输出 Token'),('total_tokens','总 Token')]:
                            with ui.card().classes('w-full'):
                                ui.label(label)
                                ui.label(str(result['stats'][key])).classes('text-xl')
                    ui.label(f"平台计费金额：¥{result['stats']['cost']}；平均耗时：{float(result['stats']['latency_ms']):.0f} ms")
                    ui.label(f"最近调用：{result['stats']['last_request_at'] or '暂无'}")
                    ui.label('平台统计仅包含经过本平台的调用；平台计费金额与上游实际账单可能不同。').classes('text-sm text-gray-500')
                    if result['models']: table(result['models'])
                    ui.separator()
                    ui.label('上游账户与余额').classes('font-bold')
                    upstream = result['upstream']
                    ui.label(upstream['account_message'])
                    ui.label(upstream['message'])
                    if upstream['status'] == 'ok':
                        ui.label('上游账户可用：' + ('是' if upstream.get('is_available') else '否'))
                        for balance in upstream['balances']:
                            ui.label(f"币种：{balance['currency']}；总余额：{balance['total_balance']}；赠送余额：{balance['granted_balance']}；充值余额：{balance['topped_up_balance']}")
                    ui.label(f"查询时间：{result['queried_at']}").classes('text-sm text-gray-500')
            except Exception as error:
                ui.notify(str(getattr(error, 'detail', error)), type='negative')
            finally:
                refresh_button.enable()
        with ui.row().classes('w-full items-center gap-3 flex-nowrap'):
            refresh_button = ui.button('刷新详情', on_click=refresh)
            ui.button('关闭', on_click=dialog.close).props('outline')
    dialog.open()
    return refresh


def channel_manager(user):
    def edit(channel=None):
        with ui.dialog() as dialog, ui.card().classes('w-full max-w-xl'):
            ui.label('修改渠道' if channel else '新增渠道').classes('text-xl font-bold')
            name = ui.input('渠道名称', value=channel['name'] if channel else '').classes('w-full')
            address = ui.input('上游 API 地址（含 /v1）', value=channel['base_url'] if channel else '').classes('w-full')
            secret = ui.input('上游密钥（修改时留空保留）', password=True).classes('w-full')
            scope = ui.input('模型名称（逗号分隔）', value=channel['model_scope'] if channel else '').classes('w-full')
            weight = ui.number('路由排序权重（较大值优先）', value=channel['weight'] if channel else 1, min=1, max=1000000)
            enabled = ui.switch('启用', value=channel['enabled'] if channel else True)
            def save():
                try:
                    payload=api.ChannelPayload(name=name.value,base_url=address.value,api_key=secret.value or '',model_scope=scope.value or '',weight=int(weight.value),enabled=enabled.value)
                    if channel: api.update_channel(channel['id'],payload,user)
                    else: api.create_channel(payload,user)
                    dialog.close(); listing.refresh()
                    ui.notify('渠道已保存',type='positive')
                except Exception as error:
                    ui.notify(str(getattr(error,'detail',error)),type='negative')
            with ui.row().classes('w-full items-center gap-3 flex-nowrap'):
                ui.button('保存',on_click=save)
                ui.button('取消',on_click=dialog.close).props('outline')
        dialog.open()

    async def test(channel):
        try:
            result=await gui_run.io_bound(api.test_channel,channel['id'],user)
            ui.notify(f"连接成功：{result['model_count']} 个模型，{result['latency_ms']} ms",type='positive')
        except Exception as error:
            ui.notify(str(getattr(error,'detail',error)),type='negative')

    def delete(channel):
        with ui.dialog() as dialog, ui.card():
            ui.label(f"确认删除渠道“{channel['name']}”？历史调用记录保留。")
            def confirm():
                try:
                    api.delete_channel(channel['id'],user)
                    dialog.close(); listing.refresh()
                except Exception as error:
                    ui.notify(str(getattr(error,'detail',error)),type='negative')
            with ui.row().classes('w-full items-center gap-3 flex-nowrap'):
                ui.button('确认删除',on_click=confirm).props('color=negative')
                ui.button('取消',on_click=dialog.close).props('outline')
        dialog.open()

    @ui.refreshable
    def listing():
        rows=api.list_channels(user)
        columns=[{'name':key,'label':LABELS.get(key,key),'field':key,'sortable':True} for key in ('id','name','base_url','model_scope','enabled','weight')]
        columns.append({'name':'actions','label':'操作','field':'actions'})
        view=ui.table(columns=columns,rows=json.loads(json.dumps(rows,default=str)),row_key='id',pagination=15).classes('w-full')
        view.add_slot('body-cell-enabled','<q-td :props="props">{{ props.value ? "启用" : "停用" }}</q-td>')
        view.add_slot('body-cell-actions','''
            <q-td :props="props">
                <q-btn flat label="详情" color="primary" @click="$parent.$emit('channel-details',props.row.id)" />
                <q-btn flat label="修改 / 启停" color="primary" @click="$parent.$emit('channel-edit',props.row.id)" />
                <q-btn flat label="测试" color="primary" @click="$parent.$emit('channel-test',props.row.id)" />
                <q-btn flat label="删除" color="negative" @click="$parent.$emit('channel-delete',props.row.id)" />
            </q-td>
        ''')
        def selected(event): return next(row for row in rows if row['id']==int(event.args))
        async def details_selected(event):
            await channel_details_dialog(selected(event)['id'])()
        view.on('channel-details', details_selected)
        view.on('channel-edit',lambda event:edit(selected(event)))
        async def test_selected(event): await test(selected(event))
        view.on('channel-test',test_selected)
        view.on('channel-delete',lambda event:delete(selected(event)))
    ui.button('新增渠道',on_click=lambda:edit())
    listing()


def package_editor(user):
    def edit(package=None):
        with ui.dialog() as dialog, ui.card().classes('w-96'):
            ui.label('修改套餐' if package else '新增套餐')
            name = ui.input('套餐名称', value=package['name'] if package else '')
            price = ui.number('价格', value=float(package['price']) if package else 0, min=0)
            quota = ui.number('Token 配额', value=package['quota'] if package else 0, min=0)
            days = ui.number('有效天数', value=package['valid_days'] if package else 30, min=1)
            enabled = ui.switch('启用', value=package['enabled'] if package else True)
            ui.label('从已启用渠道选择套餐模型，可多选。标记“未配置价格”的模型请先到模型计费中保存价格，再保存套餐。')
            routes = package.get('model_routes', {}) if package else {}
            selected_models = api.package_models(routes)
            model_choice = package_model_selector(selected_models)
            def save():
                try:
                    payload = api.PackagePayload(name=name.value.strip(),price=price.value,quota=int(quota.value),valid_days=int(days.value),enabled=enabled.value,model_routes={'models':list(dict.fromkeys(model_choice.value or []))})
                    if package:
                        api.update_package(package['id'],payload,user)
                    else:
                        api.create_package(payload,user)
                    dialog.close()
                    listing.refresh()
                    ui.notify('套餐已保存',type='positive')
                except Exception as error:
                    ui.notify(str(getattr(error,'detail',error)),type='negative')
            with ui.row().classes('w-full items-center gap-3 flex-nowrap'):
                ui.button('保存',on_click=save)
                ui.button('取消',on_click=dialog.close).props('outline')
        dialog.open()

    def delete(package):
        with ui.dialog() as dialog, ui.card():
            ui.label(f"确认删除套餐“{package['name']}”？历史订单将保留。")
            def confirm():
                try:
                    api.delete_package(package['id'],user)
                    dialog.close()
                    listing.refresh()
                    ui.notify('套餐已删除',type='positive')
                except Exception as error:
                    ui.notify(str(getattr(error,'detail',error)),type='negative')
            with ui.row().classes('w-full items-center gap-3 flex-nowrap'):
                ui.button('确认删除',on_click=confirm).props('color=negative')
                ui.button('取消',on_click=dialog.close).props('outline')
        dialog.open()

    @ui.refreshable
    def listing():
        rows = api.admin_packages(user)
        columns = [
            {'name': key, 'label': LABELS.get(key, key), 'field': key, 'sortable': True}
            for key in ('id', 'name', 'price', 'quota', 'valid_days', 'model_routes', 'enabled')
        ]
        columns.append({'name': 'actions', 'label': '操作', 'field': 'actions'})
        display_rows = [{**row, 'model_routes': '、'.join(api.package_models(row.get('model_routes') or {})) or '未配置'} for row in rows]
        package_table = ui.table(columns=columns, rows=json.loads(json.dumps(display_rows, default=str)), row_key='id', pagination=15).classes('w-full')
        package_table.add_slot('body-cell-enabled','<q-td :props="props">{{ props.value ? "启用" : "禁用" }}</q-td>')
        package_table.add_slot('body-cell-actions', '''
            <q-td :props="props">
                <q-btn flat color="primary" label="修改" @click="$parent.$emit('edit', props.row)" />
                <q-btn flat color="negative" label="删除" @click="$parent.$emit('delete', props.row)" />
            </q-td>
        ''')
        def selected_package(event):
            return next(package for package in rows if package['id'] == event.args['id'])
        package_table.on('edit', lambda event: edit(selected_package(event)))
        package_table.on('delete', lambda event: delete(selected_package(event)))
    ui.button('新增套餐',on_click=lambda: edit())
    listing()


class LazyTabLoader:
    """Build each panel on first selection, scoped to this page's client."""
    def __init__(self):
        self.builders = {}
        self.loaded = set()

    def __call__(self, tab):
        def register(build):
            with ui.tab_panel(tab) as panel:
                pass
            self.builders[tab.props['name']] = (panel, build)
            return build
        return register

    def load(self, name):
        if name in self.loaded or name not in self.builders:
            return
        panel, build = self.builders[name]
        panel.clear()
        with panel:
            build()
        self.loaded.add(name)

    def connect(self, panels, initial_tab):
        panels.on_value_change(lambda event: self.load(event.value))
        self.load(initial_tab.props['name'])


def customer_view(user):
    permissions = current_permissions(user)
    with ui.row().classes('w-full items-center justify-between'):
        ui.label(f'余额：¥{user["balance"]:,.2f}').classes('text-2xl font-bold')
        ui.button('刷新余额与订单', on_click=lambda: ui.navigate.to('/taas')).props('outline')
    with ui.tabs().classes('w-full') as customer_tabs:
        overview_sheet = ui.tab('我的统计')
        keys_sheet = ui.tab('API Key')
        orders_sheet = ui.tab('我的订单') if 'orders.view' in permissions else None
        usage_sheet = ui.tab('调用记录')
        payment_sheet = ui.tab('充值 / 支付')
        redeem_sheet = ui.tab('兑换码')
    lazy_tab = LazyTabLoader()
    with ui.tab_panels(customer_tabs, value=overview_sheet).classes('w-full customer-sheets') as panels:
        @lazy_tab(overview_sheet)
        def build_overview():
            customer_statistics(user)
        @lazy_tab(keys_sheet)
        def build_keys():
            customer_keys(user)
        if orders_sheet:
            @lazy_tab(orders_sheet)
            def build_orders():
                @ui.refreshable
                def my_orders():
                    with ui.card().classes('w-full'):
                        ui.label('我的订单').classes('text-xl font-bold')
                        ui.label('套餐付款后获得 Token 额度。消费订单可在 API Key 详情中选择；旧余额订单不重复授予额度。')
                        table(query('SELECT order_no,amount,status,channel,entitlement,quota,quota_used,quota-quota_used AS remaining_tokens,expires_at,model_routes,created_at,paid_at FROM payment_orders WHERE user_id=%s ORDER BY id DESC LIMIT 100', (user['id'],)))
                customer_checkout(user, show_transactions=False, on_order_change=my_orders.refresh)
                my_orders()
        @lazy_tab(usage_sheet)
        def build_usage():
            with ui.card().classes('w-full'):
                ui.label('我的调用记录（最近 100 次）').classes('text-xl font-bold')
                table(query('SELECT o.order_no,l.task_type,l.model,l.tokens_in,l.tokens_out,l.cost,l.status_code,l.latency_ms,l.created_at FROM usage_logs l LEFT JOIN payment_orders o ON o.id=l.order_id WHERE l.user_id=%s ORDER BY l.id DESC LIMIT 100', (user['id'],)))
        @lazy_tab(payment_sheet)
        def build_payment():
            customer_checkout(user)
        @lazy_tab(redeem_sheet)
        def build_redeem():
            with ui.card().classes('w-full'):
                ui.label('兑换码').classes('text-xl font-bold')
                code = ui.input('兑换码')
                ui.button('兑换', on_click=action(lambda: api.redeem(api.RedeemPayload(code=code.value), user)))

    lazy_tab.connect(panels, overview_sheet)


def customer_statistics(user):
    stats = api.portal_statistics(user)
    with ui.card().classes('w-full'):
        ui.label('我的使用统计').classes('text-xl font-bold')
        metrics = [
                ('API Key 数量', stats['key_count']),
                ('订单总数', stats['order_count']),
                ('待支付订单', stats['pending_order_count']),
                ('已支付金额', f"¥{stats['paid_amount']:,.2f}"),
                ('调用次数', stats['request_count']),
                ('输入 Token', stats['tokens_in']),
                ('输出 Token', stats['tokens_out']),
                ('调用费用', f"¥{stats['usage_cost']:,.4f}"),
        ]
        for row_metrics in (metrics[:5], metrics[5:]):
            with ui.element('div').classes('w-full gap-4').style(f'display: grid; grid-template-columns: repeat({len(row_metrics)}, minmax(0, 1fr))'):
                for label, value in row_metrics:
                    with ui.card().classes('w-full min-w-0 h-full'):
                        ui.label(label).classes('text-sm text-gray-600 break-words')
                        ui.label(str(value)).classes('text-2xl font-bold break-words')


def package_model_selector(selected_models=None):
    selected_models = list(selected_models or [])
    def options(selected):
        available = {row['id'] for row in api.openai_models()['data']}
        priced = {row['model'] for row in query('SELECT model FROM model_prices ORDER BY model')}
        result = {}
        for model in sorted(available | set(selected)):
            reasons = []
            if model not in available: reasons.append('无启用渠道')
            if model not in priced: reasons.append('未配置价格')
            result[model] = model + (f"（{'、'.join(reasons)}）" if reasons else '')
        return result
    choice = ui.select(options(selected_models), label='套餐包含的模型', value=selected_models,
                       multiple=True, with_input=True).props('use-chips clearable').classes('w-full')
    hint = ui.label('可以选择多个模型组合套餐。').classes('text-sm text-gray-500')
    def refresh():
        try:
            choices = options(choice.value or [])
            choice.set_options(choices, value=choice.value or [])
            hint.set_text('模型列表已刷新。' if choices else '暂无模型，请先配置并启用上游渠道及其模型列表。')
        except Exception as error:
            ui.notify(str(getattr(error, 'detail', error)), type='negative')
    if not choice.options:
        hint.set_text('暂无模型，请先配置并启用上游渠道及其模型列表。')
    ui.button('刷新套餐模型', on_click=refresh).props('flat dense')
    return choice


def model_price_manager(user):
    ui.label('模型计费（人民币元 / 百万 Token）').classes('text-xl font-bold')
    ui.label('每个模型单独设置输入、输出价格。未配置价格的模型不能调用；免费模型请明确保存为 0。').classes('text-sm text-gray-500')

    def price_row(model, price, available):
        with ui.element('tr'):
            with ui.element('td'):
                ui.label(model).classes('font-bold')
                ui.label('启用渠道提供' if available else '当前无启用渠道').classes('text-sm text-gray-500')
            with ui.element('td'):
                input_price = ui.number('输入单价', value=float(price['input_price']) if price else None,
                                        min=0, precision=6).props('outlined dense step=0.000001').classes('w-48')
            with ui.element('td'):
                output_price = ui.number('输出单价', value=float(price['output_price']) if price else None,
                                         min=0, precision=6).props('outlined dense step=0.000001').classes('w-48')
            with ui.element('td'):
                state = ui.label('已配置' if price else '未配置，无法调用')
                saved = ui.label(f"已保存：输入 {price['input_price']} / 输出 {price['output_price']}" if price else '请填写并保存本模型价格').classes('text-sm text-gray-500')
            def save():
                save_button.disable()
                try:
                    viewer = current_user()
                    if not viewer: raise api.HTTPException(401, '会话已失效，请重新登录')
                    result = api.set_model_price(model, api.ModelPricePayload(input_price=str(input_price.value),output_price=str(output_price.value)), viewer)
                    state.set_text('已配置')
                    saved.set_text(f"已保存：输入 {result['input_price']} / 输出 {result['output_price']}")
                    ui.notify('已保存 ' + model + ' 的价格', type='positive')
                except Exception as error:
                    ui.notify(str(getattr(error, 'detail', error)), type='negative')
                finally:
                    save_button.enable()
            with ui.element('td'):
                save_button = ui.button('保存本模型', on_click=save)

    @ui.refreshable
    def listing():
        prices = {row['model']:row for row in api.model_prices(user)}
        available = {row['id'] for row in api.openai_models()['data']}
        models = sorted(available | prices.keys())
        if not models:
            ui.label('暂无模型，请先在已启用渠道中配置模型列表。')
            return
        with ui.element('div').classes('w-full overflow-x-auto'):
            with ui.element('table').classes('w-full text-left').style('border-collapse: separate; border-spacing: 12px'):
                with ui.element('thead'), ui.element('tr'):
                    for label in ('模型', '输入单价（元 / 百万 Token）', '输出单价（元 / 百万 Token）', '计费状态', '操作'):
                        with ui.element('th'): ui.label(label)
                with ui.element('tbody'):
                    for model in models:
                        price_row(model, prices.get(model), model in available)
    ui.button('刷新模型与价格', on_click=listing.refresh).props('flat')
    listing()


def key_model_selector():
    def available_models():
        return [item['id'] for item in api.openai_models()['data']]
    options = available_models()
    choice = ui.select(options, value=[], multiple=True, with_input=True,
                       label='授权模型（可多选，留空表示不额外限制）').props('use-chips clearable').classes('w-full')
    hint = ui.label('模型来自已启用渠道，实际调用仍受套餐模型、订单额度和模型价格配置约束。').classes('text-sm text-gray-500')
    def refresh():
        try:
            models = available_models()
            choice.set_options(models, value=[model for model in (choice.value or []) if model in models])
            hint.set_text('已刷新模型列表。' if models else '暂无可选模型，请联系管理员在已启用的渠道中配置模型列表。')
        except Exception as error:
            ui.notify(str(getattr(error, 'detail', error)), type='negative')
    if not options:
        hint.set_text('暂无可选模型，请联系管理员在已启用的渠道中配置模型列表。')
    ui.button('刷新可选模型', on_click=refresh).props('flat dense')
    return choice


def key_model_scope(choice):
    return ','.join(dict.fromkeys(choice.value or []))


def customer_keys(user):
    with ui.card().classes('w-full'):
        ui.label('API Key').classes('text-xl font-bold')
        name = ui.input('名称')
        quota = ui.number('Token 配额（0 表示不限）', value=0, min=0)
        model_scope = key_model_selector()
        expires_at = ui.input('过期时间（留空表示永不过期）').props('type=datetime-local').classes('w-full')
        key_request = ui.context.client.request
        orders = api.consumption_orders(user)
        order_choice = ui.select({'': '自动：最早付款的可用订单', **{order['order_no']:f"{order['order_no']} · 剩余 {order['remaining_tokens']} Token" for order in orders}}, value='', label='优先消费订单').classes('w-full')
        ui.button('创建', on_click=action(lambda: api.create_portal_key(api.KeyPayload(name=name.value, quota=int(quota.value), model_scope=key_model_scope(model_scope), expires_at=expires_at.value or None,order_no=order_choice.value or None), user, key_request)))
        api_key_table(query('SELECT k.id,k.name,k.quota,k.quota_used,o.order_no,k.model_scope,k.expires_at,k.enabled FROM api_keys k LEFT JOIN payment_orders o ON o.id=k.consumption_order_id WHERE k.user_id=%s', (user['id'],)), user)


def customer_checkout(user, show_transactions=True, on_order_change=None):
    selected_order = None
    @ui.refreshable
    def checkout():
        methods = api.payment_methods(user)
        packages = query('SELECT id,name,price,quota FROM packages WHERE enabled=TRUE ORDER BY id')
        pending = query("SELECT order_no,amount,channel FROM payment_orders WHERE user_id=%s AND status='pending' ORDER BY id DESC", (user['id'],)) if 'orders.view' in current_permissions(user) else []
        with ui.card().classes('w-full'):
            ui.label('套餐下单与支付').classes('text-xl font-bold')
            ui.label('可先创建待支付订单，也可选择支付方式直接购买。支付成功后获得套餐额度。')
            package_choice = ui.select({row['id']:f"{row['name']} · ¥{row['price']} · {row['quota']} TOKEN" for row in packages}, label='选择充值套餐', value=packages[0]['id'] if packages else None).classes('w-full')
            if not packages:
                ui.label('暂无可购买套餐，请联系管理员添加并启用套餐。')
            if not methods['can_create_order']:
                ui.label('当前账号未获创建订单权限，请联系管理员授权。')
            elif not any(method['available'] for method in methods['methods']):
                ui.label('当前暂无可用支付方式：可以创建待支付订单，但暂时无法完成付款。余额不足时请联系管理员充值；微信、支付宝需管理员完成支付配置后才能使用。').classes('text-orange-800 bg-orange-50 rounded p-3 w-full')
            async def create_pending():
                nonlocal selected_order
                if package_choice.value is None:
                    ui.notify('请选择套餐', type='warning')
                    return
                create_button.disable()
                try:
                    viewer = current_user()
                    if not viewer: raise api.HTTPException(401, '会话已失效，请重新登录')
                    order = await gui_run.io_bound(api.create_order, api.OrderPayload(package_id=int(package_choice.value)), viewer)
                    selected_order = order['order_no']
                    ui.notify('下单成功，订单号：' + order['order_no'] + '（待支付）', type='positive')
                    checkout.refresh()
                    if on_order_change: on_order_change()
                except Exception as error:
                    ui.notify(str(getattr(error, 'detail', error)), type='negative')
                    create_button.enable()
            create_button = ui.button('创建订单（稍后支付）', on_click=create_pending)
            if not methods['can_create_order'] or not packages: create_button.disable()
            for method in methods['methods']:
                async def purchase(channel=method['channel']):
                    nonlocal selected_order
                    if package_choice.value is None:
                        ui.notify('请选择充值套餐', type='warning')
                        return
                    try:
                        order = await gui_run.io_bound(api.create_order, api.OrderPayload(package_id=int(package_choice.value)), user)
                        selected_order = order['order_no']
                        checkout.refresh()
                        if on_order_change: on_order_change()
                        if channel == 'balance':
                            result = await gui_run.io_bound(api.pay_order_balance, order['order_no'], user)
                            ui.notify(f"余额支付成功，剩余余额 ¥{result['balance']}", type='positive')
                            ui.navigate.reload()
                        else:
                            await show_payment(order['order_no'], channel, user)
                    except Exception as error:
                        ui.notify(str(getattr(error, 'detail', error)), type='negative')
                with ui.row().classes('items-center w-full'):
                    button = ui.button(method['name'], on_click=purchase)
                    if not method['available'] or not methods['can_create_order'] or not packages:
                        button.disable()
                    if method['reason']:
                        ui.label(f"{method['name']}暂不支持，请选择其他支付方式。原因：{method['reason']}").classes('text-sm text-orange-700')
            if pending:
                ui.separator()
                ui.label('继续支付已有订单').classes('font-bold')
                pending_options = {row['order_no']:f"{row['order_no']} · ¥{row['amount']}" for row in pending}
                order_choice = ui.select(pending_options, label='待支付订单', value=selected_order if selected_order in pending_options else pending[0]['order_no']).classes('w-full')
                for method in methods['methods']:
                    async def resume(channel=method['channel']):
                        if not order_choice.value:
                            ui.notify('请先选择订单', type='warning')
                            return
                        if channel == 'balance':
                            try:
                                result = await gui_run.io_bound(api.pay_order_balance, order_choice.value, user)
                                ui.notify(f"余额支付成功，剩余余额 ¥{result['balance']}", type='positive')
                                ui.navigate.reload()
                            except Exception as error:
                                ui.notify(str(getattr(error, 'detail', error)), type='negative')
                        else:
                            await show_payment(order_choice.value, channel, user)
                    button = ui.button('继续' + method['name'], on_click=resume)
                    if not method['available']:
                        button.disable()
                        ui.label(f"{method['name']}暂不支持，请选择其他支付方式。原因：{method['reason']}").classes('text-sm text-orange-700')
            ui.button('刷新支付状态', on_click=checkout.refresh).props('outline')
    checkout()
    if show_transactions:
        with ui.card().classes('w-full'):
            ui.label('我的充值流水').classes('text-xl font-bold')
            table(query('SELECT amount,balance_before,balance_after,kind,channel,payment_trade_no,reference,created_at FROM recharge_transactions WHERE user_id=%s ORDER BY id DESC LIMIT 100', (user['id'],)))


async def show_payment(order_no, channel, user):
    try:
        result = await gui_run.io_bound(api.pay_order, order_no, api.PaymentPayload(channel=channel), user)
        with ui.dialog() as dialog, ui.card().classes('w-96'):
            ui.label('微信扫码支付' if channel == 'wechat' else '支付宝支付').classes('text-xl')
            ui.label(f'订单：{order_no}')
            if result.get('code_url'):
                output = io.BytesIO()
                qrcode.make(result['code_url'], image_factory=SvgPathImage).save(output)
                ui.image('data:image/svg+xml;base64,' + base64.b64encode(output.getvalue()).decode()).classes('w-64 h-64')
            else:
                ui.link('打开支付宝收银台', result['payment_url'], new_tab=True)
            ui.label('自动查询支付结果，以支付平台通知为准。')
            status = ui.label()
            async def check():
                try:
                    order = await gui_run.io_bound(api.get_order, order_no, user)
                except Exception as error:
                    polling.deactivate()
                    status.text = str(getattr(error, 'detail', error))
                    return
                if order['status'] == 'paid':
                    polling.deactivate()
                    status.text = '支付成功，余额已到账'
                    dialog.close()
                    ui.notify('支付状态已更新', type='positive')
                    ui.navigate.reload()
                else:
                    status.text = '暂未收到支付成功通知，请稍后查询'
            if 'orders.view' in current_permissions(user):
                polling = ui.timer(3, check)
                ui.button('查询支付结果', on_click=check)
                dialog.on('hide', polling.deactivate)
            ui.button('关闭', on_click=dialog.close)
        dialog.open()
    except Exception as error:
        ui.notify(str(getattr(error, 'detail', error)), type='negative')


def payment_settings(user):
    current = payments.public_config()
    inputs = {}
    with ui.card().classes('w-full'):
        ui.label('微信与支付宝配置').classes('text-xl font-bold')
        inputs['public_base_url'] = ui.input('公网 HTTPS 平台地址', value=current['public_base_url']).classes('w-full')
        ui.label('私钥使用 PEM 格式，留空保留已保存密钥。保存后即时生效。')
        groups = [
            ('微信支付（Native 扫码）', [
                ('wechat_enabled', '启用微信支付'), ('wechat_app_id', '微信 AppID'),
                ('wechat_mch_id', '微信商户号'), ('wechat_serial_no', '商户证书序列号'),
                ('wechat_private_key', '微信商户私钥 PEM'), ('wechat_api_v3_key', '微信 APIv3 密钥'),
                ('wechat_platform_serial', '微信平台公钥 ID / 证书序列号'),
                ('wechat_platform_public_key', '微信平台公钥 PEM'),
            ]),
            ('支付宝（电脑网站支付）', [
                ('alipay_enabled', '启用支付宝支付'), ('alipay_sandbox', '支付宝沙箱模式'),
                ('alipay_app_id', '支付宝应用 ID'), ('alipay_seller_id', '支付宝收款账户 UID'),
                ('alipay_private_key', '支付宝应用私钥 PEM'),
                ('alipay_public_key', '支付宝公钥 PEM'),
            ]),
        ]
        for title, fields in groups:
            with ui.expansion(title).classes('w-full'):
                for key, label in fields:
                    if isinstance(current[key], bool):
                        inputs[key] = ui.switch(label, value=current[key])
                    elif key.endswith('private_key'):
                        inputs[key] = ui.textarea(label + '（留空保留）', value='').classes('w-full')
                    elif key in payments.SECRET_FIELDS:
                        inputs[key] = ui.input(label + '（留空保留）', password=True, password_toggle_button=True).classes('w-full')
                    elif key.endswith('public_key'):
                        inputs[key] = ui.textarea(label, value=current[key]).classes('w-full')
                    else:
                        inputs[key] = ui.input(label, value=current[key]).classes('w-full')
        async def save():
            try:
                payload = payments.PaymentConfig(**{key: field.value for key, field in inputs.items()})
                await gui_run.io_bound(api.save_payment_config, payload, user)
                for key in payments.SECRET_FIELDS:
                    inputs[key].value = ''
                ui.notify('支付配置已保存', type='positive')
                ui.navigate.reload()
            except Exception as error:
                ui.notify(str(getattr(error, 'detail', error)), type='negative')
        ui.button('保存支付配置', on_click=save)
        ui.label('微信回调：/api/payments/wechat/notify；支付宝回调：/api/payments/alipay/notify')


def signed_in() -> bool:
    token = gui_app.storage.user.get('token')
    if not token:
        return False
    return bool(query("SELECT s.user_id FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=%s AND s.expires_at>now() AND u.status='active'", (token,), one=True))


def current_permissions(user):
    return api.effective_permissions(user)


def clear_page():
    pass


def login_view():
    clear_page()
    with ui.column().classes('w-full max-w-md mx-auto mt-24 gap-4'):
        ui.label(platform_name()).classes('text-4xl font-bold')
        ui.label('v0.261115 · 请遵守版权协议。').classes('text-xs text-gray-500')
        username = ui.input('用户名').classes('w-full')
        password = ui.input('密码', password=True, password_toggle_button=True).classes('w-full')
        notice = ui.label().classes('text-red-600')

        def submit():
            row = query("SELECT id,username,display_name,role FROM users WHERE username=%s AND password_hash=%s AND status='active'", (username.value.strip(), hash_password(password.value)), one=True)
            if not row:
                notice.text = '账号或密码错误'
                return
            token = secrets.token_urlsafe(40)
            query('INSERT INTO sessions(token,user_id,expires_at) VALUES(%s,%s,%s)', (token,row['id'],datetime.now(timezone.utc)+timedelta(hours=12)))
            gui_app.storage.user.update({'token': token})
            ui.navigate.to('/taas')

        ui.button('登录', on_click=submit).classes('w-full')
        def register_customer():
            try:
                if len(password.value or '') < 8:
                    notice.text = '密码至少需要 8 个字符'
                    return
                api.register(api.Register(username=username.value.strip(), password=password.value, display_name=username.value.strip()))
                notice.text = '注册成功，请登录。'
                ui.notify('客户账号已创建', type='positive')
            except Exception as error:
                notice.text = str(getattr(error, 'detail', error))
        ui.button('注册客户账号', on_click=register_customer).classes('w-full')

def dashboard():
    user = current_user()
    if not user:
        login_view()
        return
    permissions = current_permissions(user)
    clear_page()
    with ui.header().classes('taas-header'):
        platform_heading = ui.label(f'{platform_name()} 管理控制台').classes('text-xl font-bold')
        ui.label('v0.261115 · 请遵守版权协议。').classes('text-xs')
        def logout():
            query('DELETE FROM sessions WHERE token=%s', (gui_app.storage.user.get('token'),))
            gui_app.storage.user.clear()
            ui.navigate.to('/taas')
        with ui.row().classes('items-center gap-4'):
            ui.label(f"{user.get('display_name')} · {ROLE_LABELS.get(user.get('role'), user.get('role'))}")
            ui.button('充值 / 支付', on_click=lambda: ui.navigate.to('/taas/payment')).props('outline color=white')
            ui.button('退出登录', on_click=logout).props('flat color=white')
    if user['role'] != 'admin':
        with ui.column().classes('customer-content'):
            customer_view(user)
        if not permissions.intersection({'users.manage','keys.manage','packages.manage','payments.configure','orders.manage','orders.confirm','recharges.manage'}):
            return
    with ui.tabs().classes('w-full') as tabs:
        overview_tab = ui.tab('总览')
        users_tab = ui.tab('用户') if 'users.manage' in permissions else None
        channels_tab = ui.tab('渠道') if 'keys.manage' in permissions else None
        packages_tab = ui.tab('套餐') if 'packages.manage' in permissions else None
        keys_tab = ui.tab('API Key') if 'keys.manage' in permissions else None
        models_tab = ui.tab('模型') if 'keys.manage' in permissions else None
        permissions_tab = ui.tab('角色权限') if 'users.manage' in permissions else None
        settings_tab = ui.tab('系统配置') if 'users.manage' in permissions else None
        payment_tab = ui.tab('支付配置') if 'payments.configure' in permissions else None
        usage_tab = ui.tab('调用日志') if 'users.manage' in permissions else None
        codes_tab = ui.tab('兑换码s') if 'packages.manage' in permissions else None
        orders_tab = ui.tab('订单与充值') if permissions.intersection({'orders.manage','orders.confirm','recharges.manage'}) else None
    lazy_tab = LazyTabLoader()
    with ui.tab_panels(tabs, value=overview_tab).classes('w-full') as panels:
        if payment_tab:
          @lazy_tab(payment_tab)
          def build_payment():
            payment_settings(user)
        if keys_tab:
          @lazy_tab(keys_tab)
          def build_keys():
            ui.label('API Key 管理').classes('text-2xl font-bold')
            ui.label('创建 API Key 并查看使用情况').classes('text-2xl font-bold')
            name = ui.input('密钥名称')
            key_quota = ui.number('Token 配额（0 表示不限）', value=0, min=0)
            key_scope = key_model_selector()
            key_expires = ui.input('过期时间（留空表示永不过期）').props('type=datetime-local').classes('w-full')
            key_request = ui.context.client.request
            ui.button('创建 API Key', on_click=action(lambda: api.create_key(api.KeyPayload(name=name.value,quota=int(key_quota.value),model_scope=key_model_scope(key_scope),expires_at=key_expires.value or None),user,key_request)))
            api_key_table(query('SELECT k.id,u.username,k.name,k.quota,k.quota_used,k.model_scope,k.expires_at,k.enabled,k.created_at FROM api_keys k JOIN users u ON u.id=k.user_id ORDER BY k.id DESC'), user)
        if models_tab:
          @lazy_tab(models_tab)
          def build_models():
            ui.label('模型列表').classes('text-2xl font-bold')
            ui.label('模型由已启用的渠道提供。').classes('text-2xl font-bold')
            table(api.openai_models()['data'])
            model_price_manager(user)
        if permissions_tab:
          @lazy_tab(permissions_tab)
          def build_permissions():
            ui.label('角色权限').classes('text-2xl font-bold')
            table(query('SELECT r.role,r.permission_code,p.name FROM role_permissions r JOIN permissions p ON p.code=r.permission_code ORDER BY r.role,r.permission_code'))
            ui.label('修改角色权限').classes('text-xl font-bold')
            role = ui.select(ROLE_LABELS, value='customer', label='角色')
            permission_options = {row['code']: row['name'] for row in query('SELECT code,name FROM permissions ORDER BY code')}
            role_permissions_input = ui.select(permission_options, multiple=True, label='权限', with_input=True).classes('w-full')
            def load_role():
                role_permissions_input.value = [row['permission_code'] for row in query('SELECT permission_code FROM role_permissions WHERE role=%s', (role.value,))]
            role.on_value_change(lambda _: load_role())
            load_role()
            def save_role():
                result = api.update_role(role.value, api.RolePayload(role=role.value, permission_codes=list(role_permissions_input.value or [])), user)
                ui.notify('角色权限已保存', type='positive')
                ui.navigate.reload()
            ui.button('保存角色权限', on_click=save_role)
            user_id = ui.number('用户 ID', min=1)
            user_role = ui.select(ROLE_LABELS, value='customer', label='用户角色')
            ui.button('修改用户角色', on_click=action(lambda: api.update_user_role(int(user_id.value), api.RolePayload(role=user_role.value), user)))
            ui.label('指定用户的支付与订单授权').classes('text-xl font-bold')
            ui.label('用户授权优先于角色权限；选择“继承角色”恢复角色设置。保存后权限即时生效，刷新页面更新菜单。')
            auth_users = query('SELECT id,username,display_name FROM users ORDER BY username')
            target_user = ui.select({row['id']: f"{row['username']} · {row['display_name']}（ID：{row['id']}）" for row in auth_users}, label='授权用户', with_input=True)
            override_inputs = {code: ui.select({'inherit':'继承角色','allow':'允许','deny':'禁止'}, value='inherit', label=name) for code,name in api.PAYMENT_PERMISSIONS.items()}
            def load_authorization():
                if not target_user.value: return
                details = api.user_permission_details(int(target_user.value), user)
                for code, field in override_inputs.items():
                    field.value = 'inherit' if code not in details['overrides'] else ('allow' if details['overrides'][code] else 'deny')
            target_user.on_value_change(lambda _: load_authorization())
            def save_authorization():
                if not target_user.value: raise api.HTTPException(400, '请选择授权用户')
                payload = api.UserPermissionPayload(overrides={code: field.value == 'allow' for code,field in override_inputs.items() if field.value != 'inherit'})
                return api.update_user_permissions(int(target_user.value), payload, user)
            ui.button('保存用户授权', on_click=action(save_authorization))
        if settings_tab:
          @lazy_tab(settings_tab)
          def build_settings():
            ui.label('系统配置').classes('text-2xl font-bold')
            if 'payments.configure' in permissions:
                payment_settings(user)
            with ui.card().classes('w-full'):
                ui.label('数据库配置').classes('text-xl font-bold')
                current_db = api.DATABASE_SETTINGS
                db_type = ui.select({'postgresql':'PostgreSQL（PQ）','mysql':'MySQL / MariaDB'}, label='数据库类型', value=current_db.get('database_type','postgresql'))
                host = ui.input('数据库地址', value=current_db.get('host', 'localhost'))
                port = ui.number('端口', value=int(current_db.get('port', 3306 if current_db.get('database_type')=='mysql' else 5432)), min=1, max=65535)
                db_type.on_value_change(lambda e: port.set_value(3306 if e.value=='mysql' else 5432))
                dbname = ui.input('数据库名称', value=current_db.get('dbname', ''))
                db_user = ui.input('数据库用户名', value=current_db.get('user', ''))
                db_password = ui.input('数据库密码（留空保留当前密码）', password=True, password_toggle_button=True)
                sslmode = ui.select(['disable','allow','prefer','require','verify-ca','verify-full'], label='SSL 模式', value=current_db.get('sslmode', 'prefer'))
                ui.label('保存前自动测试连接，重启服务后生效；不会迁移原数据库数据。环境变量 TAAS_DATABASE_URL 优先于本地配置。')
                def configure_database(save=False, initialize=False):
                    try:
                        if not query('SELECT 1 FROM role_permissions WHERE role=%s AND permission_code=%s', (user['role'], 'users.manage'), one=True):
                            raise ValueError('没有数据库配置权限')
                        same=db_type.value==current_db.get('database_type','postgresql') and (host.value.strip(),int(port.value),dbname.value.strip(),db_user.value.strip())==(current_db.get('host'),int(current_db.get('port',5432)),current_db.get('dbname'),current_db.get('user'))
                        if not db_password.value and not same:
                            raise ValueError('更换连接时请填写目标数据库密码')
                        config = DatabaseConfig(database_type=db_type.value,host=host.value.strip(),port=int(port.value),dbname=dbname.value.strip(),user=db_user.value.strip(),password=db_password.value or current_db.get('password',''),sslmode=sslmode.value)
                        if initialize:
                            from .database_config import initialize_database
                            initialize_database(config)
                        elif save:
                            save_config(config)
                        else:
                            test_connection(config)
                        ui.notify('初始化完成，已有数据保留；未切换连接，未迁移历史数据。' if initialize else ('配置已保存，重启服务后生效' if save else '数据库连接成功'), type='positive')
                    except (ValueError, TypeError) as error:
                        ui.notify(str(error), type='negative')
                ui.button('测试连接', on_click=lambda: configure_database())
                ui.button('保存数据库配置', on_click=lambda: configure_database(True))
                async def confirm_initialize():
                    with ui.dialog() as dialog, ui.card():
                        ui.label('初始化表单中的目标数据库？保留已有数据，不切换连接，也不迁移历史数据。')
                        with ui.row():
                            ui.button('取消', on_click=lambda: dialog.submit(False))
                            ui.button('确认初始化', on_click=lambda: dialog.submit(True))
                    if await dialog:
                        configure_database(initialize=True)
                ui.button('初始化数据库', on_click=confirm_initialize)
            ui.label('系统配置保存在 PostgreSQL 中。').classes('text-2xl font-bold')
            with ui.card().classes('w-full'):
                ui.label('平台图片资源').classes('text-xl font-bold')
                for key, label in [('platform_logo','平台 Logo'),('login_background','登录背景图'),('portal_banner','门户横幅图'),('favicon','浏览器图标')]:
                    with ui.row().classes('items-center w-full'):
                        ui.label(label).classes('w-40')
                        current = setting_value(key)
                        if current:
                            ui.image(current).classes('w-20 h-12 object-contain')
                        ui.upload(label='上传图片', on_upload=lambda e, k=key: upload_asset(k, e), auto_upload=True).props('accept="image/*"').classes('max-w-xs')
                        ui.button('删除', on_click=lambda k=key: remove_asset(k)).props('flat color=negative')
            setting_rows = query('SELECT setting_key,setting_value FROM platform_settings ORDER BY setting_key')
            setting_inputs = {}
            for item in setting_rows:
                setting_inputs[item['setting_key']] = ui.input(LABELS.get(item['setting_key'], item['setting_key']), value=item['setting_value']).classes('w-full')
            if not setting_rows:
                for key in ('platform_name','platform_logo','default_currency','default_input_price','default_output_price','allow_register','gateway_timeout_seconds'):
                    setting_inputs[key] = ui.input(LABELS.get(key, key), value='')
            if 'platform_name' not in setting_inputs:
                setting_inputs['platform_name'] = ui.input('平台名称', value=platform_name()).classes('w-full')
            if 'api_base_url' not in setting_inputs:
                setting_inputs['api_base_url'] = ui.input(LABELS['api_base_url'], value='').classes('w-full')
            def save_settings():
                for key, field in setting_inputs.items():
                    api.update_setting(key, api.SettingPayload(value=field.value), user)
                name = platform_name()
                platform_heading.text = f'{name} 管理控制台'
                ui.page_title(name)
                ui.notify('系统配置已保存', type='positive')
                ui.navigate.reload()
            ui.button('保存系统配置', on_click=save_settings)
        @lazy_tab(overview_tab)
        def build_overview():
            ui.label('运营总览').classes('text-2xl font-bold')
            metrics = query("SELECT (SELECT count(*) FROM users) AS users, (SELECT count(*) FROM channels) AS channels, (SELECT count(*) FROM api_keys) AS keys, (SELECT count(*) FROM usage_logs) AS requests", one=True) if user['role'] == 'admin' else {}
            with ui.row().classes('w-full gap-4'):
                for label, key in [('用户', 'users'), ('渠道', 'channels'), ('API Key', 'keys'), ('请求', 'requests')] if metrics else []:
                    with ui.card().classes('grow'):
                        ui.label(label)
                        ui.label(str(metrics[key])).classes('text-3xl font-bold')
        if users_tab:
          @lazy_tab(users_tab)
          def build_users():
            ui.label('用户管理').classes('text-2xl font-bold')
            with ui.row():
                username = ui.input('用户名')
                password = ui.input('初始密码', password=True)
                ui.button('新增用户', on_click=action(lambda: api.create_user(api.UserPayload(username=username.value,password=password.value),user)))
            user_manager(user)
        if channels_tab:
          @lazy_tab(channels_tab)
          def build_channels():
            ui.label('渠道管理').classes('text-2xl font-bold')
            channel_manager(user)
        if packages_tab:
          @lazy_tab(packages_tab)
          def build_packages():
            ui.label('套餐管理').classes('text-2xl font-bold')
            package_editor(user)
        if usage_tab:
          @lazy_tab(usage_tab)
          def build_usage():
            ui.label('调用日志').classes('text-2xl font-bold')
            rows = query("SELECT id,model,tokens_in,tokens_out,cost,status_code,latency_ms,created_at FROM usage_logs ORDER BY id DESC LIMIT 200")
            table(rows)
        if codes_tab:
          @lazy_tab(codes_tab)
          def build_codes():
            amount = ui.number('兑换閲戦', value=0, min=0)
            count = ui.number('数量', value=1, min=1, max=100)
            ui.button('生成兑换码', on_click=action(lambda: api.create_codes(api.CodePayload(amount=amount.value,count=int(count.value)),user)))
            table(query('SELECT * FROM redemption_codes ORDER BY id DESC LIMIT 100'))
        if orders_tab:
          @lazy_tab(orders_tab)
          def build_orders():
            ui.label('订单与充值统计').classes('text-2xl font-bold')
            with ui.row():
                start_date = ui.input('开始日期').props('type=date')
                end_date = ui.input('结束日期').props('type=date')
            @ui.refreshable
            def billing_summary():
                if 'orders.manage' not in permissions:
                    return
                try:
                    summary = api.billing_statistics(user, date.fromisoformat(start_date.value) if start_date.value else None, date.fromisoformat(end_date.value) if end_date.value else None)
                except (ValueError, api.HTTPException) as error:
                    ui.notify(str(getattr(error, 'detail', error)), type='negative')
                    return
                orders = summary['orders']
                recharges = summary['recharges']
                metrics = [
                        ('订单总数', orders['total_orders']),
                        ('待支付订单', orders['pending_orders']),
                        ('已支付订单', orders['paid_orders']),
                        ('已支付金额', f"¥{orders['paid_amount']:,.2f}"),
                        ('待支付金额', f"¥{orders['pending_amount']:,.2f}"),
                        ('充值笔数', recharges['transaction_count']),
                        ('充值及套餐支付总额', f"¥{recharges['credited_amount']:,.2f}"),
                        ('钱包实际入账', f"¥{recharges['wallet_credited_amount']:,.2f}"),
                        ('手动充值', f"¥{recharges['manual_amount']:,.2f}"),
                        ('订单入账', f"¥{recharges['payment_amount']:,.2f}"),
                        ('微信支付', f"¥{recharges['wechat_amount']:,.2f}"),
                        ('支付宝支付', f"¥{recharges['alipay_amount']:,.2f}"),
                        ('兑换码入账', f"¥{recharges['redemption_amount']:,.2f}"),
                ]
                split = (len(metrics) + 1) // 2
                with ui.column().classes('w-full gap-4'):
                    for row_metrics in (metrics[:split], metrics[split:]):
                        with ui.element('div').classes('w-full gap-4').style(f'display: grid; grid-template-columns: repeat({len(row_metrics)}, minmax(0, 1fr))'):
                            for label, value in row_metrics:
                                with ui.card().classes('w-full min-w-0 h-full'):
                                    ui.label(label).classes('w-full min-h-12 break-words')
                                    ui.label(str(value)).classes('w-full text-2xl font-bold break-words')
                ui.label('按北京时间筛选，包含结束日期。套餐支付金额计入支付总额，钱包实际入账单独统计；余额支付支出不计入充值总额。留空表示全部历史记录。')
                ui.label('每日充值趋势').classes('text-xl font-bold')
                ui.echart({
                    'tooltip': {'trigger': 'axis'},
                    'xAxis': {'type': 'category', 'data': [str(item['day']) for item in summary['daily']]},
                    'yAxis': {'type': 'value', 'name': '金额（元）'},
                    'series': [{'name': '入账金额', 'type': 'bar', 'data': [float(item['amount']) for item in summary['daily']]}],
                }).classes('w-full h-64')
                ui.label('充值明细（最近 200 笔）').classes('text-xl font-bold')
                table(summary['transactions'])
            billing_summary()
            ui.button('刷新统计', on_click=billing_summary.refresh)
            recharge_users = query('SELECT id,username,display_name FROM users ORDER BY username')
            uid = ui.select(
                {item['id']: f"{item['username']} · {item['display_name']}（ID：{item['id']}）" for item in recharge_users},
                label='充值用户', with_input=True,
            ).classes('w-96')
            credit = ui.number('充值金额', min=0.01)
            reference = ui.input('充值备注／凭据')
            def recharge_selected_user():
                if uid.value is None:
                    raise api.HTTPException(400, '请选择充值用户')
                result = api.recharge(api.RechargePayload(user_id=int(uid.value),amount=credit.value,reference=reference.value or ''),user)
                billing_summary.refresh()
                return result
            if 'recharges.manage' in permissions:
                ui.button('充值', on_click=action(recharge_selected_user))
            order_no = ui.input('已核实付款的订单号')
            if 'orders.confirm' in permissions:
                ui.button('确认入账', on_click=action(lambda: api.confirm_order(order_no.value,user)))
            if 'orders.manage' in permissions:
                table(query('SELECT o.order_no,o.user_id,u.username,u.display_name,o.amount,o.status,o.created_at FROM payment_orders o JOIN users u ON u.id=o.user_id ORDER BY o.id DESC LIMIT 100'))

    lazy_tab.connect(panels, overview_tab)



def page_layout():
    ui.add_css('''
        .nicegui-content { width: 100%; box-sizing: border-box; padding: 24px; }
        .taas-header { display: flex; align-items: center; justify-content: space-between;
                       gap: 16px; padding: 16px 24px; }
        .customer-content { width: 100%; max-width: 1200px; margin: 0 auto;
                            min-width: 0; gap: 24px; align-items: stretch; }
        .customer-content > .q-card { width: 100%; min-width: 0; padding: 24px; gap: 16px; }
        .customer-sheets .q-tab-panel { padding: 0; min-width: 0; }
        .customer-sheets .q-tab-panel > .q-card { width: 100%; min-width: 0; padding: 24px; gap: 16px; }
        .customer-content .q-field:not(.q-table__select) { width: 100%; }
        .q-table__bottom { flex-wrap: nowrap; overflow-x: auto; }
        .q-table__control { flex-wrap: nowrap; flex-shrink: 0; white-space: nowrap; }
        .q-table__bottom .q-table__select { width: auto; flex: 0 0 auto; }
        .q-table__bottom .q-table__bottom-item { white-space: nowrap; flex-shrink: 0; }
        .customer-content .q-table__container { width: 100%; min-width: 0; max-width: 100%; }
        .customer-content .q-table__middle { overflow-x: auto; }
        @media (max-width: 600px) {
            .nicegui-content { padding: 16px; }
            .taas-header { padding: 12px 16px; }
            .customer-content > .q-card { padding: 16px; }
            .customer-sheets .q-tab-panel > .q-card { padding: 16px; }
        }
    ''')
    ui.page_title(platform_name())


@ui.page('/taas/payment')
def payment_page():
    page_layout()
    if not signed_in():
        ui.label('请先登录后进行充值与支付。')
        ui.button('前往登录', on_click=lambda: ui.navigate.to('/taas'))
        return
    user = query("SELECT u.id,u.username,u.display_name,u.role,u.balance FROM users u JOIN sessions s ON s.user_id=u.id WHERE s.token=%s", (gui_app.storage.user['token'],), one=True)
    with ui.header().classes('taas-header'):
        ui.label(f'{platform_name()} · 充值与支付').classes('text-xl font-bold')
        ui.button('返回控制台', on_click=lambda: ui.navigate.to('/taas')).props('flat color=white')
    with ui.column().classes('customer-content'):
        ui.label(f"{user['display_name']} · 余额：¥{user['balance']:,.2f}").classes('text-xl font-bold')
        customer_checkout(user)


@ui.page('/taas')
def taas_page():
    page_layout()
    dashboard()


def run():
    secret_path = __import__('pathlib').Path(__file__).resolve().parents[2] / '.storage-secret'
    if not secret_path.exists():
        secret_path.write_text(secrets.token_hex(32), encoding='utf-8')
    ui.run_with(app, title='Qionghai TaaS', language='zh-CN', storage_secret=os.getenv('TAAS_STORAGE_SECRET') or secret_path.read_text(encoding='utf-8'), show_welcome_message=False)


run()


