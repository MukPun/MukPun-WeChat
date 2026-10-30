import base64
from copy import deepcopy
import hashlib
import unittest
from wechat_bridge import image_input as images
from wechat_bridge import image_input_wire as transfer
from wechat_bridge.forward_wire import encode, MAX_FRAME

PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5P8AAAAASUVORK5CYII=')

def event(size=300000):
    raw=PNG+b'x'*(size-len(PNG))
    return {'kind':'event','cursor':'123','anchor':'a'*64,'data':{'account':'wechat-primary','id':'source',
        'room_id':'peer','sender_id':'peer','body':'','timestamp':1,'origin':'live','direction':'incoming','type':3,
        'image':images.payload(raw,'image/png','original')}}


class InputWire(unittest.TestCase):
    def test_full_25mib_roundtrip_uses_bounded_acknowledged_frames(self):
        original=event(25*1024*1024)
        producer=transfer.Producer(original, '1'*32)
        ack=[]; received=[]
        receiver=transfer.Receiver(ack.append)
        relay=transfer.Relay()
        class Peer:
            def __init__(self, fn):self.fn=fn
            def send(self,value):self.fn(value)
        def host(frame):
            result=receiver.feed(frame)
            if result is not None:received.append(result)
        next_frames=[]
        peers={'host':Peer(host),'native':Peer(lambda frame:next_frames.append(producer.ack(frame)))}
        first=producer.begin(); header=relay.handle('native',first,peers)
        self.assertNotIn('base64',header['data']['image'])
        relay.forward_begin(peers)
        count=0
        while not received:
            while ack:relay.handle('host',ack.pop(0),peers)
            frame=next_frames.pop(0)
            self.assertLessEqual(len(encode(frame)),MAX_FRAME)
            relay.handle('native',frame,peers);count+=1
        self.assertGreater(count,300)
        self.assertEqual(received,[original])
        self.assertTrue(relay.active)
        relay.complete('123');self.assertFalse(relay.active)

    def test_receiver_rejects_duplicate_out_of_order_digest_and_incomplete(self):
        for mode in ('order','digest','incomplete'):
            with self.subTest(mode=mode):
                acks=[]; p=transfer.Producer(event(),'2'*32); r=transfer.Receiver(acks.append)
                r.feed(p.begin()); chunk=p.ack(acks.pop())
                if mode=='order':
                    chunk['index']=1
                    with self.assertRaises(ValueError):r.feed(chunk)
                elif mode=='incomplete':
                    with self.assertRaises(ValueError):r.feed({'kind':'input_image_commit','transferId':'2'*32})
                else:
                    chunk['data']=base64.b64encode(b'\0'*transfer.CHUNK_BYTES).decode()
                    r.feed(chunk)
                    while True:
                        frame=p.ack(acks.pop())
                        if frame['kind']=='input_image_commit':break
                        r.feed(frame)
                    with self.assertRaises(ValueError):r.feed(frame)

    def test_relay_identity_ack_and_deadline_are_strict(self):
        relay=transfer.Relay(clock=lambda:0)
        p=transfer.Producer(event(),'3'*32)
        peers={'host':type('P',(),{'send':lambda _,v:None})(),'native':type('P',(),{'send':lambda _,v:None})()}
        relay.handle('native',p.begin(),peers)
        with self.assertRaises(ValueError):relay.handle('native',{'kind':'input_image_ack','transferId':'3'*32,'index':-1},peers)
        with self.assertRaises(ValueError):relay.handle('host',{'kind':'input_image_ack','transferId':'4'*32,'index':-1},peers)
        relay.clock=lambda:181
        with self.assertRaises(ValueError):relay.check_deadline()

    def test_source_validator_runs_before_receiver_allocates(self):
        receiver=transfer.Receiver(lambda _:None, validate=lambda _:(_ for _ in ()).throw(ValueError('source')))
        with self.assertRaisesRegex(ValueError,'source'):receiver.feed(transfer.Producer(event(),'5'*32).begin())
        self.assertFalse(receiver.active)


if __name__=='__main__':unittest.main()
