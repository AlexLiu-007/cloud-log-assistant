"""Multi-topic resources, multi-bot delivery, and client-independent workflows."""
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cloud_logs
from cloud_log_common import LogError
from cloud_log_correlation import correlate
from cloud_log_delivery import cards, channel_settings, send_document
from cloud_log_parsing import normalize
from cloud_log_reporting import render
from cloud_log_grouping import notification_groups
from cloud_log_state import enqueue_and_claim, load_authorization_fingerprints
from providers import volcengine_tls as tls
from test_cloud_logs import record, CONNECTION, START, END, TOKEN
from test_skill_audit import invoke


class MultiSourceTests(unittest.TestCase):
    def test_matching_model_key_and_error_are_grouped_but_boundaries_are_not(self):
        first = record(model='model-a', request_id='request-a')
        second = record(model='model-a', request_id='request-b')
        second['resource'] = 'another-topic'
        text = render({'records':[first, second]})
        self.assertIn('2 条异常日志', text)
        self.assertIn('another-topic', text)
        self.assertEqual(len(notification_groups({'records':[first, second]})[0]['records']), 2)
        self.assertNotIn('request-a', text)
        self.assertNotIn('request-b', text)

        for field, value in (('model', 'model-b'), ('authorization', 'Bearer another-key-987654'),
                             ('error_message', 'A different error'), ('upstream_provider', 'other'),
                             ('outcome', 'failed')):
            with self.subTest(field=field):
                left = dict(first)
                right = dict(second)
                right[field] = value
                self.assertEqual(len(notification_groups({'records':[left, right]})), 2)

    def test_two_clouds_share_one_card(self):
        rows=[record(marker='azure'), record(marker='tls')]
        rows[0].update(connection_id='azure-ai',resource='workspace-a',environment='.ai')
        rows[1].update(connection_id='volc-cn',provider='volcengine_tls',resource='topic-b',environment='.cn')
        result=cards(render({'records':rows}),'test')
        self.assertEqual(len(result),1)
        payload=json.dumps(result,ensure_ascii=False)
        self.assertIn('azure-ai / workspace-a',payload)
        self.assertIn('volc-cn / topic-b',payload)
        self.assertNotIn(TOKEN,payload)
        elements = result[0]['card']['elements']
        titles = [e['text'] for e in elements if e.get('text',{}).get('content','').startswith('**日志告警｜')]
        self.assertEqual(len(titles), 2)
        self.assertTrue(all(t['tag']=='lark_md' and t['content'].endswith('**') for t in titles))
        self.assertEqual(sum(e['tag']=='hr' for e in elements), 1)
        self.assertFalse(any(e['tag']=='note' for e in elements))
        self.assertIn('test', result[0]['card']['header']['title']['content'])

    def test_large_batch_splits_only_for_capacity(self):
        rows=[record(marker=i, error_message=f'Distinct error {i}') for i in range(20)]
        result=cards(render({'records':rows}),'test')
        self.assertGreater(len(result),1)
        self.assertLess(len(result),len(rows))
        payload=json.dumps(result,ensure_ascii=False)
        for row in rows:
            self.assertIn(row['error_message'],payload)
        for card in result:
            self.assertLess(len(json.dumps(card,ensure_ascii=False).encode()),18500)
            self.assertLessEqual(len(card['card']['elements']),40)

    def test_topic_array_and_legacy_environment_are_supported(self):
        with patch.dict(os.environ,{'VOLC_LOG_TOPIC_ID':'legacy'},clear=True):
            self.assertEqual(tls.configured_topics({}),['legacy'])
            self.assertEqual(tls.configured_topics({'topic_ids':['a','b']}),['a','b'])
        with patch.dict(os.environ,{'VOLC_LOG_TOPIC_ID':'["a","b"]'},clear=True):
            self.assertEqual(tls.configured_topics({}),['a','b'])
        with patch.dict(os.environ,{'TOPICS':'["c","d"]'},clear=True):
            self.assertEqual(tls.configured_topics({'topic_ids_env':'TOPICS'}),['c','d'])

    def test_invalid_topic_arrays_fail(self):
        for value in ([],['same','same'],[''],[1], 'not-an-array'):
            with self.subTest(value=value), self.assertRaises(LogError):
                tls.configured_topics({'topic_ids':value})

    def test_topics_query_independently_and_preserve_resource(self):
        connection={'id':'volc','provider':'volcengine_tls','topic_ids':['good','bad']}
        args=SimpleNamespace(start=START.isoformat(),end=END.isoformat(),since=None,max_rows=100)
        def query(connection,*a,**k):
            resource=connection['_topic_id']
            if resource=='bad':
                return {'connection_id':'volc','resource':resource,'complete':False,'status':'failed'},[]
            row=record()
            row.update(connection_id='volc',resource=resource)
            return {'connection_id':'volc','resource':resource,'complete':True,'status':'ok'},[row]
        with patch('cloud_logs.query_one',side_effect=query) as query_mock:
            result=cloud_logs.query_all(args,{},[connection])
        self.assertEqual(query_mock.call_count,2)
        self.assertEqual(len(result['records']),1)
        self.assertFalse(result['complete'])
        self.assertEqual({s['resource'] for s in result['sources']},{'good','bad'})
        self.assertNotIn('_topic_id',connection)

    def test_identical_ids_in_different_topics_do_not_merge(self):
        rows=[record(marker=1),record(marker=2)]
        rows[0]['resource']='a'
        rows[1]['resource']='b'
        self.assertEqual(len(correlate(rows,rows[0]['event_id'])['records']),1)
        self.assertEqual(len(correlate(rows,rows[0]['event_id'],cross_connections=True)['records']),2)

    def test_failed_topic_is_named_in_diagnostic_card(self):
        text=render({'records':[],'sources':[{'provider':'volcengine_tls','connection_id':'volc',
            'resource':'failed-topic','status':'failed','complete':False,'error':'unavailable'}]})
        self.assertIn('volcengine_tls / volc / failed-topic',text)


