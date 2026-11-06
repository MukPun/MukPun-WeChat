"""Replicate the probe worker's sticker path inside the daemon netns."""
import json, re, sys, time, hashlib, base64
sys.path.insert(0, '/home/mukpun/MukPun-WeChat/scripts')
sys.path.insert(0, '/home/mukpun/MukPun-WeChat')
import image_native as mod

import subprocess
TOKEN = open('/proc/' + subprocess.check_output(['pgrep', '-f', '/opt/wx --server-config']).decode().split()[0] + '/root/var/lib/astrbot-wechat-poc/data/api-token').read().strip()
MID = 'msg_eyJkYXRhYmFzZSI6Im1lc3NhZ2UvbWVzc2FnZV8wLmRiIiwidGFibGUiOiJNc2dfZGE1YTZmZDRlNDllYmRkMWUzZjVlYmIxMGEzZDNiMzAiLCJsb2NhbElkIjoiMTg0NCJ9'
PEER = '19807690022@chatroom'

class FakeLookup:
    class Policy:
        prefix = '/v1/accounts/wechat-primary'
        path = ''
    policy = Policy()
    def _get(self, path, deadline, timeout=8):
        import http.client
        c = http.client.HTTPConnection('127.0.0.1', 26174, timeout=timeout)
        c.request('GET', '/v1/accounts/wechat-primary' + path if not path.startswith('/v1') else path,
                  None, {'Authorization': 'Bearer ' + TOKEN, 'Accept-Encoding': 'identity'})
        r = c.getresponse()
        raw = r.read()
        c.close()
        return json.loads(raw)

client = mod.Client('wechat-primary', TOKEN)
deadline = time.monotonic() + 60

print('STEP1 recent_summary(download=False, sticker=True)', flush=True)
try:
    r = mod.recent_summary(FakeLookup(), PEER, client, deadline, download=False, sticker=True)
    print('OK:', json.dumps(r, ensure_ascii=False)[:400])
except Exception as e:
    import traceback; traceback.print_exc()
    print('RAISED:', type(e).__name__, repr(e))

print('STEP2 read_sticker(download=True)', flush=True)
try:
    img = client.read_sticker(MID, time.monotonic() + 60)
    print('OK:', {k: img[k] for k in ('bytes', 'sha256', 'mime', 'quality')})
except mod.ImageError as e:
    print('ImageError(compliant):', str(e))
except Exception as e:
    import traceback; traceback.print_exc()
    print('RAISED:', type(e).__name__, repr(e))
