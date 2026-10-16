import io
import json
import sqlite3
import unittest
import hashlib
import hmac
import threading
import time
import urllib.request
import http.client
import tempfile
from pathlib import Path
from http.server import HTTPServer
from unittest.mock import Mock, patch
from live import (Sender, SendRateLimit, API_URL, broadcast_restart, make_handler,
                  read_event_body, event_time_seconds, sweep_expiry)
from qbot import ALLOWED_GROUPS, TEST_GROUP, PRODUCTION_GROUP, group_reply


class TransportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.rate_path = Path(directory.name) / 'rate.sqlite3'
        self.now = 100.0
        self.limiter = SendRateLimit(self.rate_path, clock=lambda:self.now)
        self.sender = Sender('test-token', rate_limit=self.limiter)
        self.sender.opener = Mock()
        self.stop = patch('live.STOP_FILE')
        stop = self.stop.start()
        stop.exists.return_value = False
        self.addCleanup(self.stop.stop)

    def test_reject_arbitrary_actions_and_destinations_without_io(self):
        cases = [None, {}, {'action':'send_private_msg'}, {'action':'send_msg'},
                 {'action':'send_group_msg','params':{'group_id':'NOT-A-REAL-GROUP','message':[]}},
                 {'action':'send_group_msg','params':{'group_id':TEST_GROUP,'message':[], 'user_id':'fake'}}]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(PermissionError):
                self.sender.send(value)
        self.sender.opener.open.assert_not_called()

    def test_exact_four_group_policy_rejects_all_other_routes_without_io(self):
        self.assertEqual(ALLOWED_GROUPS, frozenset(('312579123','1050045834','1006808801','929618496')))
        destinations = [None, True, 0, -1, '', '312579123 ', ' 1050045834',
                        312579123.0, 1050045834.0, '312579124', '1050045835', '929618497']
        destinations += [str(value) for value in range(100000,101000)]
        for destination in destinations:
            request = {'action':'send_group_msg','params':{'group_id':destination,
                'message':[{'type':'text','data':{'text':'offline policy probe'}}]}}
            with self.subTest(destination=destination), self.assertRaises(PermissionError):
                self.sender.send(request)
        for action in ('send_private_msg','send_msg','send_group_forward_msg','send_private_forward_msg'):
            with self.subTest(action=action), self.assertRaises(PermissionError):
                self.sender.send({'action':action,'params':{'group_id':312579123}})
        self.sender.opener.open.assert_not_called()

    def test_only_fixed_url_group_and_literal_text(self):
        response = io.BytesIO(b'{"status":"ok","retcode":0}')
        self.sender.opener.open.return_value = response
        self.assertTrue(self.sender.send(group_reply(TEST_GROUP, '[CQ:at,qq=all]')))
        request = self.sender.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, API_URL)
        body = json.loads(request.data)
        self.assertEqual(body['group_id'], 312579123)
        self.assertEqual(body['message'], [{'type':'text','data':{'text':'[CQ:at,qq=all]'}}])

    def test_each_authorized_group_can_send_through_mock_transport(self):
        for group in sorted(ALLOWED_GROUPS):
            with self.subTest(group=group):
                self.now += 5
                self.sender.opener.open.return_value = io.BytesIO(b'{"status":"ok","retcode":0}')
                self.assertTrue(self.sender.send(group_reply(group, 'offline policy probe')))
                request = self.sender.opener.open.call_args.args[0]
                self.assertEqual(json.loads(request.data)['group_id'], int(group))

    def test_stop_prevents_io(self):
        with patch('live.STOP_FILE') as stop:
            stop.exists.return_value = True
            self.assertFalse(self.sender.send(group_reply(TEST_GROUP, 'x')))
        self.sender.opener.open.assert_not_called()

    def test_send_failure_not_retried(self):
        self.sender.opener.open.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.sender.send(group_reply(TEST_GROUP, 'x'))
        self.assertEqual(self.sender.opener.open.call_count, 1)
        self.now += 4.999
        self.assertFalse(self.sender.send(group_reply(PRODUCTION_GROUP, 'x')))
        self.assertEqual(self.sender.opener.open.call_count, 1)

    def test_global_limit_across_groups_and_sender_restarts(self):
        self.sender.opener.open.side_effect = lambda *a,**kw:io.BytesIO(b'{"status":"ok","retcode":0}')
        self.assertTrue(self.sender.send(group_reply(TEST_GROUP, 'first')))
        restarted = Sender('test-token', rate_limit=SendRateLimit(self.rate_path, clock=lambda:self.now))
        restarted.opener = self.sender.opener
        self.now = 104.999
        self.assertFalse(restarted.send(group_reply(PRODUCTION_GROUP, 'too soon')))
        self.assertEqual(self.sender.opener.open.call_count, 1)
        self.now = 105.0
        self.assertTrue(restarted.send(group_reply(PRODUCTION_GROUP, 'allowed')))
        self.assertEqual(self.sender.opener.open.call_count, 2)

    def test_clock_reset_starts_cooldown(self):
        self.assertTrue(self.limiter.acquire())
        self.now = 1.0
        self.assertFalse(self.limiter.acquire())
        self.now = 5.999
        self.assertFalse(self.limiter.acquire())
        self.now = 6.0
        self.assertTrue(self.limiter.acquire())

    def test_http_dispatch_never_replies_to_other_sessions(self):
        bot = Mock(self_id='test-bot')
        bot.db.total_changes = 0
        bot.handle.return_value = group_reply(TEST_GROUP, 'test')
        sender = Mock()
        token = 'local-test-secret'
        server = HTTPServer(('127.0.0.1', 0), make_handler({TEST_GROUP: bot}, sender, token))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def post(event, chunked=False):
            body = json.dumps(event).encode()
            signature = 'sha1=' + hmac.new(token.encode(), body, hashlib.sha1).hexdigest()
            if chunked:
                connection = http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2)
                try:
                    connection.request('POST','/events',body=iter([body[:7],body[7:]]),
                        headers={'x-signature':signature},encode_chunked=True)
                    response = connection.getresponse()
                    self.assertEqual(response.status,200)
                    self.assertEqual(response.read(),b'{}')
                finally:
                    connection.close()
                return
            request = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/events',
                data=body, headers={'x-signature':signature})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=2) as response:
                self.assertEqual(response.read(), b'{}')
        try:
            base = dict(post_type='message', message_type='group', group_id=TEST_GROUP,
                        self_id='test-bot', time=time.time())
            for change in ({'group_id':'NOT-A-REAL-GROUP'}, {'message_type':'private'},
                           {'post_type':'notice'}, {'self_id':'unexpected'}, {'time':0}):
                post(dict(base, **change))
            bot.handle.assert_not_called()
            sender.send.assert_not_called()
            post(base)
            sender.send.assert_called_once_with(group_reply(TEST_GROUP, 'test'), exempt=False)
            sender.send.reset_mock()
            post(base, chunked=True)
            sender.send.assert_called_once_with(group_reply(TEST_GROUP, 'test'), exempt=False)
            sender.send.reset_mock()
            bot.handle.return_value = group_reply(PRODUCTION_GROUP, 'wrong route')
            post(base, chunked=True)
            sender.send.assert_not_called()
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_chunked_body_limits_and_truncation(self):
        headers = {'Transfer-Encoding':'chunked'}
        self.assertEqual(read_event_body(headers,io.BytesIO(b'2\r\n{}\r\n0\r\n\r\n')), b'{}')
        for body in (b'40001\r\n',b'2\r\nx',b'-1\r\n',b'no\r\n',b'2\r\n{}XX',b'0\r\n\r\n'):
            with self.subTest(body=body),self.assertRaises(ValueError):
                read_event_body(headers,io.BytesIO(body))
        with self.assertRaises(ValueError):
            read_event_body(dict(headers,**{'Content-Length':'2'}),io.BytesIO(b'{}'))

    def test_seconds_milliseconds_and_numeric_string_timestamps(self):
        for value in (1800000000, 1800000000000, '1800000000', '1800000000000'):
            self.assertEqual(event_time_seconds(value),1800000000)
        for value in (None,True,float('nan'),float('inf'),'invalid',0,-1):
            self.assertIsNone(event_time_seconds(value))


