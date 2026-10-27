"""Authenticated reverse WebSocket client; loopback endpoint only."""
import asyncio
from dataclasses import dataclass, field
import json
import logging
import math
import time
from urllib.parse import urlsplit

from websockets.asyncio.client import connect

from .protocol import MAX_JSON, DecodeError, decode
from .protocol import decode as _protocol_decode


# OneBot actions may contain base64 media. This budget is independent of the
# 256KiB Native IPC limit; no image bytes are sent to IPC until validated there.
MAX_ACTION_BYTES = 4 * 1024 * 1024


def decode_action(raw):
    return _protocol_decode(raw, maximum=MAX_ACTION_BYTES)


@dataclass(frozen=True)
class Endpoint:
    url: str
    token: str = field(repr=False)

    def __post_init__(self):
        parsed = urlsplit(self.url)
        if (parsed.scheme != 'ws' or parsed.hostname != '127.0.0.1' or parsed.username or parsed.password
                or parsed.path not in {'/ws', '/ws/'} or parsed.query or parsed.fragment
                or parsed.port is None or not 1 <= parsed.port <= 65535
                or not isinstance(self.token, str) or not 16 <= len(self.token) <= 1024
                or any(not 33 <= ord(c) <= 126 for c in self.token)):
            raise ValueError('invalid_endpoint')


class Runner:
    def __init__(self, bridge, endpoint, *, on_reply=None):
        self.bridge, self.endpoint = bridge, endpoint
        self.stop = asyncio.Event()
        self.ready = asyncio.Event()
        self.connections = self.failures = self.forwarded = 0
        self.decode_rejections = 0
        self.socket = None
        self.on_reply = on_reply
        self.logger = logging.getLogger('wechat_bridge.websocket')
        self.logger.addHandler(logging.NullHandler())
        self.logger.propagate = False
        self.logger.setLevel(logging.CRITICAL)

    async def _session(self, socket):
        self.bridge.connected()
        self.socket = socket
        self.connections += 1
        tasks = []

        async def incoming():
            while True:
                message = await self.bridge.backend.next_message()
                event = self.bridge.ingest(message)
                if event is not None:
                    # Ingestion is consumed before network I/O. Broken links
                    # never replay this question into the model automatically.
                    delivered = False
                    try:
                        await socket.send(json.dumps(event, ensure_ascii=False))
                        delivered = True
                    finally:
                        callback = getattr(self.bridge, 'event_delivered', None)
                        if callback is not None:
                            await callback(message, delivered)
                    self.forwarded += 1

        async def actions():
            async for raw in socket:
                request = None
                try:
                    request = decode_action(raw)
                except DecodeError as error:
                    self.decode_rejections += 1
                    answer = self.bridge.result(1400, message='invalid_json')
                    if error.has_echo:
                        answer['echo'] = error.echo
                except (ValueError, TypeError, UnicodeError, RecursionError):
                    self.decode_rejections += 1
                    answer = self.bridge.result(1400, message='invalid_json')
                else:
                    answer = await self.bridge.action(request)
                await socket.send(json.dumps(answer, ensure_ascii=False, allow_nan=False))
                if (self.on_reply is not None and isinstance(request,dict)
                        and request.get('action') in {'send_group_msg','send_private_msg','send_msg'}):
                    self.on_reply(answer)

        async def heartbeat():
            await socket.send(json.dumps(self.bridge.meta('heartbeat')))
            while True:
                await asyncio.sleep(15)
                await socket.send(json.dumps(self.bridge.meta('heartbeat')))

        try:
            await socket.send(json.dumps(self.bridge.meta('lifecycle')))
            self.ready.set()
            tasks = [asyncio.create_task(worker()) for worker in (incoming, actions, heartbeat)]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.ready.clear()
            self.socket = None
            self.bridge.disconnected()

    async def run(self, *, maximum_seconds=180):
        if type(maximum_seconds) not in (int, float) or not math.isfinite(maximum_seconds) or not 0 < maximum_seconds <= 604800:
            raise ValueError('invalid_duration')
        deadline, delay = time.monotonic() + maximum_seconds, .2
        while not self.stop.is_set() and time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                async with asyncio.timeout(remaining):
                    async with connect(self.endpoint.url, additional_headers={
                        'Authorization': 'Bearer ' + self.endpoint.token,
                        'X-Self-ID': str(self.bridge.self_id), 'X-Client-Role': 'Universal'},
                        proxy=None, compression=None, open_timeout=3, close_timeout=1,
                        ping_interval=20, ping_timeout=20, max_size=MAX_ACTION_BYTES,
                        max_queue=1, write_limit=32768, logger=self.logger) as socket:
                        running = asyncio.create_task(self._session(socket))
                        stopping = asyncio.create_task(self.stop.wait())
                        try:
                            done, _ = await asyncio.wait([running, stopping], return_when=asyncio.FIRST_COMPLETED)
                            if running in done:
                                running.result()
                        finally:
                            running.cancel()
                            stopping.cancel()
                            await asyncio.gather(running, stopping, return_exceptions=True)
                delay = .2
            except asyncio.CancelledError:
                raise
            except Exception:
                self.failures += 1  # Never log upstream exceptions or headers.
            if not self.stop.is_set() and time.monotonic() < deadline:
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=min(delay, max(.001, deadline-time.monotonic())))
                except TimeoutError:
                    pass
                delay = min(delay * 2, 5)
