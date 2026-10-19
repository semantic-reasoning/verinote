"""Offline HTTP fixtures; only socketpair tests exercise real framing/deadlines."""
import dataclasses
import http.client
import json
import socket
import ssl
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from verinote.agents import CLI
from verinote.config import Credentials as ServerCredentials
from verinote.openrouter import _HTTPS, HOST, PATH, OpenRouter
from verinote.transports import Credentials, ExecutionOptions
from verinote.usage import MAX_METADATA

KEY = 'sk-or-synthetic-fixture-key'


def completion(content='{"ok":true}'):
    return {'id': 'gen-test_123', 'model': 'vendor/actual-model',
            'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': content}}],
            'usage': {'prompt_tokens': 5, 'completion_tokens': 8, 'total_tokens': 13, 'cost': .01}}


class Response:
    def __init__(self, raw, status=200, encoding='identity', length=None, chunk=65536):
        self.raw, self.status, self.encoding, self.length = raw, status, encoding, length
        self.chunked, self.chunk = False, chunk
        self.offset = 0

    def getheader(self, name, default=None):
        return self.encoding if name == 'Content-Encoding' else default

    def close(self):
        pass

    def read1(self, amount):
        end = self.offset + min(amount, self.chunk)
        part = self.raw[self.offset:end]
        self.offset += len(part)
        return part


class Connection:
    def __init__(self, call, response, attempts, *, error=None):
        self.call, self.response, self.attempts, self.error = call, response, attempts, error
        self.sock = None
        self.closed = False

    def connect(self):
        self.call.remaining()

    def request(self, *args, **kwargs):
        self.call.remaining()
        self.call.sent = True
        self.attempts.append((args, kwargs))
        if self.error:
            raise self.error

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class CallFolders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.counter = 0

    def folder(self):
        self.counter += 1
        return Path(self.tmp.name).resolve()/str(self.counter)