class MultiBotTests(unittest.TestCase):
    CONFIG={'notifications':{'test':{'provider':'feishu'}}}
    BOTS=[{'webhook':'https://open.feishu.cn/open-apis/bot/v2/hook/first','secret':'one'},
          {'webhook':'https://open.feishu.cn/open-apis/bot/v2/hook/second','secret':'two'}]

    def test_send_groups_display_but_keeps_each_event_and_receipt(self):
        rows = [record(model='model-a', request_id='request-a'),
                record(model='model-a', request_id='request-b')]
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {'FEISHU_WEBHOOK':self.BOTS[0]['webhook']}, clear=True), \
                patch('cloud_log_delivery.post') as post:
            document = {'kind':'scan', **enqueue_and_claim(directory, rows, ['example'])}
            event_ids = [row['event_id'] for row in document['alerts']]
            fingerprints = load_authorization_fingerprints(directory, event_ids)
            self.assertEqual(len(set(fingerprints.values())), 1)
            sent = send_document(self.CONFIG, 'test', document, {}, directory)

            payload_text = json.dumps(post.call_args.args[2], ensure_ascii=False)
            self.assertIn('2 条异常日志', payload_text)
            self.assertNotIn(TOKEN, payload_text)
            self.assertEqual(sent['acknowledged_events'], 2)

            database = sqlite3.connect(Path(directory) / 'outbox.sqlite3')
            try:
                persisted = database.execute('SELECT event_id, payload FROM events').fetchall()
                receipts = database.execute('SELECT event_id FROM target_receipts').fetchall()
            finally:
                database.close()
            self.assertEqual({event_id for event_id, _ in persisted}, set(event_ids))
            self.assertNotIn(TOKEN, json.dumps([payload for _, payload in persisted]))
            self.assertEqual({event_id for (event_id,) in receipts}, set(event_ids))

    def test_every_configured_bot_receives_same_combined_card(self):
        document={'records':[record(marker=1),record(marker=2)]}
        with patch.dict(os.environ,{'FEISHU_WEBHOOK':json.dumps(self.BOTS)},clear=True), patch('cloud_log_delivery.post') as post:
            result=send_document(self.CONFIG,'test',document,{})
        self.assertEqual(post.call_count,2)
        self.assertEqual(result['sent_messages'],2)
        self.assertEqual(post.call_args_list[0].args[2],post.call_args_list[1].args[2])
        self.assertEqual([call.args[1] for call in post.call_args_list],['one','two'])

    def test_partial_target_failure_does_not_repeat_successful_bot(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{'FEISHU_WEBHOOK':json.dumps(self.BOTS)},clear=True):
            document={'kind':'scan',**enqueue_and_claim(directory,[record()],['example'])}
            def fail_first(webhook,*a):
                if webhook.endswith('/first'):
                    raise LogError('mock failure')
            with patch('cloud_log_delivery.post',side_effect=fail_first) as post:
                with self.assertRaises(LogError):
                    send_document(self.CONFIG,'test',document,{},directory)
                self.assertEqual(post.call_count,2)
            retry={'kind':'scan',**enqueue_and_claim(directory,[],['example'])}
            with patch('cloud_log_delivery.post') as post:
                result=send_document(self.CONFIG,'test',retry,{},directory)
                self.assertEqual(post.call_count,1)
                self.assertTrue(post.call_args.args[0].endswith('/first'))
                self.assertEqual(result['acknowledged_events'],1)
                self.assertEqual(result['targets'][1]['status'],'already_delivered')

    def test_adding_bot_does_not_replay_globally_acknowledged_events(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{'FEISHU_WEBHOOK':json.dumps(self.BOTS[:1])},clear=True), patch('cloud_log_delivery.post') as post:
            document={'kind':'scan',**enqueue_and_claim(directory,[record(marker=1)],['example'])}
            send_document(self.CONFIG,'test',document,{},directory)
            os.environ['FEISHU_WEBHOOK']=json.dumps(self.BOTS)
            document={'kind':'scan',**enqueue_and_claim(directory,[record(marker=1),record(marker=2)],['example'])}
            self.assertEqual(len(document['alerts']),1)
            result=send_document(self.CONFIG,'test',document,{},directory)
            self.assertEqual(result['sent_messages'],2)
            self.assertEqual(post.call_count,3)

    def test_empty_duplicate_and_invalid_webhooks_fail(self):
        for entries in ([],[self.BOTS[0],self.BOTS[0]],[{'webhook':'http://bad.example/test'}]):
            with patch.dict(os.environ,{'FEISHU_WEBHOOK':json.dumps(entries)},clear=True), self.assertRaises(LogError):
                channel_settings({'provider':'feishu'})

    def test_webhook_json_credentials_are_redacted_from_unlabeled_errors(self):
        with patch.dict(os.environ,{'FEISHU_WEBHOOK':json.dumps(self.BOTS)},clear=True), tempfile.TemporaryDirectory() as directory:
            secrets=cloud_logs.runtime_secrets(self.CONFIG)
            message='callback failed at '+self.BOTS[0]['webhook']
            claim=enqueue_and_claim(directory,[record(error_message=message)],['example'],extra_secrets=secrets)
            self.assertNotIn(self.BOTS[0]['webhook'],json.dumps(claim))


