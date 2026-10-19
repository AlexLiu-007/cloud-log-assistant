"""Failure paths and complete offline CLI workflows; no real cloud or IM calls."""
from contextlib import redirect_stdout
from copy import deepcopy
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
import cloud_logs
from cloud_log_common import LogError, get_profile, load_env
from cloud_log_correlation import correlate
from cloud_log_delivery import channel_settings, send_document
from cloud_log_enrichment import enrich
from cloud_log_parsing import normalize
from cloud_log_reporting import public_record, render
from cloud_log_state import begin_send, enqueue_and_claim, receipt, renew_send
from providers import azure_log_analytics as azure
from providers import volcengine_tls as tls
from test_cloud_logs import record, TOKEN, CONNECTION, START, END


def invoke(arguments):
    with patch.object(sys, 'argv', ['cloud_logs.py', *arguments]), redirect_stdout(io.StringIO()) as output:
        code = cloud_logs.main()
    return code, json.loads(output.getvalue())


class SecretAuditTests(unittest.TestCase):
    def test_header_arrays_and_name_value_pairs(self):
        token = 'vendor-token-which-is-not-sk-prefixed-123'
        for headers in ({'X-API-Key':[token]}, [{'name':'X-API-Key','value':token}]):
            with self.subTest(headers_type=type(headers).__name__):
                row = normalize({'LogMessage':{'level':'ERROR','headers':headers,'error_message':'Rejected '+token}},
                                CONNECTION, {'parser':'gateway'})
                self.assertEqual(row['authorization'], token)
                self.assertNotIn(token, json.dumps(public_record(row)))
                self.assertNotIn(token, render({'records':[row]}))

    def test_environment_secrets_are_removed_before_persistence(self):
        token = 'unknown-provider-secret-abc123'
        with tempfile.TemporaryDirectory() as directory:
            claim = enqueue_and_claim(directory, [record(error_message=token)], ['example'], extra_secrets={token})
            self.assertNotIn(token, json.dumps(claim))
            self.assertNotIn(token.encode(), (Path(directory)/'outbox.sqlite3').read_bytes())

    def test_short_authorization_and_nested_escaping(self):
        for token in ('tiny', 'vendor-token-123456789'):
            row = record(authorization=token, error_message=json.dumps(json.dumps({'headers':{'X-API-Key':[token]}})))
            text = render({'records':[row]})
            self.assertNotIn(token, text)


class ConfigurationAuditTests(unittest.TestCase):
    def config(self, directory, **updates):
        config = {'schema_version':1, 'connections':[CONNECTION], **updates}
        path = Path(directory)/'config.json'
        path.write_text(json.dumps(config), encoding='utf-8')
        return str(path)

    def test_doctor_missing_credentials_returns_failure(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            code, result = invoke(['doctor','--config', self.config(directory)])
            self.assertEqual(code, 2)
            self.assertFalse(result['ready'])
            self.assertFalse(result['network_checked'])

    def test_doctor_ready_json_webhook_without_network(self):
        env = {name:'synthetic' for name in azure.ENV.values()}
        env.update(FEISHU_WEBHOOK=json.dumps([{'webhook':'https://open.feishu.cn/open-apis/bot/v2/hook/test','secret':'signed'}]))
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, env, clear=True):
            config = self.config(directory, notifications={'test':{'provider':'feishu'}})
            code, result = invoke(['doctor','--config',config])
            self.assertEqual(code, 0)
            self.assertTrue(result['notifications']['test']['signing_enabled'])
            self.assertNotIn('signed', json.dumps(result))

    def test_invalid_profile_patterns_fail_before_query(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'profile.json'
            for pattern in ('[', 'no-capturing-group'):
                path.write_text(json.dumps({'patterns':{'request_id':pattern}}), encoding='utf-8')
                with self.assertRaises(LogError):
                    get_profile({'profile':str(path)})

    def test_multi_bot_configuration_preserves_all_targets(self):
        entries = [{'webhook':'https://open.feishu.cn/open-apis/bot/v2/hook/first'},
                   {'webhook':'https://open.feishu.cn/open-apis/bot/v2/hook/second','secret':'second-secret'}]
        with patch.dict(os.environ, {'FEISHU_WEBHOOK':json.dumps(entries)}, clear=True):
            targets, _, _ = channel_settings({'provider':'feishu'})
            self.assertEqual(len(targets),2)
            self.assertTrue(targets[1]['webhook'].endswith('/second'))
            self.assertEqual(targets[1]['secret'],'second-secret')

    def test_env_loader_preserves_process_values_and_literal_shell_text(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'AUDIT_A':'process'}, clear=True):
            path = Path(directory)/'.env'
            path.write_text('AUDIT_A=file\nAUDIT_B="$(never-execute)"\n',encoding='utf-8')
            load_env(path)
            self.assertEqual(os.environ['AUDIT_A'],'process')
            self.assertEqual(os.environ['AUDIT_B'],'$(never-execute)')


