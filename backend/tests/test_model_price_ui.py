# ============================================================
# 文件名称：test_model_price_ui.py
# 生成日期：2026-10-15
# 修改日期：2026-11-20
# 版本信息：v0.261115
# 作    者：JUN
# 备    注：本程序由AI辅助完成。
#           文件功能：验证 model_price_ui 相关功能。
#           文件用途：用于开发验证及功能回归。
#           注意事项：使用隔离测试环境，避免操作正式业务数据。
# ============================================================
import unittest
from decimal import Decimal
from unittest.mock import patch

from nicegui import ui
from app import taas_app as console


class ModelPriceUiTests(unittest.TestCase):
    def test_rows_save_their_own_model_and_prices(self):
        def save(model, payload, user):
            return {'model':model,'input_price':payload.input_price,'output_price':payload.output_price}
        with patch.object(console.api,'model_prices',return_value=[]), \
             patch.object(console.api,'openai_models',return_value={'data':[{'id':'model-a'},{'id':'model-b'}]}), \
             patch.object(console,'current_user',return_value={'id':1}), \
             patch.object(console.api,'set_model_price',side_effect=save) as saved, \
             patch.object(ui,'notify'):
            with ui.column() as root:
                console.model_price_manager({'id':1})
            numbers=[e for e in root.descendants() if isinstance(e,ui.number)]
            buttons=[e for e in root.descendants() if isinstance(e,ui.button) and e.text=='保存本模型']
            self.assertEqual(len(numbers),4)
            self.assertEqual(len(buttons),2)
            for element,value in zip(numbers,[1.5,3,10,20]): element.set_value(value)
            for button in buttons:
                next(listener for listener in button._event_listeners.values() if listener.type=='click').handler(None)
            self.assertEqual(saved.call_count,2)
            self.assertEqual(saved.call_args_list[0].args[0],'model-a')
            self.assertEqual(saved.call_args_list[0].args[1].input_price,Decimal('1.5'))
            self.assertEqual(saved.call_args_list[1].args[0],'model-b')
            self.assertEqual(saved.call_args_list[1].args[1].output_price,Decimal('20'))


if __name__ == '__main__': unittest.main()
