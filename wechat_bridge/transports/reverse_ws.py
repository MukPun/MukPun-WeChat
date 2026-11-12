"""Authenticated reverse WebSocket I/O; no protocol, storage or SDK owner."""
import asyncio
import json
import logging
import math
import time
from websockets.asyncio.client import connect

MAX_ACTION_BYTES = 16 * 1024 * 1024


class Runner:
    def __init__(self, bridge, endpoint, *, event_source=None, on_reply=None,
                 connector=connect, clock=time.monotonic):
        self.bridge, self.endpoint = bridge, endpoint
        self.event_source = event_source
        self.connector, self.clock = connector, clock
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
                source = self.event_source if self.event_source is not None else self.bridge.backend
                message = await source.next_message()
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
                handling = self.bridge.handle_frame(raw)
                raw = None
                request, answer, rejected = await handling
                self.decode_rejections += int(rejected)
                await socket.send(json.dumps(answer, ensure_ascii=False, allow_nan=False))
                if self.on_reply is not None:
                    self.bridge.reply_sent(request, answer, self.on_reply)

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
        deadline, attempt = self.clock() + maximum_seconds, 0
        delays = (1, 2, 4, 8, 16, 30)
        while not self.stop.is_set() and self.clock() < deadline:
            remaining = deadline - self.clock()
            try:
                async with asyncio.timeout(remaining):
                    async with self.connector(self.endpoint.url, additional_headers={
                        'Authorization': 'Bearer ' + self.endpoint.token,
                        'X-Self-ID': str(self.bridge.self_id), 'X-Client-Role': 'Universal'},
                        proxy=None, compression=None, open_timeout=3, close_timeout=1,
                        ping_interval=20, ping_timeout=20, max_size=MAX_ACTION_BYTES,
                        max_queue=1, write_limit=32768, logger=self.logger) as socket:
                        attempt = 0
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
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                self.failures += 1  # Never log upstream exceptions or headers.
            if not self.stop.is_set() and self.clock() < deadline:
                delay = delays[attempt]
                attempt += 1
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=min(delay, max(.001, deadline-self.clock())))
                except TimeoutError:
                    pass
                if attempt == len(delays) and not self.stop.is_set():
                    try:
                        await asyncio.wait_for(self.stop.wait(), timeout=min(300,max(.001,deadline-self.clock())))
                    except TimeoutError:
                        pass
                    attempt = 0
