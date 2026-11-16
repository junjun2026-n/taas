import unittest
from unittest.mock import Mock, patch

from nicegui import ui
from app import taas_app as console


class LazyTabsTests(unittest.TestCase):
    def test_admin_initial_page_queries_overview_only(self):
        permissions = {'users.manage', 'keys.manage', 'packages.manage',
                       'payments.configure', 'orders.manage'}
        with patch.object(console, 'current_user', return_value={'id': 1, 'role': 'admin', 'display_name': 'Admin'}), \
             patch.object(console, 'current_permissions', return_value=permissions), \
             patch.object(console, 'platform_name', return_value='Test'), \
             patch.object(console, 'query', return_value=dict(users=1, channels=2, keys=3, requests=4)) as query, \
             patch.object(console, 'payment_settings') as payment, \
             patch.object(console, 'channel_manager') as channels, \
             patch.object(ui, 'header', ui.row):
            with ui.column() as root:
                console.dashboard()
            query.assert_called_once()
            self.assertIn('AS requests', query.call_args.args[0])
            payment.assert_not_called()
            channels.assert_not_called()
            panels = next(e for e in root.descendants() if isinstance(e, ui.tab_panels))
            target = next(e for e in root.descendants() if isinstance(e, ui.tab) and e.label == '\u6e20\u9053')
            panels.set_value(target.props['name'])
            channels.assert_called_once()

    def test_hidden_panel_waits_for_selection_and_keeps_its_state(self):
        loader = console.LazyTabLoader()
        overview, hidden = Mock(), Mock()
        with ui.column():
            with ui.tabs() as tabs:
                first = ui.tab('first')
                second = ui.tab('second')
            with ui.tab_panels(tabs, value=first) as panels:
                loader(first)(overview)
                @loader(second)
                def build_hidden():
                    hidden()
                    ui.input('state', value='original')
            loader.connect(panels, first)
            overview.assert_called_once()
            hidden.assert_not_called()
            panels.set_value(second.props['name'])
            hidden.assert_called_once()
            field = next(e for e in loader.builders[second.props['name']][0].descendants() if isinstance(e, ui.input))
            field.value = 'edited'
            panels.set_value(first.props['name'])
            panels.set_value(second.props['name'])
            hidden.assert_called_once()
            self.assertEqual(field.value, 'edited')

    def test_customer_initial_page_builds_statistics_only(self):
        with patch.object(console, 'current_permissions', return_value={'orders.view'}), \
             patch.object(console, 'customer_statistics') as stats, \
             patch.object(console, 'customer_keys') as keys, \
             patch.object(console, 'customer_checkout') as checkout, \
             patch.object(console, 'query') as query:
            with ui.column() as root:
                console.customer_view({'id': 1, 'balance': 0})
            stats.assert_called_once()
            keys.assert_not_called()
            checkout.assert_not_called()
            query.assert_not_called()
            panels = next(e for e in root.descendants() if isinstance(e, ui.tab_panels))
            key_tab = next(e for e in root.descendants() if isinstance(e, ui.tab) and e.label == 'API Key')
            panels.set_value(key_tab.props['name'])
            keys.assert_called_once()