class GenericWorkflowTests(unittest.TestCase):
    def test_non_gateway_service_profile(self):
        profile={'parser':'generic','level_map':{'17':'ERROR'},'fields':{
            'level':['severityNumber'],'request_id':['context.requestId'],'actor':['context.userId']}}
        row=normalize({'TimeGenerated':'2026-10-17T00:01:00Z','LogMessage':{
            'severityNumber':17,'context':{'requestId':'job-request-1','userId':'alice'},
            'error':{'message':'database timed out'}}}, {**CONNECTION,'service':'orders'},profile)
        self.assertEqual((row['level'],row['request_id'],row['actor']),('ERROR','job-request-1','alice'))
        self.assertEqual(row['error_message'],'database timed out')
        self.assertIn('orders',render({'records':[row]}))

    def test_offline_normalize_enrich_render_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            config=root/'config.json'
            config.write_text(json.dumps({'schema_version':1,'connections':[CONNECTION]}),encoding='utf-8')
            raw=root/'raw.json'
            raw.write_text(json.dumps([record()['raw']]),encoding='utf-8')
            code, normalized=invoke(['normalize','--config',str(config),'--input',str(raw)])
            self.assertEqual(code,0)
            source=root/'normalized.json'
            source.write_text(json.dumps(normalized),encoding='utf-8')
            target=root/'enriched.json'
            with patch.object(cloud_logs.sys,'argv',['cloud_logs.py','enrich','--input',str(source),'--context',str(source),'--output',str(target)]):
                self.assertEqual(cloud_logs.main(),0)
            preview=root/'preview.txt'
            with patch.object(cloud_logs.sys,'argv',['cloud_logs.py','render','--config',str(config),'--input',str(target),'--secrets-input',str(source),'--output',str(preview)]):
                self.assertEqual(cloud_logs.main(),0)
            self.assertNotIn(TOKEN,preview.read_text(encoding='utf-8'))


if __name__=='__main__':
    unittest.main()
