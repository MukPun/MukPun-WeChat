#!/usr/bin/env python3
"""Read saved OneBot settings and perform an Event-only WS handshake.

No lifecycle/message/heartbeat event or API action is sent. Tokens stay only in
memory; all diagnostics are fixed labels, booleans or integer HTTP statuses.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import resource
import secrets
import socket
import stat

CONFIG = Path('/home/bridgeuser/AstrBot/data/cmd_config.json')
PORT = 6199
GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


def handshake(token=None):
    nonce = base64.b64encode(secrets.token_bytes(16)).decode('ascii')
    headers = ['GET /ws HTTP/1.1', 'Host: 127.0.0.1:6199', 'Upgrade: websocket',
               'Connection: Upgrade', 'Sec-WebSocket-Version: 13', 'Sec-WebSocket-Key: '+nonce,
               'X-Self-ID: 2147483601', 'X-Client-Role: Event']
    if token is not None:
        headers.append('Authorization: Bearer '+token)
    with socket.create_connection(('127.0.0.1', PORT), timeout=3) as client:
        client.settimeout(3)
        client.sendall(('\r\n'.join(headers)+'\r\n\r\n').encode('ascii'))
        raw = bytearray()
        while b'\r\n\r\n' not in raw and len(raw) < 8192:
            chunk = client.recv(min(4096,8192-len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if b'\r\n\r\n' not in raw:
            raise ValueError('incomplete_handshake')
        lines = bytes(raw).split(b'\r\n\r\n',1)[0].decode('ascii').split('\r\n')
        status = int(lines[0].split()[1])
        values = {}
        for line in lines[1:]:
            key, value = line.split(':',1)
            key = key.lower()
            if key in values:
                raise ValueError('duplicate_header')
            values[key] = value.strip()
        if status == 101:
            expected = base64.b64encode(hashlib.sha1((nonce+GUID).encode('ascii')).digest()).decode('ascii')
            if (values.get('sec-websocket-accept') != expected or values.get('upgrade','').lower() != 'websocket'
                    or 'upgrade' not in {part.strip() for part in values.get('connection','').lower().split(',')}):
                raise ValueError('invalid_handshake')
            # RFC6455 masked Close(1000), no event payloads or API actions.
            mask = secrets.token_bytes(4)
            body = b'\x03\xe8'
            client.sendall(b'\x88\x82'+mask+bytes(value^mask[index%4] for index,value in enumerate(body)))
        return status


def main():
    resource.setrlimit(resource.RLIMIT_AS,(67108864,67108864))
    resource.setrlimit(resource.RLIMIT_CPU,(3,3))
    result = {'stage':'astrbot_onebot_check','ok':False,'configured':False,
              'events_sent':0,'api_actions_sent':0,'configuration_changed':False}
    try:
        descriptor = os.open(CONFIG,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 4*1024*1024:
                raise ValueError('config_file')
            raw = os.read(descriptor,4*1024*1024+1)
        finally:
            os.close(descriptor)
        value = json.loads(raw.decode('utf-8-sig'))
        platforms = value.get('platform')
        if not isinstance(platforms,list) or len(platforms)>100:
            raise ValueError('config_shape')
        matches = [item for item in platforms if isinstance(item,dict) and item.get('type')=='aiocqhttp']
        result['onebot_platform_count'] = len(matches)
        if len(matches)!=1:
            result['code']='onebot_platform_not_unique'
            return result
        item = matches[0]
        result.update(configured=True,enabled=item.get('enable') is True)
        port = item.get('ws_reverse_port')
        result['port_matches'] = type(port) is int and port == PORT or port == str(PORT)
        token = item.get('ws_reverse_token')
        result['token_set'] = isinstance(token,str) and bool(token)
        token_ascii = isinstance(token,str) and 1 <= len(token)<=1024 and all(33<=ord(c)<=126 for c in token)
        result['token_bridge_compatible'] = token_ascii and len(token)>=16
        if not result['enabled'] or not result['port_matches']:
            result['code']='configuration_not_ready'
            return result
        anonymous = handshake()
        result['anonymous_http_status'] = anonymous
        result['authentication_enforced'] = anonymous in (401,403)
        if not token_ascii:
            result['code']='token_missing_or_invalid'
            return result
        authenticated = handshake(token)
        result['authenticated_http_status'] = authenticated
        result['websocket_handshake_ok'] = authenticated == 101
        result['ok'] = (authenticated == 101 and result['authentication_enforced']
                        and result['token_bridge_compatible'])
        result['code'] = 'ok' if result['ok'] else 'handshake_or_token_check_failed'
    except (OSError,ValueError,UnicodeError,KeyError,TypeError):
        result['code']='bounded_check_failed'
    return result


if __name__=='__main__':
    report = main()
    print(json.dumps(report,sort_keys=True),flush=True)
    raise SystemExit(0 if report['ok'] else 1)
