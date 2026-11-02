import io
import subprocess
import sys
import unittest


class DiagnosticsTests(unittest.TestCase):
    def test_fixed_output_never_includes_secret_text(self):
        from scripts import recovery_diagnostics as d
        stream=io.StringIO()
        d.emit('secret-token','secret-message',counts={'quarantined':2,'secret':17},stream=stream)
        self.assertNotIn('secret',stream.getvalue())
        self.assertIn('"quarantined":2',stream.getvalue())

    def test_real_final_error_frame_then_eof_preserves_specific_code(self):
        from scripts import run_private_forwarder as core
        from scripts import recovery_diagnostics as d
        script='import sys; sys.stdout.write(\'{"kind":"native_error","code":"recovery_anchor_mismatch"}\\n\'); sys.stdout.flush()'
        capture=core.Duplex([sys.executable,'-c',script],{})
        try:
            capture.process.wait(timeout=5)
            self.assertEqual(d.exited_code(capture),'recovery_anchor_mismatch')
        finally:
            capture.close()

class WebSocketRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_six_connection_attempts_then_cooldown_without_send(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import patch
        from wechat_bridge import transport
        runner=transport.Runner(SimpleNamespace(self_id=1),transport.Endpoint('ws://127.0.0.1:6199/ws','synthetic-token-123456'))
        connections=[]; delays=[]
        class Unavailable:
            async def __aenter__(self):
                connections.append(1)
                raise ConnectionRefusedError('secret upstream response')
            async def __aexit__(self,*args):pass
        async def waiting(awaitable,timeout):
            awaitable.close()
            delays.append(timeout)
            if timeout==300:runner.stop.set()
            raise TimeoutError()
        with patch.object(transport,'connect',return_value=Unavailable()),patch.object(transport.asyncio,'wait_for',side_effect=waiting):
            await runner.run(maximum_seconds=604800)
        self.assertEqual(len(connections),6)
        self.assertEqual(delays,[1,2,4,8,16,30,300])
        self.assertFalse(runner.ready.is_set())

class SSERetryTests(unittest.TestCase):
    def test_unavailable_stream_has_six_attempt_budget_and_cooldown(self):
        import time
        from unittest.mock import patch
        from scripts import forward_sse
        pump=forward_sse.Pump('wechat-primary','x'*32,'3',time.monotonic()+1000)
        delays=[]; calls=[]
        def waiting(seconds):
            delays.append(seconds)
            if seconds==300:pump.stopped.set()
        class Connection:
            def __init__(self,*args,**kwargs):calls.append(1)
            def request(self,*args,**kwargs):raise ConnectionError('secret upstream')
            def close(self):pass
        with patch.object(forward_sse.http.client,'HTTPConnection',Connection),patch.object(pump.stopped,'wait',side_effect=waiting):
            pump.run()
        self.assertEqual(len(calls),6)
        self.assertEqual(delays,[1,2,4,8,16,30,300])
        self.assertEqual(pump.after,'3')
