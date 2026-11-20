# ============================================================
# 文件名称：test_checkout_ui.py
# 生成日期：2026-10-20
# 修改日期：2026-11-20
# 版本信息：v0.261115
# 作    者：JUN
# 备    注：本程序由AI辅助完成。
#           文件功能：验证 checkout_ui 相关功能。
#           文件用途：用于开发验证及功能回归。
#           注意事项：使用隔离测试环境，避免操作正式业务数据。
# ============================================================
import asyncio
import inspect
import unittest
from unittest.mock import AsyncMock, Mock, patch

from nicegui import ui
from app import taas_app as console


class CheckoutUiTests(unittest.TestCase):
    def render_checkout(self, *, can_create=True):
        user = {'id': 42}
        methods = {
            'can_create_order': can_create,
            'methods': [
                {'channel': 'wechat', 'name': 'WeChat', 'available': False, 'reason': 'unconfigured'},
                {'channel': 'balance', 'name': 'Balance', 'available': True, 'reason': ''},
            ],
        }
        patches = [
            patch.object(console.api, 'payment_methods', return_value=methods),
            patch.object(console, 'query', side_effect=lambda sql, *args: (
                [{'id': 9, 'name': 'package', 'price': 10, 'quota': 1000}]
                if 'FROM packages' in sql else [])),
            patch.object(console, 'current_permissions', return_value={'orders.view'}),
            patch.object(console, 'current_user', return_value=user),
            patch.object(console.ui, 'notify'),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        changed = Mock()
        with ui.column() as root:
            console.customer_checkout(user, show_transactions=False, on_order_change=changed)
        return root, changed

    def create_button(self, root):
        return next(e for e in root.descendants()
                    if isinstance(e, ui.button) and e.text == '\u521b\u5efa\u8ba2\u5355\uff08\u7a0d\u540e\u652f\u4ed8\uff09')

    def test_create_pending_order_without_online_payment_configuration(self):
        root, changed = self.render_checkout()
        button = self.create_button(root)
        handler = next(e.handler for e in button._event_listeners.values() if e.type == 'click')
        with patch.object(console.gui_run, 'io_bound', new_callable=AsyncMock,
                          return_value={'order_no': 'test-order'}) as execute:
            with patch('nicegui.functions.refreshable.refreshable.refresh'):
                asyncio.run(inspect.getclosurevars(handler).nonlocals['callback']())
        self.assertEqual(execute.await_args.args[0], console.api.create_order)
        self.assertEqual(execute.await_args.args[1].package_id, 9)
        self.assertEqual(execute.await_args.args[2]['id'], 42)
        changed.assert_called_once_with()
        self.assertTrue(any(isinstance(e, ui.button) and e.text == 'Balance'
                            for e in root.descendants()))

    def test_creation_error_restores_button_and_reports_reason(self):
        root, changed = self.render_checkout()
        button = self.create_button(root)
        handler = next(e.handler for e in button._event_listeners.values() if e.type == 'click')
        with patch.object(console.gui_run, 'io_bound', new_callable=AsyncMock,
                          side_effect=console.api.HTTPException(400, 'invalid package')):
            with patch('nicegui.functions.refreshable.refreshable.refresh'):
                asyncio.run(inspect.getclosurevars(handler).nonlocals['callback']())
        self.assertTrue(button.enabled)
        console.ui.notify.assert_called_with('invalid package', type='negative')
        changed.assert_not_called()

    def test_creation_requires_permission(self):
        root, _ = self.render_checkout(can_create=False)
        self.assertFalse(self.create_button(root).enabled)

    def test_pending_order_is_selected_for_payment(self):
        user = {'id': 42}
        with patch.object(console.api, 'payment_methods', return_value={
                'can_create_order': True, 'methods': []}), \
             patch.object(console, 'current_permissions', return_value={'orders.view'}), \
             patch.object(console, 'query', side_effect=lambda sql, *args: (
                 [] if 'FROM packages' in sql else [
                     {'order_no': 'latest-order', 'amount': 20, 'channel': 'manual'}])):
            with ui.column() as root:
                console.customer_checkout(user, show_transactions=False)
        choices = [element for element in root.descendants() if isinstance(element, ui.select)]
        self.assertEqual(choices[-1].value, 'latest-order')
        self.assertTrue(any(isinstance(element, ui.label) and
                            '\u5f53\u524d\u6682\u65e0\u53ef\u7528\u652f\u4ed8\u65b9\u5f0f' in element.text
                            for element in root.descendants()))


if __name__ == '__main__':
    unittest.main()
