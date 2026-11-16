import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from app import main as api


class KeyManagementTests(unittest.TestCase):
    def connection(self, results):
        context = MagicMock()
        cursor = context.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = results
        return context, cursor

    def test_owner_can_disable_and_reenable(self):
        for enabled in (False, True):
            context, cursor = self.connection([{'user_id':2},{'id':10,'enabled':enabled}])
            with patch.object(api,'db',return_value=context):
                result = api.set_key_status(10,api.KeyStatusPayload(enabled=enabled),{'id':2})
            self.assertEqual(result['enabled'],enabled)
            self.assertEqual(cursor.execute.call_args.args[1],(enabled,10))

    def test_other_user_requires_management_permission(self):
        context, cursor = self.connection([{'user_id':2}])
        with patch.object(api,'db',return_value=context), patch.object(api,'check_permission',side_effect=HTTPException(403,'denied')):
            with self.assertRaises(HTTPException) as error:
                api.set_key_status(10,api.KeyStatusPayload(enabled=False),{'id':3})
        self.assertEqual(error.exception.status_code,403)
        self.assertEqual(cursor.execute.call_count,1)

    def test_manager_can_delete_other_users_key(self):
        context, cursor = self.connection([{'user_id':2},None])
        with patch.object(api,'db',return_value=context), patch.object(api,'check_permission') as permission:
            api.delete_key(10,{'id':1})
        permission.assert_called_once_with({'id':1},'keys.manage')
        self.assertIn('DELETE FROM api_keys',cursor.execute.call_args.args[0])

    def test_inflight_call_prevents_deletion(self):
        context, cursor = self.connection([{'user_id':2},{'exists':1}])
        with patch.object(api,'db',return_value=context):
            with self.assertRaises(HTTPException) as error: api.delete_key(10,{'id':2})
        self.assertEqual(error.exception.status_code,409)
        self.assertFalse(any('DELETE FROM api_keys' in call.args[0] for call in cursor.execute.call_args_list))

    def test_portal_delete_remains_owner_only(self):
        context, cursor = self.connection([{'user_id':2}])
        with patch.object(api,'db',return_value=context):
            with self.assertRaises(HTTPException) as error: api.delete_portal_key(10,{'id':1})
        self.assertEqual(error.exception.status_code,404)
        self.assertEqual(cursor.execute.call_count,1)

    def test_missing_key_reports_not_found(self):
        context, _ = self.connection([None])
        with patch.object(api,'db',return_value=context):
            with self.assertRaises(HTTPException) as error: api.delete_key(10,{'id':2})
        self.assertEqual(error.exception.status_code,404)


if __name__ == '__main__': unittest.main()
