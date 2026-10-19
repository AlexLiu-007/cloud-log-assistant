"""Offline regression checks. No cloud calls or real IM messages."""
import base64
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cloud_log_common import LogError, parse_time
from cloud_log_correlation import correlate
from cloud_log_delivery import cards, post, send_document
from cloud_log_parsing import normalize
from cloud_log_reporting import collect_secrets, public_record, render
from cloud_log_state import begin_send, enqueue_and_claim, receipt
from cloud_logs import query_one
from providers import azure_log_analytics as azure
from providers import volcengine_tls as tls

TOKEN = 'sk-synthetic-token-for-tests-123456'
CONNECTION = {'id': 'example', 'provider': 'azure_log_analytics'}
START, END = parse_time('2026-10-17T00:00:00Z'), parse_time('2026-10-17T00:05:00Z')


def record(**values):
    payload = {'level': 'ERROR', 'request_id': 'request-1', 'authorization': 'Bearer ' + TOKEN,
               'error_message': 'Request rejected', **values}
    return normalize({'TimeGenerated': '2026-10-17T00:01:00Z', 'LogMessage': payload},
                     CONNECTION, {'parser': 'gateway'})


class ParsingTests(unittest.TestCase):
    def test_only_info_debug_excluded_and_all_statuses_retained(self):
        rows = [{'TimeGenerated': '2026-10-17T00:01:00Z', 'LogMessage': {'level': level, 'gateway_status': status}}
                for level in ('INFO', 'debug', 'ERROR', 'warning', '')
                for status in ('200', '401', '403', '404', '429', '500')]
        adapter = SimpleNamespace(query=lambda *a, **kw: {'rows': rows, 'complete': True})
        with patch('cloud_logs.adapter', return_value=adapter):
            source, result = query_one(CONNECTION, START, END)
            self.assertEqual(len(result), 18)
            self.assertEqual({r['gateway_status'] for r in result}, {'200','401','403','404','429','500'})
            self.assertTrue(source['complete'])
            self.assertEqual(len(query_one(CONNECTION, START, END, context=True)[1]), 30)

    def test_upstream_error_supplements_older_error(self):
        result = record(error_message='', err_msg={'error': {'message': 'old message'}},
                        upstream_error={'error': {'message': 'new message', 'code': 'InvalidParameter', 'type': 'BadRequest'}})
        self.assertEqual(result['error_message'], 'old message')
        self.assertEqual(result['error_code'], 'InvalidParameter')
        self.assertEqual(result['error_type'], 'BadRequest')

    def test_gateway_prefix_and_tls_milliseconds(self):
        result = normalize({'__time__': 1792195260000, '__content__':
            '2026-10-17 08:01:00 | WARNING | api.routes.video_router:_log_video_create_timing:42 - '
            'Request ID: [req-1] [VIDEO_CREATE_TIMING] upstream_error=[{"error":{"code":"BadRequest","message":"content[2] missing"}}]'},
            CONNECTION, {'parser': 'gateway'})
        self.assertEqual(result['request_id'], 'req-1')
        self.assertEqual(result['stage'], 'upstream')
        self.assertEqual(result['error_message'], 'content[2] missing')
        self.assertEqual(result['timestamp'], '2026-10-17T00:01:00+00:00')

    def test_raw_authorization_preserved_until_publication(self):
        row = record()
        self.assertIn(TOKEN, json.dumps(row))
        public = public_record(row)
        self.assertNotIn('raw', public)
        self.assertNotIn(TOKEN, json.dumps(public))
        self.assertEqual(public, public_record(public))

    def test_nested_and_analysis_secrets_redacted(self):
        row = record(error_message=json.dumps({'nested': json.dumps({'api_key': TOKEN})}))
        analysis = {row['event_id']: {'certainty': 'confirmed', 'judgment': 'Rejected ' + TOKEN,
                                    'recommendation': 'Check permissions', 'evidence': 'INTERNAL_EVIDENCE_123'}}
        text = render({'records': [row]}, analysis)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn('INTERNAL_EVIDENCE_123', text)
        self.assertNotIn('日志证据支持', text)
        self.assertIn('分析：Rejected', text)


