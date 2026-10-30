import asyncio
import base64
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from wechat_bridge import image_input as images
from wechat_bridge.image_input_files import ImageFiles
from wechat_bridge.image_input_wire import Producer
from wechat_bridge.durable import Spool
from wechat_bridge.forward_gateway import Gateway
from wechat_bridge.continuous import PrivateBridge
from wechat_bridge.protocol import Config
from wechat_bridge.transport import Runner, Endpoint

PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5P8AAAAASUVORK5CYII=')

class GatewayHD(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name);self.root.chmod(0o700)
        self.files=self.root/'files';self.files.mkdir(mode=0o700)
        self.state=Spool(self.root/'bridge.sqlite');self.addCleanup(self.state.close)
        self.sent=[];self.storage=ImageFiles(self.files)
        self.gateway=Gateway('wechat-primary','peer',self.state,'generation',self.sent.append,
                             image_input=True,image_files=self.storage)

    def event(self,size=300000,body=''):
        raw=PNG+b'x'*(size-len(PNG))
        return {'kind':'event','cursor':'1','data':dict(account='wechat-primary',id='source',room_id='peer',sender_id='peer',
            body=body,timestamp=int(time.time()),direction='incoming',origin='live',type=1 if body else 3,
            image=images.payload(raw,'image/png','original'))}

    def transfer(self,event):
        producer=Producer(event,'f'*32);self.gateway.feed(producer.begin())
        while not producer.done:
            ack=self.sent.pop();self.gateway.feed(producer.ack(ack))

    async def test_25mib_file_event_reaches_actual_cqhttp_with_bounded_ws(self):
        from aiocqhttp import CQHttp
        import socket
        bot=CQHttp('synthetic-hd',access_token='synthetic-hd-token')
        received=[];arrived=asyncio.Event()
        @bot.on_message
        async def on_message(event):received.append(event);arrived.set()
        probe=socket.socket();probe.bind(('127.0.0.1',0));port=probe.getsockname()[1];probe.close()
        shutdown=asyncio.Event()
        serving=asyncio.create_task(bot.run_task(host='127.0.0.1',port=port,shutdown_trigger=shutdown.wait))
        event=self.event(25*1024*1024);expected=event['data']['image']['sha256']
        self.transfer(event)
        row=self.state.next_pending();self.assertNotIn('base64',row[1]['image'])
        self.assertEqual(self.state.cursor('generation'),'1')
        self.assertEqual(self.sent[-1],{'kind':'ack','cursor':'1'})
        core=PrivateBridge(Config('wechat-primary','self',None,'peer',send_interval=5),'peer',self.state,self.gateway,image_input=True)
        runner=Runner(core,Endpoint(f'ws://127.0.0.1:{port}/ws','synthetic-hd-token'))
        running=asyncio.create_task(runner.run(maximum_seconds=8))
        try:
            await asyncio.wait_for(arrived.wait(),7)
            self.assertEqual(len(received),1)
            self.assertLess(len(json.dumps(dict(received[0]))),4096)
            path=received[0]['message'][0]['data']['file']
            self.assertEqual(path,images.EXPORT_URI+expected+'.png')
            # AstrBot's Image uses file[8:], matching its own fromFileSystem format.
            self.assertEqual(path[8:],'/AstrBot/data/temp/wechat-hd-input/'+expected+'.png')
            staged=self.files/(expected+'.png')
            self.assertEqual(staged.stat().st_size,25*1024*1024)
            self.assertEqual(hashlib.sha256(staged.read_bytes()).hexdigest(),expected)
        finally:
            runner.stop.set();running.cancel();shutdown.set()
            await asyncio.gather(running,serving,return_exceptions=True)

    async def test_file_capacity_failure_keeps_question_and_text_works(self):
        with patch.object(self.storage,'stage',side_effect=images.ImageError('image_capacity')):
            self.transfer(self.event(body='Please explain this image'))
        data=self.state.next_pending()[1]
        self.assertNotIn('image',data);self.assertEqual(data['body'],'Please explain this image')
        self.assertEqual(self.sent[-1],{'kind':'ack','cursor':'1'})

    async def test_wire_cannot_inject_an_existing_file_descriptor(self):
        event=self.event();event['data']['image']=self.storage.stage(event['data']['image'])
        with self.assertRaises(images.ImageError):self.gateway.feed(event)
        self.assertIsNone(self.state.next_pending())


if __name__=='__main__':unittest.main()