class EnrichmentAuditTests(unittest.TestCase):
    def fixtures(self):
        alert = record(marker='end', event_type='REQUEST_COMPLETED', gateway_status='400', error_message='')
        error = record(marker='upstream', event_type='VIDEO_CREATE_TIMING', error_message='asset missing',
                       upstream_status='400', upstream_code='InvalidParameter')
        start = record(marker='start', event_type='REQUEST_RECEIVED', error_message='', method='POST', path='/video')
        context = {'complete':True, 'records':[start,error,alert], 'sources':[{'connection_id':'example','complete':True,'status':'ok'}]}
        scan = {'kind':'scan','delivery_id':'lease-123','alerts':[public_record(alert)]}
        return scan, context

    def test_completes_fields_keeps_lease_and_masks_context(self):
        scan, context = self.fixtures()
        result = enrich(scan, [context])
        self.assertEqual(result['delivery_id'], scan['delivery_id'])
        self.assertEqual(result['alerts'][0]['event_id'], scan['alerts'][0]['event_id'])
        self.assertEqual(result['alerts'][0]['error_message'], 'asset missing')
        self.assertEqual(result['alerts'][0]['path'], '/video')
        self.assertEqual(result['associations'][0]['outcome'], 'failed')
        self.assertNotIn(TOKEN,json.dumps(result))
        self.assertEqual(scan['alerts'][0]['error_message'],'')

    def test_other_connection_does_not_fill_fields(self):
        scan, context = self.fixtures()
        for row in context['records']:
            row['connection_id'] = 'other'
        self.assertEqual(enrich(scan,[context])['alerts'][0]['error_message'],'')

    def test_conflicting_error_fields_are_not_guessed(self):
        scan, context = self.fixtures()
        context['records'].append(record(marker='another',error_message='different error'))
        result = enrich(scan,[context])
        self.assertEqual(result['alerts'][0]['error_message'],'')
        self.assertTrue(result['associations'][0]['warnings'])

    def test_final_success_does_not_borrow_retry_error(self):
        scan, context = self.fixtures()
        scan['alerts'][0].update(outcome='success', gateway_status='200')
        context['records'][-1].update(outcome='success', gateway_status='200')
        result = enrich(scan,[context])
        self.assertEqual(result['alerts'][0]['error_message'],'')
        self.assertEqual(result['associations'][0]['outcome'],'success')

    def test_ambiguous_requests_are_not_enriched(self):
        scan, context = self.fixtures()
        context['records'].append(record(marker='second-end',event_type='REQUEST_COMPLETED',gateway_status='500'))
        result = enrich(scan,[context])
        self.assertEqual(result['alerts'][0]['error_message'],'')
        self.assertTrue(result['associations'][0]['ambiguous'])

    def test_no_id_does_not_use_key_as_request_identity(self):
        scan, context = self.fixtures()
        scan['alerts'][0]['request_id']=''
        result = enrich(scan,[context])
        self.assertEqual(result['alerts'][0]['error_message'],'')
        self.assertEqual(result['associations'],[])

    def test_incremental_enrichment_preserves_existing_association(self):
        scan, context=self.fixtures()
        first=enrich(scan,[context])
        second=enrich(first,[{'records':[]}])
        self.assertEqual(first['associations'],second['associations'])

    def test_error_code_without_details_does_not_dump_request_summary(self):
        row=record(error_message='',upstream_code='BAD_REQUEST')
        row['message']='request body with unrelated verbose data'
        text=render({'records':[row]})
        self.assertIn('错误信息：未记录',text)
        self.assertNotIn('unrelated verbose data',text)


