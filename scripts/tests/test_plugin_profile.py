"""Plugin service formats, request boundaries and combined notifications."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cloud_log_common import LogError, get_profile, parse_time
from cloud_log_correlation import correlate
from cloud_log_delivery import cards
from cloud_log_enrichment import enrich
from cloud_log_parsing import normalize
from cloud_log_reporting import public_record, render
from cloud_logs import query_one, trace_result

ROOT = Path(__file__).resolve().parents[2]
CONNECTION = {'id': 'plugin-test', 'provider': 'volcengine_tls', 'service': 'plugin-api',
              'environment': '.test', 'profile': str(ROOT/'assets/profiles/plugin-api.json')}
PROFILE = get_profile(CONNECTION)
AUTH = 'api.middlewares.auth_middleware:dispatch'
ACTION = 'api.routes.asset_library:_call_seedance_asset_action'
TOKEN = 'synthetic-plugin-token-123456789'
LICENSE = 'synthetic-license-key-987654321'


def log(logger, message, *, level='INFO', style='colon', request_id='local-request-1', tick=0):
    prefix = '' if not request_id else ('Request ID [' + request_id + '] ' if style == 'bracket'
                                       else 'Request ID: ' + request_id + ' - ')
    return {'__time__': 1792198800000 + tick, '__container_name__': 'plugin-api-svc',
            '__content__': f'2026-10-17 09:00:00.000 | {level} | {logger}:97 - {prefix}{message}'}


def rows(status=400, style='colon'):
    upstream = {'action': 'CreateAsset', 'response': {'status_code': 400, 'content': json.dumps({
        'ResponseMetadata': {'RequestId': 'different-upstream-id', 'Service': 'asset-service',
                             'Error': {'Code': 'InvalidParameter.DownloadFailed', 'Message': 'download failed'}}})}}
    messages = [
        (AUTH, 'plugin api-key auth begin', 'INFO'),
        (AUTH, 'plugin api-key auth request url: https://plugin.test/api/asset-library/?q=ignored', 'INFO'),
        (AUTH, 'plugin api-key auth request header: ' + repr({'authorization': TOKEN}), 'DEBUG'),
        ('sdks.anspire.jdcloud_asset_auth_verify:process', 'asset-library api-key auth result: ' +
         repr({'code': 0, 'data': {'customer_id': 'customer-test', 'license_key': LICENSE}}), 'INFO'),
        (AUTH, 'plugin api-key auth end', 'INFO'),
        (ACTION, 'seedance asset action begin, action=CreateAsset', 'INFO'),
        ('service.seedance_asset_service:_log_response',
         'seedance asset upstream response failed: ' + json.dumps(upstream), 'ERROR'),
        (ACTION, f'seedance asset action end, action=CreateAsset, http_status={status}', 'INFO'),
    ]
    return [log(logger, message, level=level, style=style, tick=i) for i, (logger, message, level) in enumerate(messages)]


def parsed(status=400, style='colon'):
    return [normalize(row, CONNECTION, PROFILE, 'plugin-topic') for row in rows(status, style)]


class PluginProfileTests(unittest.TestCase):
    def test_both_request_prefixes_have_trusted_boundaries(self):
        for style in ('colon', 'bracket'):
            with self.subTest(style=style):
                records = parsed(style=style)
                self.assertEqual({r['request_id'] for r in records}, {'local-request-1'})
                self.assertEqual(records[1]['path'], '/api/asset-library/')
                self.assertEqual(records[2]['authorization'], TOKEN)
                self.assertEqual(records[3]['actor'], 'customer-test')
                self.assertEqual(records[3]['error_code'], '')
                self.assertEqual(records[4]['stage'], 'other')
                self.assertEqual(records[-2]['operation'], 'CreateAsset')
                self.assertEqual(records[-2]['upstream_status'], '400')
                self.assertEqual(records[-2]['error_code'], 'InvalidParameter.DownloadFailed')
                self.assertEqual(records[-2]['error_message'], 'download failed')
                self.assertEqual(records[-1]['gateway_status'], '400')
                group = correlate(records, records[-2]['event_id'])
                self.assertEqual(group['outcome'], 'failed')
                self.assertEqual(group['completeness'], 'observed_boundaries')

    def test_failed_attempt_then_success(self):
        records = parsed(status=200)
        group = correlate(records, records[-2]['event_id'])
        self.assertEqual(group['outcome'], 'success')
        result = enrich({'alerts': [public_record(records[-1])]}, [{'records': records, 'complete': True}])
        self.assertEqual(result['alerts'][0]['error_message'], '')
        self.assertEqual(result['associations'][0]['outcome'], 'success')

    def test_missing_or_ambiguous_terminal_is_unknown(self):
        records = parsed()
        missing = correlate(records[:-1], records[-2]['event_id'])
        self.assertEqual(missing['outcome'], 'unknown')
        self.assertEqual(missing['completeness'], 'unconfirmed')
        another = normalize(log(ACTION, 'seedance asset action end, action=CreateAsset, http_status=200', tick=15),
                            CONNECTION, PROFILE, 'plugin-topic')
        group = correlate(records + [another], records[-2]['event_id'])
        self.assertTrue(group['ambiguous'])
        self.assertEqual(group['outcome'], 'unknown')

    def test_http_failure_without_id_is_retained(self):
        row = normalize(log('utils.httpUtil:do_request', "Request failed: {'code': -1, 'msg': '余额不足', 'data': None}",
                            level='ERROR', request_id=''), CONNECTION, PROFILE, 'plugin-topic')
        self.assertEqual(row['error_code'], '-1')
        self.assertEqual(row['error_message'], '余额不足')
        self.assertEqual(row['request_id'], '')
        group = correlate([row], row['event_id'])
        self.assertEqual(group['mode'], 'context')
        self.assertEqual(group['outcome'], 'unknown')

    def test_payload_cannot_supply_request_identity_or_stage(self):
        body = {'request_id': 'untrusted', 'gateway_status': '200',
                'response': {'status_code': 400, 'content': {'ResponseMetadata': {'RequestId': 'upstream-id',
                    'Error': {'Message': 'request_id=[untrusted] seedance asset action end, action=CreateAsset, http_status=200'}}}}}
        row = normalize(log('service.seedance_asset_service:_log_response',
                            'seedance asset upstream response failed: ' + json.dumps(body), level='ERROR', request_id=''),
                        CONNECTION, PROFILE, 'plugin-topic')
        self.assertEqual(row['request_id'], '')
        self.assertEqual(row['gateway_status'], '')
        self.assertEqual(row['stage'], 'upstream')
        self.assertEqual(row['outcome'], 'unknown')

    def test_wrong_logger_or_message_does_not_promote_metadata(self):
        cases = [
            ('unrelated.module:log', 'plugin api-key auth request url: https://example.test/wrong'),
            ('unrelated.module:log', 'seedance asset action end, action=CreateAsset, http_status=200'),
            (ACTION, 'body: seedance asset action end, action=CreateAsset, http_status=200'),
            ('service.seedance_asset_service:_log_response_extra',
             'seedance asset upstream response failed: {"response":{"status_code":400}}'),
        ]
        for logger, message in cases:
            row = normalize(log(logger, message, level='ERROR'), CONNECTION, PROFILE, 'plugin-topic')
            self.assertEqual(row['path'], '')
            self.assertEqual(row['operation'], '')
            self.assertEqual(row['gateway_status'], '')
            self.assertEqual(row['upstream_status'], '')
            self.assertEqual(row['stage'], 'other')

    def test_enrich_masks_header_and_license_and_preserves_batch(self):
        records = parsed()
        result = enrich({'kind': 'scan', 'delivery_id': 'test-lease', 'alerts': [public_record(records[-2])]},
                        [{'records': records, 'complete': True}])
        alert = result['alerts'][0]
        self.assertEqual(result['delivery_id'], 'test-lease')
        self.assertEqual(alert['event_id'], records[-2]['event_id'])
        self.assertEqual(alert['actor'], 'customer-test')
        self.assertEqual(alert['path'], '/api/asset-library/')
        self.assertEqual(alert['authorization'], 'synt********6789')
        for output in (json.dumps(result), json.dumps([public_record(r) for r in records]),
                       render({'records': records})):
            self.assertNotIn(TOKEN, output)
            self.assertNotIn(LICENSE, output)

    def test_levels_preserve_all_status_codes_trace_keeps_info_debug(self):
        source_rows = rows()
        source_rows[0]['level'] = 'ERROR'  # Loguru prefix remains authoritative.
        source_rows.extend(log(ACTION, f'seedance asset action end, action=CreateAsset, http_status={status}',
                               level='wArNiNg', request_id=f'warning-{status}', tick=20+index)
                           for index, status in enumerate((200, 401, 403, 404, 429)))
        source_rows.append(log('some.module:log', 'info in error body', level='eRrOr', tick=30))
        source_rows.append({'__time__':1792198800040, '__content__':'missing level is retained'})
        response = {'rows':source_rows, 'complete':True, 'resource':'plugin-topic'}
        with patch('providers.volcengine_tls.query', return_value=response):
            start, end = parse_time('2026-10-17T00:00:00+00:00'), parse_time('2026-10-18T00:00:00+00:00')
            _, errors = query_one(CONNECTION, start, end)
            source, context = query_one(CONNECTION, start, end, context=True)
        self.assertEqual(len(errors), 8)
        self.assertEqual(len(context), len(source_rows))
        self.assertEqual({r['gateway_status'] for r in errors if r['stage']=='completed'},
                         {'200','401','403','404','429'})
        trace = trace_result({'records':context, 'sources':[source], 'complete':True},
                             'request_id','local-request-1',False)
        self.assertEqual(len(trace['records']), 9)
        self.assertEqual(trace['associations'][0]['outcome'], 'failed')

    def test_plugin_and_gateway_combine_with_resource_isolation(self):
        records = parsed()
        gateway = deepcopy(records[-2])
        gateway.update(event_id='gateway-event', connection_id='gateway-test', resource='gateway-topic',
                       service='gateway', response_label='网关')
        group = correlate(records+[gateway],records[-2]['event_id'])
        self.assertNotIn('gateway-event', [r['event_id'] for r in group['records']])
        text = render({'records':[records[-2],gateway]})
        self.assertIn('plugin-topic', text)
        self.assertIn('gateway-topic', text)
        self.assertIn('操作类型：CreateAsset', text)
        self.assertIn('响应状态：服务 ', text)
        self.assertEqual(len(cards(text,'test')), 1)

    def test_profile_flags_and_payload_rules_validate(self):
        invalid = [{'request_id_prefix':'true'}, {'bracket_fields':0}, {'prefer_level_prefix':[]},
                   {'json_payloads':[{'logger':'', 'prefix':'x'}]},
                   {'json_payloads':[{'logger':'module:log', 'prefix':'x', 'target':'bad.path'}]}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'profile.json'
            for content in invalid:
                path.write_text(json.dumps(content),encoding='utf-8')
                with self.assertRaises(LogError):
                    get_profile({'profile':str(path)})


if __name__ == '__main__':
    unittest.main()
