import unittest

from src.agent.task_solver import curl_api_command, default_heritage_experience


class ApiContractVariantTests(unittest.TestCase):
    def contract(self, docs):
        return default_heritage_experience(
            '请查询北京文化遗产 API http://localhost:8899', [{'content': docs}])

    def test_documented_x_api_key_and_city_are_not_overridden(self):
        item = self.contract('''
        GET /api/v2/heritage/search?city=北京
        X-API-Key: <your-api-key>
        API Key: `changed-key-2026`
        ''')
        self.assertEqual(item['authStyle'], 'X-API-Key')
        self.assertEqual(item['cityParam'], 'city')
        self.assertEqual(item['token'], 'changed-key-2026')
        command = curl_api_command({**item, 'city': '北京', 'limit': 50})
        self.assertIn('-H', command)
        self.assertIn('X-API-Key: changed-key-2026', command)
        self.assertIn('city=北京', command)

    def test_custom_header_and_parameter_are_supported(self):
        item = self.contract('''
        URL: http://localhost:8899/v2/items
        Header: X-Client-Token
        Token: `abc-token-2026`
        GET /v2/items?region=北京
        ''')
        self.assertEqual(item['authStyle'], 'X-Client-Token')
        self.assertEqual(item['cityParam'], 'region')
        self.assertEqual(item['path'], '/v2/items')
        command = curl_api_command({**item, 'city': '北京', 'limit': 20})
        self.assertIn('X-Client-Token: abc-token-2026', command)
        self.assertIn('region=北京', command)


if __name__ == '__main__':
    unittest.main()