class StateAndDeliveryAuditTests(unittest.TestCase):
    def test_claim_cli_drains_without_cloud_credentials(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{},clear=True):
            config = Path(directory)/'config.json'
            config.write_text(json.dumps({'schema_version':1,'connections':[CONNECTION]}),encoding='utf-8')
            claim = enqueue_and_claim(directory,[record(marker=i) for i in range(3)],['example'],batch_size=1)
            self.assertEqual(claim['available_count'],2)
            receipt(directory,claim['delivery_id'],True)
            code, result = invoke(['claim','--config',str(config),'--state-dir',directory,'--batch-size','2'])
            self.assertEqual(code,0)
            self.assertEqual(len(result['alerts']),2)
            self.assertEqual(result['available_count'],0)

    def test_expired_lease_cannot_send(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('cloud_log_state.time.time',return_value=100):
                claim=enqueue_and_claim(directory,[record()],['example'],lease_seconds=30)
                begin_send(directory,claim['delivery_id'],[record()['event_id']],'dest')
            with patch('cloud_log_state.time.time',return_value=1001):
                with self.assertRaises(LogError):
                    renew_send(directory,claim['delivery_id'],1)

    def test_renewal_prevents_reclaim_during_long_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('cloud_log_state.time.time',return_value=100):
                claim=enqueue_and_claim(directory,[record()],['example'])
                begin_send(directory,claim['delivery_id'],[record()['event_id']],'dest')
            with patch('cloud_log_state.time.time',return_value=999):
                renew_send(directory,claim['delivery_id'],1)
            with patch('cloud_log_state.time.time',return_value=1001):
                self.assertFalse(enqueue_and_claim(directory,[],['example'])['alerts'])

    def test_duplicate_event_ids_cannot_send_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            claim=enqueue_and_claim(directory,[record()],['example'])
            with self.assertRaises(LogError):
                begin_send(directory,claim['delivery_id'],[record()['event_id']]*2,'dest')

    def test_incomplete_empty_results_are_visible_and_sent_as_diagnostics(self):
        document={'records':[], 'complete':False, 'sources':[{'connection_id':'example','complete':False}]}
        self.assertIn('不完整',render(document))
        config={'notifications':{'test':{'provider':'feishu'}}}
        with patch.dict(os.environ,{'FEISHU_WEBHOOK':'https://open.feishu.cn/open-apis/bot/v2/hook/test'},clear=True), patch('cloud_log_delivery.post') as post:
            self.assertEqual(send_document(config,'test',document,{})['sent_messages'],1)
            self.assertIn('不完整',json.dumps(post.call_args.args[2],ensure_ascii=False))


class ProviderAndCorrelationAuditTests(unittest.TestCase):
    def test_azure_missing_primary_table_is_not_a_healthy_empty_query(self):
        with patch.dict(os.environ,{v:'synthetic' for v in azure.ENV.values()}), patch.object(azure,'post',side_effect=[{'access_token':'test'},{'tables':[]} ]):
            with self.assertRaises(LogError):
                azure.query({},START,END,10)

    def test_tls_malformed_later_page_preserves_earlier_rows(self):
        def page(logs,more):
            return {'Logs':logs,'Analysis':False,'ResultStatus':'complete','ListOver':not more,'Context':'cursor'}
        with patch.dict(os.environ,{v:'synthetic' for v in tls.ENV.values()}), patch.object(tls,'search_logs') as service:
            service.side_effect=[page([{'message':'kept'}],True),page(None,False)]
            result=tls.query({},START,END,10)
            self.assertFalse(result['complete'])
            self.assertEqual(result['rows'],[{'message':'kept'}])

    def test_multi_cloud_failure_does_not_erase_success(self):
        adapter=SimpleNamespace(query=lambda *a,**k:{'rows':[record()['raw']], 'complete':True})
        args=SimpleNamespace(start=START.isoformat(),end=END.isoformat(),since=None,max_rows=10)
        def select(connection):
            if connection['id']=='other':
                raise LogError('unavailable')
            return adapter
        with patch('cloud_logs.adapter',side_effect=select):
            result=cloud_logs.query_all(args,{},[CONNECTION,{**CONNECTION,'id':'other'}])
        self.assertEqual(len(result['records']),1)
        self.assertEqual([s['status'] for s in result['sources']],['ok','failed'])
        self.assertFalse(result['complete'])

    def test_auth_policy_fields_and_gateway_code(self):
        row=record(event_type='REQUEST_COMPLETED',gateway_code='GW_AUTH_KEY_FORBIDDEN',gateway_status='403')
        self.assertEqual(row['error_code'],'GW_AUTH_KEY_FORBIDDEN')
        row=record(logger='auth_key_policy:check',internal_code='AUTH_KEY_BLOCKED',reason='blocked',error_message='')
        self.assertEqual(row['error_code'],'AUTH_KEY_BLOCKED')
        self.assertEqual(row['error_message'],'blocked')

    def test_heuristics_do_not_transitively_merge_distant_requests(self):
        rows=[record(marker=i,request_id='',instance_id='pod',path='/video') for i in range(3)]
        for row, stamp in zip(rows,('00:00:00','00:00:20','00:00:40')):
            row['timestamp']='2026-10-17T'+stamp+'+00:00'
        result=correlate(rows,rows[0]['event_id'],window_seconds=30)
        self.assertEqual(len(result['records']),2)
        self.assertEqual(result['mode'],'heuristic')
        self.assertEqual(result['outcome'],'unknown')


if __name__ == '__main__':
    unittest.main()