class CorrelationTests(unittest.TestCase):
    def events(self):
        first, last = record(marker='start'), record(marker='end')
        first['stage'], last['stage'], last['outcome'] = 'received', 'completed', 'failed'
        return [first, last]

    def test_exact_and_business_have_distinct_completeness(self):
        events = self.events()
        exact = correlate(events, events[0]['event_id'])
        self.assertEqual(exact['completeness'], 'observed_boundaries')
        self.assertEqual(exact['outcome'], 'failed')
        for item in events:
            item['business_id'] = 'job-1'
        business = correlate(events, events[0]['event_id'], field='business_id')
        self.assertEqual(business['outcome'], 'unknown')
        self.assertEqual(business['mode'], 'business')

    def test_cross_connection_requires_opt_in(self):
        events = self.events()
        events[1]['connection_id'] = 'other-cloud'
        self.assertEqual(len(correlate(events, events[0]['event_id'])['records']), 1)
        self.assertEqual(len(correlate(events, events[0]['event_id'], cross_connections=True)['records']), 2)

    def test_multiple_requests_remain_ambiguous(self):
        events = self.events()
        for i, item in enumerate(events):
            item['request_id'], item['trace_id'] = f'req-{i}', 'shared-trace'
        result = correlate(events, events[0]['event_id'], field='trace_id')
        self.assertEqual(result['outcome'], 'unknown')
        self.assertEqual(result['completeness'], 'unconfirmed')


class StateTests(unittest.TestCase):
    def test_dedup_release_old_pending_and_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            first = enqueue_and_claim(directory, [record()], ['example'])
            self.assertEqual(len(first['alerts']), 1)
            self.assertNotIn(TOKEN, json.dumps(first))
            self.assertFalse(enqueue_and_claim(directory, [record()], ['example'])['alerts'])
            receipt(directory, first['delivery_id'], False)
            retry = enqueue_and_claim(directory, [], ['example'])
            self.assertEqual(len(retry['alerts']), 1)
            self.assertEqual(receipt(directory, retry['delivery_id'], True)['affected'], 1)
            self.assertFalse(enqueue_and_claim(directory, [record()], ['example'])['alerts'])

    def test_concurrent_send_and_destination_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            claim = enqueue_and_claim(directory, [record()], ['example'])
            event_ids = [r['event_id'] for r in claim['alerts']]
            begin_send(directory, claim['delivery_id'], event_ids, 'destination-a')
            with self.assertRaises(LogError):
                begin_send(directory, claim['delivery_id'], event_ids, 'destination-a')
            receipt(directory, claim['delivery_id'], False)
            retry = enqueue_and_claim(directory, [], ['example'])
            with self.assertRaises(LogError):
                begin_send(directory, retry['delivery_id'], event_ids, 'destination-b')


