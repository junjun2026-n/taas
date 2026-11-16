import unittest
from unittest.mock import Mock, patch

from app import main as api


class ChannelDetailsTests(unittest.TestCase):
    def channel(self, address='https://api.deepseek.com/v1'):
        return dict(id=7, name='test', base_url=address, api_key='private-test-secret',
                    model_scope='model-a', enabled=True, weight=1, created_at=None)

    def test_balance_and_usage_do_not_expose_secret(self):
        response = Mock(status_code=200)
        response.json.return_value = {'is_available': True, 'balance_infos': [
            {'currency': 'CNY', 'total_balance': '12.34', 'granted_balance': '0',
             'topped_up_balance': '12.34', 'unexpected_secret': 'hidden'}]}
        with patch.object(api, 'check_permission'), \
             patch.object(api, 'query', side_effect=[self.channel(), {'requests': 3}, []]), \
             patch.object(api.httpx, 'get', return_value=response) as fetch:
            result = api.channel_details(7, {'id': 1})
        self.assertEqual(result['upstream']['status'], 'ok')
        self.assertEqual(result['upstream']['balances'][0]['total_balance'], '12.34')
        self.assertEqual(result['stats']['requests'], 3)
        self.assertNotIn('private-test-secret', str(result))
        self.assertNotIn('unexpected_secret', str(result))
        self.assertEqual(fetch.call_args.args[0], 'https://api.deepseek.com/user/balance')

    def test_unknown_provider_does_not_receive_balance_probe(self):
        with patch.object(api, 'check_permission'), \
             patch.object(api, 'query', side_effect=[self.channel('https://other.example/v1'), {}, []]), \
             patch.object(api.httpx, 'get') as fetch:
            result = api.channel_details(7, {'id': 1})
        self.assertEqual(result['upstream']['status'], 'unsupported')
        fetch.assert_not_called()

    def test_permission_checked_before_reading_channel(self):
        with patch.object(api, 'check_permission', side_effect=api.HTTPException(403, 'forbidden')), \
             patch.object(api, 'query') as query:
            with self.assertRaises(api.HTTPException): api.channel_details(7, {'id': 1})
        query.assert_not_called()

    def test_balance_failure_preserves_local_statistics(self):
        with patch.object(api, 'check_permission'), \
             patch.object(api, 'query', side_effect=[self.channel(), {'requests': 2}, []]), \
             patch.object(api.httpx, 'get', side_effect=api.httpx.ReadTimeout('timeout')):
            result = api.channel_details(7, {'id': 1})
        self.assertEqual(result['stats']['requests'], 2)
        self.assertEqual(result['upstream']['status'], 'error')


if __name__ == '__main__': unittest.main()
