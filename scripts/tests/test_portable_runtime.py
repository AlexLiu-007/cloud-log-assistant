"""Host Python and native TLS HTTP tests; no SDK, network or installation needed."""
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cloud_log_common import LogError
from providers import volcengine_tls as tls


ENV = {'VOLC_ACCESS_KEY': 'synthetic-access', 'VOLC_SECRET_KEY': 'synthetic-secret',
       'VOLC_SESSION_TOKEN': ''}
PAYLOAD = {'TopicId': 'topic-test', 'Query': '警告 AND "request-id"', 'StartTime': 1700000000000,
           'EndTime': 1700000060000, 'Limit': 100, 'Sort': 'asc', 'HighLight': False, 'MustComplete': True}
FIXED_TIME = datetime(2026, 10, 19, 4, 30, tzinfo=timezone.utc)


class NativeTlsTests(unittest.TestCase):
    def test_signatures_match_vectors_from_official_sdk_1_0_228(self):
        vectors = [('', '0c990a2ca6b85b39cb8c4f1c7107d52ec0df015167c667b50195b78454ff8ff7'),
                   ('synthetic-sts-token', 'cc705c7a1f5ed1279cb557e25e1bad81151f1ea5365fb1984d2711d4aef1d348')]
        for token, expected in vectors:
            with self.subTest(sts=bool(token)), patch.dict(os.environ, {**ENV, 'VOLC_SESSION_TOKEN': token}):
                request = tls.signed_request({}, PAYLOAD, now=FIXED_TIME)
                self.assertTrue(request.get_header('Authorization').endswith('Signature=' + expected))
                self.assertEqual(request.full_url, 'https://tls-cn-shanghai.volces.com/SearchLogs')
                self.assertEqual(json.loads(request.data), PAYLOAD)
                self.assertEqual(request.get_header('X-tls-apiversion'), '0.2.0')
                self.assertEqual(request.get_header('X-security-token'), token or None)

    def test_explicit_https_port_uses_canonical_host(self):
        with patch.dict(os.environ, ENV):
            one = tls.signed_request({}, PAYLOAD, now=FIXED_TIME)
            two = tls.signed_request({'endpoint': 'https://tls-cn-shanghai.volces.com:443/'}, PAYLOAD, now=FIXED_TIME)
        self.assertEqual(one.get_header('Authorization'), two.get_header('Authorization'))

    def test_endpoint_requires_https_and_no_embedded_credentials(self):
        for endpoint in ('http://tls.example.com', 'https://user:password@tls.example.com',
                         'https://tls.example.com/path', 'https://tls.example.com?x=1',
                         'https://tls.example.com:invalid', 'tls.example.com\nInjected: true'):
            with self.subTest(endpoint=endpoint), self.assertRaises(LogError):
                tls.endpoint_url({'endpoint': endpoint})

    def test_http_request_uses_configured_timeout_and_decodes_raw_response(self):
        expected = {'Analysis': False, 'Logs': [], 'ListOver': True, 'ResultStatus': 'complete'}
        with patch.dict(os.environ, ENV), patch.object(tls, 'build_opener') as opener:
            opener.return_value.open.return_value = io.StringIO(json.dumps(expected))
            self.assertEqual(tls.search_logs({'timeout_seconds': 12}, PAYLOAD), expected)
            self.assertEqual(opener.return_value.open.call_args.kwargs['timeout'], 12)
            self.assertEqual(opener.return_value.open.call_args.args[0].get_method(), 'POST')
            self.assertIsInstance(opener.call_args.args[0], tls._NoRedirect)

    def test_http_and_network_errors_do_not_echo_sensitive_bodies(self):
        secret = 'private-response-credential'
        failures = [HTTPError('https://tls.example.com', 403, secret, {}, io.BytesIO(secret.encode())),
                    URLError(secret), TimeoutError(secret)]
        for failure in failures:
            with patch.dict(os.environ, ENV), patch.object(tls, 'build_opener') as opener:
                opener.return_value.open.side_effect = failure
                with self.assertRaises(LogError) as result:
                    tls.search_logs({}, PAYLOAD)
                self.assertNotIn(secret, str(result.exception))

    def test_invalid_json_and_nonobject_responses_fail(self):
        for body in ('not-json', '[]', 'null'):
            with patch.dict(os.environ, ENV), patch.object(tls, 'build_opener') as opener:
                opener.return_value.open.return_value = io.StringIO(body)
                with self.assertRaises(LogError):
                    tls.search_logs({}, PAYLOAD)

    def test_raw_rest_analysis_data_is_read_without_sdk_models(self):
        self.assertEqual(tls.read_batch({'Analysis': True, 'AnalysisResult': {'Data': [{'count': 2}]}}),
                         ([{'count': 2}], 'analysis'))
        with self.assertRaises(LogError):
            tls.read_batch({'Analysis': True, 'AnalysisResult': {'Data': 'invalid'}})

    def test_redirects_are_not_followed_with_authorization(self):
        self.assertIsNone(tls._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.example'))


class HostRuntimeTests(unittest.TestCase):
    def test_doctor_runs_on_base_python_without_site_packages_or_venv(self):
        root = Path(__file__).resolve().parents[2]
        config = {'schema_version': 1, 'connections': [{'id': 'tls-test', 'provider': 'volcengine_tls',
                                                      'topic_ids': ['topic-test']}]}
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'connections.json'
            file.write_text(json.dumps(config), encoding='utf-8')
            base = getattr(sys, '_base_executable', sys.executable)
            result = subprocess.run([base, '-S', '-X', 'utf8', str(root / 'scripts/cloud_logs.py'),
                                     'doctor', '--config', str(file)],
                                    env={**os.environ, **ENV}, capture_output=True, text=True,
                                    encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data['ready'])
        self.assertFalse(data['virtual_environment'])
        self.assertFalse(data['virtual_environment_required'])
        self.assertTrue(data['standard_library_only'])
        self.assertNotIn('dedicated_environment', data)


if __name__ == '__main__':
    unittest.main()