class DeliveryTests(unittest.TestCase):
    CONFIG = {'notifications': {'test': {'provider': 'feishu', 'webhook_env': 'UNIT_WEBHOOK',
        'secret_env': 'UNIT_SECRET', 'keyword_env': 'UNIT_KEYWORD'}}}
    ENV = {'UNIT_WEBHOOK': 'https://open.feishu.cn/open-apis/bot/v2/hook/synthetic',
           'UNIT_SECRET': '', 'UNIT_KEYWORD': 'unit-test'}

    def test_card_sections_and_untrusted_plain_text(self):
        text = render({'records': [record(gateway_status='400', error_message='<at id=all></at> **not formatting**')]})
        payload = cards(text, 'unit-test')[0]
        self.assertEqual(payload['msg_type'], 'interactive')
        self.assertNotIn(TOKEN, json.dumps(payload))
        card_text = json.dumps(payload, ensure_ascii=False)
        for label in ('请求人：', '关联与追踪', '追踪状态：', '关联方式：', '关联依据：', '追踪完整性：', '请求结果：'):
            self.assertNotIn(label, card_text)
        self.assertNotIn('请求标识：', card_text)
        self.assertIn('响应状态：', card_text)
        self.assertIn('具体错误', card_text)
        matches = [e['text'] for e in payload['card']['elements'] if '<at' in e.get('text', {}).get('content','')]
        self.assertTrue(matches)
        self.assertEqual(matches[0]['tag'], 'plain_text')

    def test_long_unicode_cards_preserve_content(self):
        body = '中😀"\\' * 10000
        result = cards('日志告警｜测试\n\n' + body, 'test')
        parts = []
        self.assertGreater(len(result), 1)
        for payload in result:
            self.assertLess(len(json.dumps(payload, ensure_ascii=False).encode()), 18500)
            self.assertIn('test', payload['card']['header']['title']['content'])
            self.assertFalse(any(e['tag']=='note' for e in payload['card']['elements']))
            parts.extend(e['text']['content'] for e in payload['card']['elements'] if e['tag']=='div')
        self.assertEqual(''.join(parts), body)

    def test_signing_and_explicit_success(self):
        payload = cards('message', '')[0]
        with patch('cloud_log_delivery.urlopen', return_value=io.StringIO('{"code":0}')) as request, patch('cloud_log_delivery.time.time', return_value=123):
            post(self.ENV['UNIT_WEBHOOK'], 'secret', payload, 30)
            sent = json.loads(request.call_args.args[0].data)
            expected = base64.b64encode(hmac.new(b'123\nsecret', b'', hashlib.sha256).digest()).decode()
            self.assertEqual(sent['sign'], expected)
            self.assertEqual(sent['msg_type'], 'interactive')
            self.assertNotIn('sign', payload)

    def test_invalid_acknowledgements_fail(self):
        for response in ('{}', '[]', '{"code":false}', '{"code":"0"}', '{"code":19021}'):
            with self.subTest(response=response), patch('cloud_log_delivery.urlopen', return_value=io.StringIO(response)):
                with self.assertRaises(LogError):
                    post(self.ENV['UNIT_WEBHOOK'], '', cards('message', '')[0], 30)

    def test_send_failure_releases_and_success_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, self.ENV):
            document = {'kind': 'scan', **enqueue_and_claim(directory, [record()], ['example'])}
            with patch('cloud_log_delivery.post', side_effect=LogError('mock failure')):
                with self.assertRaises(LogError):
                    send_document(self.CONFIG, 'test', document, {}, directory)
            document = {'kind': 'scan', **enqueue_and_claim(directory, [], ['example'])}
            with patch('cloud_log_delivery.post') as send:
                result = send_document(self.CONFIG, 'test', document, {}, directory)
                self.assertEqual(result['acknowledged_events'], 1)
                self.assertEqual(send.call_args.args[2]['msg_type'], 'interactive')
            self.assertFalse(enqueue_and_claim(directory, [record()], ['example'])['alerts'])

    def test_empty_success_stays_quiet(self):
        with patch('cloud_log_delivery.post') as send:
            self.assertEqual(send_document(self.CONFIG, 'test', {'records': []}, {})['sent_messages'], 0)
            send.assert_not_called()


class ProviderTests(unittest.TestCase):
    def test_azure_partial_and_row_limit(self):
        payload = {'error': {'code': 'PartialError'}, 'tables': [{'name': 'PrimaryResult',
            'columns': [{'name': 'LogMessage'}], 'rows': [['one'], ['two']]}]}
        with patch.dict(os.environ, {v: 'synthetic' for v in azure.ENV.values()}), patch.object(azure, 'post', side_effect=[{'access_token':'synthetic'}, payload]):
            result = azure.query({}, START, END, 1)
        self.assertFalse(result['complete'])
        self.assertEqual(len(result['rows']), 1)

    def test_tls_raw_pagination_and_sql_analysis(self):
        def page(logs, more=False, **extra):
            return {'ResultStatus':'complete', 'Analysis':False, 'Logs':logs, 'ListOver':not more, 'Context':'next', **extra}
        with patch.dict(os.environ, {v:'synthetic' for v in tls.ENV.values()}), patch.object(tls, 'search_logs') as service:
            service.side_effect = [page([{'message':'a'}], True), page([{'message':'b'}])]
            result = tls.query({}, START, END, 10)
            self.assertEqual(len(result['rows']), 2)
            self.assertTrue(result['complete'])
            self.assertEqual(service.call_count, 2)
            self.assertEqual(service.call_args.args[1]['Context'], 'next')
            service.side_effect = [page([], Analysis=True, AnalysisResult={'Data':[{'message':'sql'}]})]
            result = tls.query({}, START, END, 10)
            self.assertEqual(result['rows'], [{'message':'sql'}])
            self.assertEqual(result['query_kind'], 'analysis')


if __name__ == '__main__':
    unittest.main()
