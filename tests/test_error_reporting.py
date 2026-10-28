import unittest

from app.services.error_reporting import redact_error


class ErrorReportingTests(unittest.TestCase):
    def test_binance_error_code_and_reason_remain_visible(self):
        error = Exception('binance {"code":-2015,"msg":"Invalid API-key, IP, or permissions for action."}')
        result = redact_error(error)
        self.assertIn('-2015', result)
        self.assertIn('Invalid API-key, IP, or permissions', result)

    def test_credentials_and_signed_queries_are_removed(self):
        error = Exception('binance GET https://user:pass@api.binance.com/api/v3/account?timestamp=1&signature=secret-signature API_KEY=public-key SecretKey="secret-value"')
        result = redact_error(error, ['public-key', 'secret-value'])
        for value in ('user:pass', 'secret-signature', 'public-key', 'secret-value', 'timestamp=1'):
            self.assertNotIn(value, result)
        self.assertIn('https://api.binance.com/api/v3/account', result)

    def test_json_secret_fields_and_database_urls_are_removed(self):
        error = Exception('{"signature":"unknown-signature", "password":"private-password"} postgresql://private-db')
        result = redact_error(error, ['postgresql://private-db'])
        for value in ('unknown-signature', 'private-password', 'postgresql://private-db'):
            self.assertNotIn(value, result)


if __name__ == '__main__':
    unittest.main()
