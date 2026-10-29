#!/usr/bin/env python3
"""Voice specialization of the fixed, one-shot Native media HTTP client."""
import base64
import importlib.util
from pathlib import Path
import re
import sys
from urllib.parse import quote


def _module(name):
    path = Path(__file__).with_name(name + '.py')
    spec = importlib.util.spec_from_file_location('_voice_native_' + name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


shared = _module('image_send_native')
voice = shared._contract('voice_output')


class Client(shared.Client):
    media_kind = 'voice'
    deadline_seconds = 170
    max_polls = 340

    def _source(self, audio):
        voice.validate(audio)
        self.filename = 'bridge-voice.wav'
        self.samples = (audio['bytes'] - 44) // 2
        return base64.b64decode(audio['base64'], validate=True)

    def _known_failure(self, job):
        if not super()._known_failure(job):
            return False
        chunks = job['result'].get('chunks', [])
        # Later chunks return to preparing after an earlier accepted send.
        # Only an empty/all-prepared plan proves no part reached WeChat.
        return (isinstance(chunks, list) and all(isinstance(c, dict)
                and c.get('state') == 'prepared' and c.get('serverMessageId') is None for c in chunks))

    def _accepted(self, job):
        result = job.get('result')
        if not isinstance(result, dict):
            raise shared.ImageSendError('image_receipt_binding')
        receipt, chunks = result.get('nativeReceipt'), result.get('chunks')
        notes = (self.samples + 959999) // 960000
        part = (self.samples + notes - 1) // notes
        if (job.get('phase') != 'completed' or not shared._server(job.get('serverMessageId'))
                or not isinstance(receipt, dict) or receipt.get('status') != 'server_accepted'
                or type(receipt.get('serviceCode')) is not int or receipt['serviceCode'] != 0
                or receipt.get('clientIdMatches') is not True
                or receipt.get('serverMessageId') != job['serverMessageId']
                or not isinstance(chunks, list) or len(chunks) != notes):
            raise shared.ImageSendError('image_receipt_binding')
        servers = []
        for index, chunk in enumerate(chunks):
            duration = min(part, self.samples - index * part) * 1000 // 16000
            if (not isinstance(chunk, dict) or type(chunk.get('index')) is not int or chunk['index'] != index
                    or chunk.get('state') != 'accepted' or type(chunk.get('durationMs')) is not int
                    or chunk['durationMs'] != duration or not shared._server(chunk.get('serverMessageId'))):
                raise shared.ImageSendError('image_receipt_binding')
            servers.append(chunk['serverMessageId'])
        if len(set(servers)) != len(servers) or servers[-1] != job['serverMessageId']:
            raise shared.ImageSendError('image_receipt_binding')
        self.server_id, self.server_confirmed = job['serverMessageId'], True
        return result, chunks

    def _projection(self, result, chunks, deadline):
        ids = result.get('messageIds')
        if (result.get('profileProjection') != 'complete' or result.get('officialMediaWriteback') != 'complete'
                or not isinstance(ids, list) or len(ids) != len(chunks)
                or any(not shared._identity(item) for item in ids) or len(set(ids)) != len(ids)):
            return False
        for identity, chunk in zip(ids, chunks):
            path = self.policy.prefix + '/messages/' + quote(identity, safe='')
            self.policy.gets.update({path, path + '/media'})
            message = self.request('GET', path, deadline)
            if (message.get('id') != identity or message.get('accountId') != self.account
                    or message.get('threadId') != self.destination or message.get('senderId') != self.self_sender_id
                    or message.get('direction') != 'outgoing' or type(message.get('type')) is not int
                    or message['type'] != 34 or message.get('serverId') != chunk['serverMessageId']):
                raise shared.ImageSendError('image_projection_binding')
            items = self.request('GET', path + '/media', deadline).get('items')
            if not isinstance(items, list) or len(items) != 2:
                raise shared.ImageSendError('image_projection_binding')
            for variant, mime, maximum in (('encoded', 'audio/silk', 200 * 1024), ('normal', 'audio/wav', 60 * 32000 + 44)):
                assets = [a for a in items if isinstance(a, dict) and a.get('variant') == variant]
                if len(assets) != 1:
                    raise shared.ImageSendError('image_projection_binding')
                asset = assets[0]
                if (asset.get('accountId') != self.account or asset.get('messageId') != identity
                        or asset.get('kind') != 'voice' or asset.get('state') != 'available'
                        or asset.get('officialWriteback') != 'complete'
                        or asset.get('actualVariant') != variant or asset.get('contentType') != mime
                        or type(asset.get('byteLength')) is not int or not (12 if variant == 'encoded' else 76) <= asset['byteLength'] <= maximum
                        or not isinstance(asset.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', asset['sha256'])):
                    raise shared.ImageSendError('image_projection_binding')
        self.projection_verified = True
        return True