class RestartNoticeTests(unittest.TestCase):
    class FakeSender:
        def __init__(self, ready_after=0):
            self.sent = []
            self.attempts = 0
            self.ready_after = ready_after

        def send(self, result):
            self.attempts += 1
            if self.attempts <= self.ready_after:
                raise RuntimeError('NapCat not ready')
            self.sent.append(result['params']['group_id'])
            return True

    def setUp(self):
        self.stop = patch('live.STOP_FILE')
        stopping = self.stop.start()
        stopping.exists.return_value = False
        self.addCleanup(self.stop.stop)
        self.sleep = patch('live.time.sleep')
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def test_notice_reaches_every_allowed_group(self):
        sender = self.FakeSender()
        broadcast_restart(sender, set(ALLOWED_GROUPS), deadline=10.0)
        self.assertEqual(set(sender.sent), {int(group) for group in ALLOWED_GROUPS})
        self.assertEqual(len(sender.sent), len(ALLOWED_GROUPS))

    def test_notice_retries_until_napcat_is_ready(self):
        sender = self.FakeSender(ready_after=2)
        broadcast_restart(sender, {TEST_GROUP}, deadline=10.0)
        self.assertEqual(sender.sent, [int(TEST_GROUP)])
        self.assertEqual(sender.attempts, 3)

    def test_notice_stops_when_the_service_is_stopped(self):
        sender = self.FakeSender(ready_after=99)
        with patch('live.STOP_FILE') as stop:
            stop.exists.return_value = True
            self.assertIsNone(broadcast_restart(sender, {TEST_GROUP}, deadline=10.0))
        self.assertEqual(sender.sent, [])

    def test_notice_gives_up_at_the_deadline(self):
        sender = self.FakeSender(ready_after=99)
        ticks = iter([0.0, 0.0, 0.0, 99.0, 99.0])
        broadcast_restart(sender, {TEST_GROUP}, deadline=5.0, clock=lambda: next(ticks, 99.0))
        self.assertEqual(sender.sent, [])


