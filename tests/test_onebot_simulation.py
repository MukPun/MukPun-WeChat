"""Actual aiocqhttp/Hypercorn endpoint, synthetic messages, loopback only."""
import asyncio
import unittest

from wechat_bridge.simulation import simulate


class InteroperabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_aiocqhttp_reverse_websocket(self):
        async with asyncio.timeout(15):
            checks = await simulate()
        self.assertTrue(checks['ok'], checks)
        self.assertEqual(len(checks), 13)


if __name__ == '__main__':
    unittest.main()