class OpenRouterTests(CallFolders):
    def execute(self, payload=None, *, raw=None, status=200, encoding='identity', limit=1024*1024,
                length=None, error=None, prompt='prompt', model='vendor/model', timeout=2):
        if raw is None:
            raw = json.dumps(completion() if payload is None else payload).encode()
        attempts, connections, events = [], [], []
        def factory(call):
            conn = Connection(call, Response(raw, status, encoding, length, chunk=19), attempts, error=error)
            connections.append(conn)
            return conn
        cli = CLI(http_adapter=OpenRouter(), credentials=Credentials(KEY), sink=events.append,
                  max_output=limit, timeout=timeout)
        result, caught = None, None
        with tempfile.TemporaryDirectory() as tmp, patch('verinote.openrouter._HTTPS', side_effect=factory):
            folder = Path(tmp).resolve()/'call'
            try:
                result = cli.complete('openrouter', model, prompt, folder)
            except Exception as exc:
                caught = exc
            self.assertTrue((folder/'run.json').exists())
            self.assertFalse((folder/'stdout.txt').exists())
        self.assertTrue(all(conn.closed for conn in connections))
        return result, caught, attempts, events

    def assert_safe(self, error, events):
        self.assertNotIn(KEY, repr(error))
        self.assertNotIn(KEY, json.dumps([dataclasses.asdict(event) for event in events]))
        if error is not None:
            self.assertIsNone(error.__cause__)
            self.assertIsNone(error.__context__)
            self.assertFalse(hasattr(error, 'doc'))
            self.assertNotIn(KEY, repr(vars(error)))

    def test_success_fixed_request_and_memory_credentials(self):
        result, error, attempts, events = self.execute()
        self.assertEqual(result, {'ok': True}); self.assertIsNone(error)
        self.assertEqual(len(attempts), 1)
        args, kwargs = attempts[0]
        self.assertEqual(args, ('POST', PATH))
        self.assertEqual(json.loads(kwargs['body']), {'model': 'vendor/model', 'messages': [{'role': 'user', 'content': 'prompt'}], 'stream': False, 'max_tokens': 16384})
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer '+KEY)
        self.assertEqual(set(kwargs['headers']), {'Authorization', 'Content-Type', 'Accept-Encoding', 'Connection'})
        self.assertNotIn(KEY, kwargs['body'].decode())
        meta = events[0].safe_metadata
        self.assertEqual(meta['usage']['total_tokens'], 13)
        self.assertEqual(meta['requested_model'], 'vendor/model')
        self.assertEqual(meta['resolved_model'], 'vendor/actual-model')
        self.assertEqual(meta['generation_id'], 'gen-test_123')
        self.assertEqual(meta['http_status'], 200)
        self.assertEqual(meta['transmission_state'], 'received')
        self.assertTrue(events[0].response_verified)
        self.assertEqual(events[0].safe_response_payload, completion())
        self.assertEqual(Credentials.from_server(ServerCredentials(KEY)).api_key, KEY)
        self.assert_safe(error, events)

    def test_fixed_host_verified_tls(self):
        from verinote.openrouter import _Call
        call = _Call(time.monotonic()+5, time.monotonic)
        conn = _HTTPS(call)
        self.assertEqual(conn.host, HOST)
        self.assertEqual(conn._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(conn._context.check_hostname)
        conn.close()

    def test_provider_and_domain_failure_matrix_preserves_safe_usage(self):
        cases = []
        for choices in ([], [completion()['choices'][0]]*2, None, 'invalid'):
            payload = completion(); payload['choices'] = choices; cases.append((payload, 'format'))
        for content in (None, '', ' ', [], 'not JSON', '[]', 'null', '12'):
            cases.append((completion(content), 'content'))
        for finish in ('length', 'tool_calls', None, 'error'):
            payload = completion(); payload['choices'][0]['finish_reason'] = finish; cases.append((payload, 'finish'))
        for key, value, code in [('refusal', 'no', 'refusal'), ('tool_calls', [{'id': 'native'}], 'tools'), ('function_call', {'name': 'tool'}, 'tools')]:
            payload = completion(); payload['choices'][0]['message'][key] = value; cases.append((payload, code))
        payload = completion(); payload['choices'][0]['message'] = {'reasoning': '{"ok":true}'}; cases.append((payload, 'content'))
        payload = completion(); payload['error'] = {'message': 'private provider detail'}; cases.append((payload, 'provider'))
        payload = completion(); payload['error'] = {'code': 400, 'message': 'model limit exceeded'}; cases.append((payload, 'request'))
        payload = completion(); payload['choices'][0]['error'] = {'message': 'private choice detail'}; cases.append((payload, 'provider'))
        for payload, code in cases:
            with self.subTest(code=code, payload=payload):
                result, error, attempts, events = self.execute(payload)
                self.assertIsNone(result); self.assertIsInstance(error, ValueError)
                self.assertEqual(len(attempts), 1)
                self.assertEqual(events[0].safe_metadata['error_code'], code)
                self.assertEqual(events[0].safe_metadata['usage']['total_tokens'], 13)
                self.assertEqual(events[0].transport_status, 'success')
                self.assertTrue(events[0].response_verified)
                self.assertNotIn('private', str(error)); self.assert_safe(error, events)

    def test_http_errors_one_attempt_no_redirect(self):
        for status, code in [(301, 'redirect'), (307, 'redirect'), (401, 'auth'), (402, 'balance'), (400, 'request'), (422, 'request'), (429, 'rate_limit'), (500, 'server'), (503, 'server')]:
            with self.subTest(status=status):
                result, error, attempts, events = self.execute({'error': {'message': 'provider raw detail'}}, status=status)
                self.assertIsNone(result); self.assertEqual(len(attempts), 1)
                self.assertEqual(events[0].safe_metadata['error_code'], code)
                self.assertEqual(events[0].safe_metadata['http_status'], status)
                self.assertNotIn('provider raw detail', str(error))
                self.assert_safe(error, events)

    def test_untrusted_or_incomplete_payload_never_delivered(self):
        for raw, encoding, length, limit, status, code in [
            (b'x'*500, 'identity', None, 100, 200, 'size'),
            (b'x'*500, 'identity', 1, 100, 500, 'size'),
            (b'{}', 'gzip', None, 100, 200, 'encoding'),
            (b'\xff', 'identity', None, 100, 200, 'encoding'),
            (b'not JSON', 'identity', None, 100, 200, 'format'),
            (b'{"value":1e999}', 'identity', None, 100, 200, 'format'),
            (b'{"value":1,"value":2}', 'identity', None, 100, 200, 'format'),
            (b'[]', 'identity', None, 100, 200, 'format'),
            (b'{}', 'identity', 100, 100, 200, 'connection'),
            (b'{}', 'identity', 1, 100, 200, 'connection'),
            (b'['*2000+b']'*2000, 'identity', None, 10000, 200, 'format'),
        ]:
            with self.subTest(code=code, length=length):
                result, error, attempts, events = self.execute(raw=raw, encoding=encoding, length=length, limit=limit, status=status)
                self.assertIsNone(result); self.assertEqual(len(attempts), 1)
                self.assertEqual(events[0].safe_metadata['error_code'], code)
                self.assertIsNone(events[0].safe_response_payload)
                self.assertNotIn('usage', events[0].safe_metadata)
                self.assert_safe(error, events)

    def test_secrets_plain_escaped_nested_keys_and_exceptions(self):
        for status in (200, 401, 500):
            for payload in (completion(KEY), completion(json.dumps({KEY: 'value'})), completion(json.dumps({'nested': {'value': KEY}})), completion() | {KEY: 'value'}, completion() | {'error': {'message': KEY}}):
                for escaped in (False, True):
                    raw = json.dumps(payload)
                    if escaped:
                        raw = raw.replace(KEY, ''.join('\\u%04x'%ord(c) for c in KEY))
                    result, error, attempts, events = self.execute(raw=raw.encode(), status=status)
                    self.assertIsNone(result); self.assertEqual(len(attempts), 1)
                    self.assertEqual(events[0].safe_metadata['error_code'], 'secret')
                    self.assertIsNone(events[0].safe_response_payload)
                    self.assertNotIn('usage', events[0].safe_metadata)
                    self.assert_safe(error, events)
        for exception, code in [(OSError(KEY), 'connection'), (ssl.SSLError(KEY), 'tls'), (TimeoutError(KEY), 'timeout')]:
            result, error, attempts, events = self.execute(error=exception)
            self.assertEqual(len(attempts), 1)
            self.assertEqual(events[0].safe_metadata['error_code'], code)
            self.assertEqual(events[0].safe_metadata['transmission_state'], 'sent_unknown')
            self.assert_safe(error, events)

    def test_outbound_key_echo_is_blocked_before_send(self):
        for prompt, model in [(KEY, 'model'), (''.join('\\u%04x'%ord(c) for c in KEY), 'model'), ('prompt', KEY)]:
            result, error, attempts, events = self.execute(prompt=prompt, model=model)
            self.assertEqual(attempts, [])
            self.assertEqual(events[0].transport_status, 'not_started')
            self.assertEqual(events[0].safe_metadata['transmission_state'], 'not_sent')
            self.assert_safe(error, events)

    def test_official_boolean_and_nullable_usage_survives_domain_failure(self):
        payload = completion('invalid domain JSON')
        payload['usage'].update(is_byok=False, cost_details={
            'upstream_inference_cost': .01, 'upstream_inference_prompt_cost': .004,
            'upstream_inference_completions_cost': .006, 'server_tool_cost': None})
        _, error, _, events = self.execute(payload)
        self.assertIsInstance(error, ValueError)
        self.assertEqual(events[0].safe_metadata['usage'], payload['usage'])
        self.assertEqual(events[0].safe_metadata['usage']['total_tokens'], 13)
        self.assert_safe(error, events)

    def test_pinned_options_and_zero_timeout(self):
        attempts, events = [], []
        def factory(call):
            return Connection(call, Response(json.dumps(completion()).encode()), attempts)
        cli = CLI(http_adapter=OpenRouter(), credentials=Credentials(KEY), sink=events.append,
                  options=ExecutionOptions(max_tokens=321, timeout=2))
        with patch('verinote.openrouter._HTTPS', side_effect=factory):
            self.assertEqual(cli.complete('openrouter', 'model', 'prompt', self.folder(), phase='ontology'), {'ok': True})
        self.assertEqual(json.loads(attempts[0][1]['body'])['max_tokens'], 321)
        attempts.clear()
        cli = CLI(http_adapter=OpenRouter(), credentials=Credentials(KEY), sink=events.append, timeout=0)
        with patch('verinote.openrouter._HTTPS', side_effect=factory):
            with self.assertRaises(TimeoutError):
                cli.complete('openrouter', 'model', 'prompt', self.folder())
        self.assertEqual(attempts, [])
        self.assertEqual(events[-1].safe_metadata['transmission_state'], 'not_sent')

    def test_tls_failure_before_send_has_no_exception_context(self):
        events = []
        cli = CLI(http_adapter=OpenRouter(), credentials=Credentials(KEY), sink=events.append)
        with patch.object(http.client.HTTPSConnection, 'connect', side_effect=ssl.SSLCertVerificationError(KEY)):
            try:
                cli.complete('openrouter', 'model', 'prompt', self.folder())
            except ValueError as error:
                self.assert_safe(error, events)
            else:
                self.fail('TLS failure must be rejected')
        self.assertEqual(events[0].safe_metadata['transmission_state'], 'not_sent')
        self.assertEqual(events[0].safe_metadata['error_code'], 'tls')

    def test_metadata_limit_is_independent_from_response_limit(self):
        payload = completion(); payload['usage'] = {str(i): 1 for i in range(30000)}
        result, error, attempts, events = self.execute(payload)
        self.assertEqual(result, {'ok': True})
        self.assertGreater(len(json.dumps(payload['usage']).encode()), MAX_METADATA)
        self.assertLess(len(json.dumps(events[0].safe_metadata).encode()), MAX_METADATA)
        self.assertNotIn('usage', events[0].safe_metadata)
        self.assertTrue(events[0].safe_metadata['usage_unavailable'])
        self.assertEqual(events[0].safe_response_payload['usage'], payload['usage'])
        payload = completion(); payload['id'] = '../bad'; payload['usage'] = {'total_tokens': 'not verified'}
        _, _, _, events = self.execute(payload)
        self.assertNotIn('generation_id', events[0].safe_metadata)
        self.assertNotIn('usage', events[0].safe_metadata)


class RealFraming(CallFolders):
    def run_socket(self, response, *, trickle=False, timeout=1):
        client, server = socket.socketpair()
        received = []
        def serve():
            try:
                header = b''
                while b'\r\n\r\n' not in header:
                    header += server.recv(65536)
                received.append(header)
                # http.client sends a Content-Length for the body. Drain it before
                # replying so server.close() is a clean FIN: an unread body left in
                # the socketpair RX buffer makes the kernel send a RST, which the
                # client surfaces as ConnectionResetError under CI load. Draining the
                # body makes this request/response exchange deterministic.
                header_block = header.split(b'\r\n\r\n', 1)[0]
                body_read = len(header) - len(header_block) - 4
                for line in header_block.split(b'\r\n'):
                    if line.lower().startswith(b'content-length:'):
                        remaining = int(line.split(b':', 1)[1].strip()) - body_read
                        while remaining > 0:
                            chunk = server.recv(min(65536, remaining))
                            if not chunk:
                                break
                            remaining -= len(chunk)
                        break
                if trickle:
                    for byte in response:
                        server.send(bytes([byte])); time.sleep(.01)
                else:
                    server.sendall(response)
            except OSError:
                pass
            finally:
                server.close()
        thread = threading.Thread(target=serve, daemon=True); thread.start()
        def connect(conn):
            conn.sock = client
        events = []
        cli = CLI(http_adapter=OpenRouter(), credentials=Credentials(KEY), sink=events.append, timeout=timeout)
        start = time.monotonic()
        result, error = None, None
        with patch.object(http.client.HTTPSConnection, 'connect', connect):
            try:
                result = cli.complete('openrouter', 'model', 'prompt', self.folder())
            except Exception as exc:
                error = exc
        elapsed = time.monotonic()-start
        thread.join(2)
        client.close()
        return result, error, events, elapsed, received

    def test_real_framing_missing_false_lengths_and_chunked(self):
        raw = json.dumps(completion()).encode()
        variants = [b'HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n'+raw,
                    b'HTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\n'+raw,
                    b'HTTP/1.1 200 OK\r\nContent-Length: 99999\r\n\r\n'+raw,
                    b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n'+hex(len(raw))[2:].encode()+b'\r\n'+raw+b'\r\n0\r\n\r\n']
        for index, response in enumerate(variants):
            # This test asserts framing correctness, not the deadline. Use a
            # load-tolerant budget so CI CPU contention cannot turn a correct
            # socket exchange into a false timeout; deadline behavior is covered
            # separately by the .08s trickle tests below.
            result, error, events, _, received = self.run_socket(response, timeout=30)
            self.assertEqual(len(received), 1)
            if index in (0, 3):
                self.assertEqual(result, {'ok': True}); self.assertIsNone(error)
            else:
                self.assertEqual(events[0].safe_metadata['error_code'], 'connection')
                self.assertIsNone(events[0].safe_response_payload)

    def test_actual_header_and_body_trickle_cannot_extend_deadline(self):
        for response in (b'HTTP/1.1 200 OK\r\nX-Slow: '+b'x'*200,
                         b'HTTP/1.1 200 OK\r\n\r\n'+json.dumps(completion()).encode()):
            result, error, events, elapsed, received = self.run_socket(response, trickle=True, timeout=.08)
            self.assertIsNone(result); self.assertIsInstance(error, TimeoutError)
            self.assertLess(elapsed, .5)
            self.assertEqual(events[0].safe_metadata['error_code'], 'timeout')
            self.assertIsNone(events[0].safe_response_payload)
            self.assertEqual(len(received), 1)

    def test_chunk_framing_and_trailer_deadline_releases_worker(self):
        # Connection: close transfers socket ownership from connection to response.
        # read1 can internally read a whole framing line, so shutdown must reach
        # the retained socket even after connection.sock has become None.
        from verinote.openrouter import _WORKERS
        for prefix in (b'', b'0\r\nX-Trailer: '):
            client, server = socket.socketpair()
            stopped, request_seen = threading.Event(), threading.Event()
            idle_deadline = time.monotonic() + 1
            while _WORKERS._value != 8 and time.monotonic() < idle_deadline:
                time.sleep(.005)
            self.assertEqual(_WORKERS._value, 8)
            start_capacity = _WORKERS._value
            def serve():
                try:
                    raw = b''
                    while b'\r\n\r\n' not in raw:
                        raw += server.recv(65536)  # noqa: B023 (intentional: same-iteration server)
                    request_seen.set()  # noqa: B023 (intentional: same-iteration request_seen)
                    server.sendall(b'HTTP/1.1 200 OK\r\nConnection: close\r\nTransfer-Encoding: chunked\r\n\r\n'+prefix)  # noqa: B023 (intentional: same-iteration server/prefix)
                    while True:
                        server.sendall(b'1'); time.sleep(.01)  # noqa: B023 (intentional: same-iteration server)
                except OSError:
                    pass
                finally:
                    server.close(); stopped.set()  # noqa: B023 (intentional: same-iteration server/stopped)
            server_thread = threading.Thread(target=serve, daemon=True); server_thread.start()
            def connect(conn):
                conn.sock = client  # noqa: B023 (intentional: same-iteration client)
            cli = CLI(http_adapter=OpenRouter(), credentials=Credentials(KEY), timeout=.08)
            try:
                with patch.object(http.client.HTTPSConnection, 'connect', connect):
                    with self.assertRaises(TimeoutError):
                        cli.complete('openrouter', 'model', 'prompt', self.folder())
                    self.assertTrue(request_seen.is_set())
                    self.assertTrue(stopped.wait(.5))
                deadline = time.monotonic() + .5
                while _WORKERS._value != start_capacity and time.monotonic() < deadline:
                    time.sleep(.005)
                self.assertEqual(_WORKERS._value, start_capacity)
            finally:
                client.close(); server.close(); server_thread.join(.5)

    def test_blocked_dns_or_tls_completion_cannot_send_after_timeout(self):
        gate, entered, finished = threading.Event(), threading.Event(), threading.Event()
        client, server = socket.socketpair()
        server.settimeout(.2)
        def delayed_connect(conn):
            entered.set(); gate.wait(2); conn.sock = client; finished.set()
        events = []
        cli = CLI(http_adapter=OpenRouter(), credentials=Credentials(KEY), sink=events.append, timeout=.05)
        try:
            with patch.object(http.client.HTTPSConnection, 'connect', delayed_connect):
                with self.assertRaises(TimeoutError):
                    cli.complete('openrouter', 'model', 'prompt', self.folder())
                self.assertTrue(entered.is_set())
                self.assertEqual(events[0].safe_metadata['transmission_state'], 'not_sent')
                gate.set(); self.assertTrue(finished.wait(1))
                # Once connect returns the implementation's deadline guard closes
                # the socket; no authenticated request may appear afterward.
                try:
                    data = server.recv(65536)
                except socket.timeout:
                    data = b''
                self.assertEqual(data, b'')
        finally:
            gate.set(); client.close(); server.close()