class ExpirySweepTests(unittest.TestCase):
    class FakeSender:
        def __init__(self):
            self.calls = []

        def send(self, result, *, exempt=False):
            self.calls.append((result['params']['group_id'],
                               result['params']['message'][0]['data']['text'], exempt))
            return True

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.connection = sqlite3.connect(str(Path(directory.name) / 'group.sqlite3'))
        self.addCleanup(self.connection.close)
        self.connection.execute('''CREATE TABLE whitelist (
            user_id TEXT PRIMARY KEY, until REAL NOT NULL,
            seconds INTEGER NOT NULL, granted_at REAL NOT NULL)''')
        self.connection.commit()

    def test_expired_grant_is_announced_exactly_once(self):
        self.connection.execute("INSERT INTO whitelist VALUES ('800001',100,90,10)")
        self.connection.commit()
        sender = self.FakeSender()
        connections = {TEST_GROUP: self.connection}
        self.assertEqual(sweep_expiry(sender, connections, clock=lambda: 50), [])
        self.assertEqual(sender.calls, [])
        self.assertEqual(sweep_expiry(sender, connections, clock=lambda: 101),
                         [(TEST_GROUP, '800001')])
        self.assertEqual(sender.calls, [
            (int(TEST_GROUP), '800001（QQ号），白名单资格已到期，已移出白名单。', True)])
        self.assertEqual(sweep_expiry(sender, connections, clock=lambda: 200), [])
        self.assertEqual(len(sender.calls), 1)

    def test_a_failing_send_keeps_the_row_for_the_next_tick(self):
        self.connection.execute("INSERT INTO whitelist VALUES ('800001',100,90,10)")
        self.connection.commit()

        class Broken:
            def __init__(self):
                self.seen = []

            def send(self, result, *, exempt=False):
                self.seen.append(result['params']['message'][0]['data']['text'])
                raise RuntimeError('NapCat not ready')

        sender = Broken()
        sweep_expiry(sender, {TEST_GROUP: self.connection}, clock=lambda: 101)
        self.assertEqual(len(sender.seen), 1)
        # The grant survives so the notice is retried instead of being lost.
        self.assertEqual(self.connection.execute(
            'SELECT COUNT(*) FROM whitelist').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
