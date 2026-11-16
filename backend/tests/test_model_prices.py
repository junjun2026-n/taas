import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from app import main as api


class ModelPriceTests(unittest.TestCase):
    def test_each_model_saves_its_own_prices(self):
        with patch.object(api,'check_permission'), patch.object(api,'query') as query:
            api.set_model_price('model-a',api.ModelPricePayload(input_price='1.5',output_price='3'),{'id':1})
            api.set_model_price('model-b',api.ModelPricePayload(input_price='10',output_price='20'),{'id':1})
        self.assertEqual(query.call_args_list[0].args[1],('model-a',Decimal('1.5'),Decimal('3')))
        self.assertEqual(query.call_args_list[1].args[1],('model-b',Decimal('10'),Decimal('20')))

    def test_settlement_uses_model_input_output_rates_and_keeps_small_costs(self):
        for rates,expected,status in [
            (('1','2'),Decimal('0.00000300'),200),
            (('10','20'),Decimal('0.00003000'),200),
            (('0','0'),Decimal(0),200),
            (('10','20'),Decimal(0),502),
        ]:
            context=MagicMock()
            cursor=context.__enter__.return_value.cursor.return_value.__enter__.return_value
            cursor.fetchone.return_value={'tokens':100}
            key=dict(id=1,user_id=2,order_id=3,reservation_id='test',task_type='chat')
            with patch.object(api,'db',return_value=context):
                api.settle_usage(key,{'id':4},'model-test',{'prompt_tokens':1,'completion_tokens':1},
                                 {'input_price':Decimal(rates[0]),'output_price':Decimal(rates[1])},datetime.now(timezone.utc),status)
            log=cursor.execute.call_args.args[1]
            self.assertEqual(log[3],'model-test')
            self.assertEqual(log[6],expected)


if __name__ == '__main__': unittest.main()
