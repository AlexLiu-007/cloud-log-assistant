"""The agent owns the model and follows script-generated tasks; all queries are mocked."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cloud_logs
from cloud_log_common import LogError, parse_time
from cloud_log_delivery import send_document
from cloud_log_enrichment import enrich
from cloud_log_parsing import normalize
from cloud_log_reporting import public_record, render
from cloud_log_workflow import plan_investigation
from test_skill_audit import invoke


CONNECTION = {"id": "gateway", "provider": "volcengine_tls", "topic_ids": ["topic-a", "topic-b"]}
ERROR_TEXT = "The request failed because the input text 'content[0]' may contain sensitive information."


def raw_events():
    prefix = "2026-10-19 01:53:54.522 | {level} | {logger}:264 - Request ID [demo-request] "
    events = [
        ("INFO", "api.middlewares.ingress.logging:emit_request_received_log", "[REQUEST_RECEIVED] method=[POST] path=[/v6/contents/generations/tasks]"),
        ("INFO", "api.routes.video_router:_log_video_create_timing", "[VIDEO_CREATE_TIMING] result=[upstream_rejected] upstream_status=[400] upstream_error=[" +
         json.dumps({"code": "InputTextSensitiveContentDetected", "type": "BadRequest", "message": ERROR_TEXT}) + "]"),
        ("WARNING", "api.middlewares.ingress.logging:emit_request_completed_log", "[REQUEST_COMPLETED] gateway_status=[400] gateway_code=[OK] request={'body': {'model': 'cdance2.5'}}"),
    ]
    return [{"__time__": 1792374834000 + index, "__content__": prefix.format(level=level, logger=logger) + message}
            for index, (level, logger, message) in enumerate(events)]


def parsed():
    return [normalize(row, CONNECTION, {"parser": "gateway"}, "topic-a") for row in raw_events()]


def sample():
    records = parsed()
    document = {"kind": "scan", "delivery_id": "demo-lease", "alerts": [public_record(records[-1])], "complete": True}
    context = {"kind": "trace", "complete": True, "selection": "all_levels", "records": records,
               "matched_identifier": {"field": "request_id", "value": "demo-request"},
               "sources": [{"connection_id": "gateway", "resource": "topic-a", "status": "ok", "complete": True,
                            "query_start": "2026-10-19T01:48:54Z", "query_end": "2026-10-19T01:58:55Z"}]}
    return document, context


class AgentWorkflowTests(unittest.TestCase):
    def test_scan_requires_trace_and_agent_commands_recover_info_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / 'gateway.json'
            profile.write_text('{"parser":"gateway"}', encoding='utf-8')
            config = root / 'connections.json'
            config.write_text(json.dumps({'schema_version': 1, 'connections': [{**CONNECTION, 'profile': str(profile)}]}), encoding='utf-8')

            def query(connection, *args, **kwargs):
                return {'rows': raw_events() if connection['_topic_id'] == 'topic-a' else [],
                        'complete': True, 'resource': connection['_topic_id']}

            with patch('providers.volcengine_tls.query', side_effect=query) as backend:
                code, document = invoke(['scan', '--config', str(config), '--state-dir', str(root / 'state'),
                                         '--start', '2026-10-19T01:50:00Z', '--end', '2026-10-19T02:00:00Z'])
                self.assertEqual(code, 0)
                self.assertEqual(backend.call_count, 2)  # One scan per configured Topic; no hidden trace.
                self.assertEqual(len(document['alerts']), 1)
                self.assertEqual(document['alerts'][0]['error_message'], '')
                task = document['investigation']['next_actions'][0]
                self.assertEqual(task['parameters']['resource'], 'topic-a')
                self.assertIn('2026-10-19T01:48:54', task['parameters']['start'])
                _, context = invoke([*task['argv'], '--config', str(config)])
                self.assertEqual(backend.call_count, 3)  # Trace only queries the affected Topic.
                self.assertEqual(len(context['records']), 3)
            result = enrich(document, [context])
            self.assertEqual(result['delivery_id'], document['delivery_id'])
            self.assertEqual(result['alerts'][0]['event_id'], document['alerts'][0]['event_id'])
            self.assertEqual(result['alerts'][0]['error_message'], ERROR_TEXT)
            self.assertEqual(result['alerts'][0]['error_code'], 'InputTextSensitiveContentDetected')
            self.assertEqual(result['alerts'][0]['upstream_status'], '400')
            self.assertEqual(result['investigation']['status'], 'ready')
            self.assertEqual(result['investigation']['next_actions'], [])
            self.assertIn(ERROR_TEXT, render(result))
            self.assertTrue(any(item['event_type'] == 'VIDEO_CREATE_TIMING'
                                for item in result['associations'][0]['error_evidence']))

    def test_plain_completion_is_not_an_error_message_or_error_group(self):
        document, _ = sample()
        row = document['alerts'][0]
        row.update(authorization='synthetic-key-123456', model='model-a')
        second = dict(row, event_id='second', request_id='different-request')
        text = render({'alerts': [row, second]})
        self.assertIn('错误信息：未记录', text)
        self.assertNotIn('[REQUEST_COMPLETED]', text)
        self.assertNotIn('合并展示：', text)
        self.assertNotIn('请求结果：', text)
        self.assertEqual(row['outcome'], 'failed')

    def test_send_cannot_skip_pending_trace_before_network_or_queue_mutation(self):
        document, _ = sample()
        with patch('cloud_log_delivery.post') as post, patch('cloud_log_delivery.begin_send') as begin:
            with self.assertRaisesRegex(LogError, 'investigation.next_actions'):
                send_document({}, 'test', document, {})
            post.assert_not_called()
            begin.assert_not_called()

    def test_empty_failed_and_partial_queries_are_attempts_not_infinite_tasks(self):
        for status, complete, expected in [('ok', True, 'complete'), ('failed', False, 'failed'), ('ok', False, 'partial')]:
            with self.subTest(status=expected):
                document, context = sample()
                context['records'] = []
                context['complete'] = complete
                context['sources'][0].update(status=status, complete=complete)
                result = enrich(document, [context])
                state = result['investigation']['events'][0]
                self.assertEqual(state['trace_status'], expected)
                self.assertEqual(state['chain_status'], 'unconfirmed')
                self.assertEqual(result['investigation']['next_actions'], [])
                self.assertEqual(result['investigation']['status'], 'limited')
                self.assertEqual(state['error_evidence_status'], 'not_found_in_scope' if complete else 'unverified')

    def test_filtered_query_cannot_establish_all_level_coverage(self):
        document, context = sample()
        context['selection'] = 'exclude_INFO_DEBUG'
        result = enrich(document, [context])
        self.assertEqual(result['investigation']['events'][0]['trace_status'], 'not_started')
        self.assertEqual(result['investigation']['pending_trace_count'], 1)
        self.assertNotEqual(result['associations'][0]['completeness'], 'observed_boundaries')
        with self.assertRaises(LogError):
            send_document({}, 'test', result, {})
        context['selection'] = 'all_levels'
        complete = enrich(result, [context])
        self.assertEqual(complete['investigation']['status'], 'ready')
        context['selection'] = 'exclude_INFO_DEBUG'
        self.assertEqual(enrich(complete, [context])['investigation']['status'], 'ready')

    def test_other_scope_or_time_cannot_satisfy_a_trace_task(self):
        for change in ('connection', 'resource', 'identifier', 'time'):
            document, context = sample()
            context['records'] = []
            if change == 'connection':
                context['sources'][0]['connection_id'] = 'other'
            elif change == 'resource':
                context['sources'][0]['resource'] = 'topic-b'
            elif change == 'identifier':
                context['matched_identifier']['value'] = 'other'
            else:
                context['sources'][0].update(query_start='2020-01-01T00:00:00Z', query_end='2020-01-02T00:00:00Z')
            self.assertEqual(enrich(document, [context])['investigation']['status'], 'needs_trace')

    def test_rows_cannot_override_mismatched_source_diagnostics(self):
        document, context = sample()
        context['sources'][0]['resource'] = 'other-topic'
        self.assertEqual(enrich(document, [context])['investigation']['status'], 'needs_trace')

    def test_incremental_conflict_removes_previously_borrowed_error(self):
        document, context = sample()
        first = enrich(document, [context])
        another = deepcopy(context['records'][1])
        another.update(event_id='another-attempt', error_message='a different cause')
        second_context = {**context, 'records': [another]}
        result = enrich(first, [second_context])
        self.assertEqual(result['alerts'][0]['error_message'], '')
        self.assertEqual(result['alerts'][0]['error_code'], '')
        self.assertEqual(len(result['associations'][0]['context_event_ids']), 4)
        self.assertTrue(result['associations'][0]['warnings'])
        self.assertEqual(result['associations'][0]['completeness'], 'observed_boundaries')
        self.assertEqual(result['investigation']['events'][0]['error_evidence_status'], 'conflicting')
        self.assertEqual(result['investigation']['status'], 'limited')

    def test_error_fields_from_different_attempts_are_not_stitched(self):
        document, context = sample()
        first = dict(context['records'][1], event_id='first-attempt', error_message='', error_type='')
        second = dict(context['records'][1], event_id='second-attempt', error_code='', error_type='')
        context['records'] = [context['records'][0], first, second, context['records'][-1]]
        result = enrich(document, [context])
        self.assertEqual(result['alerts'][0]['error_code'], '')
        self.assertEqual(result['alerts'][0]['error_message'], '')
        self.assertEqual(len(result['associations'][0]['error_evidence']), 2)

    def test_successful_retry_of_trace_clears_failed_query_state(self):
        document, context = sample()
        failed = deepcopy(context)
        failed['records'] = []
        failed['sources'][0].update(status='failed', complete=False)
        result = enrich(enrich(document, [failed]), [context])
        self.assertEqual(result['investigation']['status'], 'ready')
        context['sources'][0].update(query_start='2026-10-19T01:40:00Z', query_end='2026-10-19T02:10:00Z')
        result = enrich(enrich(document, [failed]), [context])
        self.assertEqual(result['investigation']['status'], 'ready')

    def test_retry_error_does_not_override_successful_request(self):
        document, context = sample()
        document['alerts'][0].update(gateway_status='200', outcome='success')
        context['records'][-1].update(gateway_status='200', outcome='success')
        result = enrich(document, [context])
        self.assertEqual(result['alerts'][0]['error_message'], '')
        self.assertEqual(result['associations'][0]['outcome'], 'success')
        self.assertEqual(result['investigation']['events'][0]['error_evidence_status'], 'context_available')

    def test_invalid_scope_is_blocked_and_window_configuration_validated(self):
        document, _ = sample()
        document['alerts'][0]['timestamp'] = 'invalid'
        self.assertIn('timestamp', plan_investigation(document)['events'][0]['missing'])
        for value in (0, 86401, True, '300'):
            with self.assertRaises(LogError):
                plan_investigation(document, window_seconds=value)

    def test_boundaries_do_not_make_missing_error_evidence_complete(self):
        document, context = sample()
        context['records'].pop(1)
        result = enrich(document, [context])
        self.assertEqual(result['investigation']['events'][0]['chain_status'], 'observed_boundaries')
        self.assertEqual(result['investigation']['events'][0]['error_evidence_status'], 'not_found_in_scope')
        self.assertEqual(result['investigation']['status'], 'limited')
        self.assertEqual(result['investigation']['pending_trace_count'], 0)

    def test_shared_request_is_one_task_and_keys_are_not_request_ids(self):
        document, _ = sample()
        other = dict(document['alerts'][0], event_id='another-event')
        document['alerts'].append(other)
        self.assertEqual(len(plan_investigation(document)['next_actions']), 1)
        for record in document['alerts']:
            record['request_id'] = ''
            record['authorization'] = 'same-synthetic-key'
        plan = plan_investigation(document)
        self.assertEqual(plan['next_actions'], [])
        self.assertTrue(all(item['trace_status'] == 'blocked' for item in plan['events']))

    def test_successful_request_needs_no_auto_trace_but_user_can_request_one(self):
        document, _ = sample()
        document['alerts'][0].update(gateway_status='200', outcome='success')
        self.assertEqual(plan_investigation(document)['next_actions'], [])
        self.assertEqual(len(plan_investigation(document, trace_all=True)['next_actions']), 1)

    def test_trace_resource_mismatch_does_not_query_other_topics(self):
        args = SimpleNamespace(start='2026-10-19T01:50:00Z', end='2026-10-19T02:00:00Z', since=None,
                               max_rows=None, resource='not-configured')
        with patch('providers.volcengine_tls.query') as query:
            result = cloud_logs.query_all(args, {}, [CONNECTION], term='demo-request', context=True)
            query.assert_not_called()
            self.assertFalse(result['complete'])
            self.assertEqual(result['sources'][0]['resource'], 'not-configured')
            self.assertEqual(result['sources'][0]['status'], 'failed')

    def test_trace_ignores_missing_custom_anomaly_query_file(self):
        connection = {**CONNECTION, '_topic_id': 'topic-a', 'query_file': 'does-not-exist.kql', '_base_dir': '.'}
        with patch('providers.volcengine_tls.query', return_value={'rows': [], 'resource': 'topic-a', 'complete': True}):
            source, _ = cloud_logs.query_one(connection, parse_time('2026-10-19T00:00:00Z'),
                                            parse_time('2026-10-20T00:00:00Z'), context=True)
            self.assertEqual(source['status'], 'ok')

    def test_context_evidence_and_provenance_never_store_raw_secrets(self):
        document, context = sample()
        token = 'sensitive-test-token-123456789'
        context['records'][1]['authorization'] = token
        context['records'][1]['error_message'] += ' ' + token
        result = enrich(document, [context])
        self.assertNotIn(token, json.dumps(result))
        self.assertNotIn('raw', result['associations'][0]['context_records'][1])


if __name__ == '__main__':
    unittest.main()
