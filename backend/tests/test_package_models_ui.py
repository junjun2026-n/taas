# ============================================================
# 文件名称：test_package_models_ui.py
# 生成日期：2026-10-20
# 修改日期：2026-11-20
# 版本信息：v0.261115
# 作    者：JUN
# 备    注：本程序由AI辅助完成。
#           文件功能：验证 package_models_ui 相关功能。
#           文件用途：用于开发验证及功能回归。
#           注意事项：使用隔离测试环境，避免操作正式业务数据。
# ============================================================
import unittest
from unittest.mock import patch
from nicegui import ui
from app import taas_app as console


class PackageModelUiTests(unittest.TestCase):
    def test_package_table_displays_models_without_changing_saved_routes(self):
        package = dict(id=1,name='test',price=20,quota=1000,valid_days=30,enabled=True,
                       model_routes={'models':['model-a','model-b']})
        with patch.object(console.api,'admin_packages',return_value=[package]):
            with ui.column() as root: console.package_editor({'id':1})
            table = next(element for element in root.descendants() if isinstance(element,ui.table))
            self.assertEqual(table.rows[0]['model_routes'],'model-a、model-b')
            self.assertEqual(package['model_routes'],{'models':['model-a','model-b']})

    def test_channel_models_available_before_pricing_is_configured(self):
        with patch.object(console.api,'openai_models',return_value={'data':[{'id':'model-a'},{'id':'model-b'}]}), \
             patch.object(console,'query',return_value=[{'model':'model-a'}]):
            with ui.column(): choice=console.package_model_selector()
            self.assertEqual(set(choice.options),{'model-a','model-b'})
            self.assertEqual(choice.options['model-a'],'model-a')
            self.assertIn('未配置价格',choice.options['model-b'])
            choice.set_value(['model-a','model-b'])
            self.assertEqual(choice.value,['model-a','model-b'])

    def test_edit_preserves_unavailable_models(self):
        with patch.object(console.api,'openai_models',return_value={'data':[]}), \
             patch.object(console,'query',return_value=[]):
            with ui.column(): choice=console.package_model_selector(['old-model'])
            self.assertEqual(choice.value,['old-model'])
            self.assertIn('无启用渠道',choice.options['old-model'])


if __name__=='__main__': unittest.main()
