"""Root-owned voice relay: serialize, audit, bound chunks, never replay."""
import hashlib
import importlib.util
from pathlib import Path
import re
import sys
import time

_path = Path(__file__).with_name('voice_output.py')
if not _path.exists():
    _path = Path(__file__).resolve().parents[1] / 'wechat_bridge/voice_output.py'
_spec = importlib.util.spec_from_file_location('_voice_relay_contract', _path)
voice = importlib.util.module_from_spec(_spec); sys.modules[_spec.name] = voice; _spec.loader.exec_module(voice)
KINDS = frozenset({'send_voice', 'voice_begin', 'voice_chunk', 'voice_chunk_ack', 'voice_commit',
                   'voice_send_intent', 'native_voice_result'})


def preflight(core):
    path = core.owner.STATE / 'forward-voice-send.json'
    if path.exists():
        core.require(core.owner.read_json(path).get('state') in {'accepted', 'rejected'},
                     'previous_voice_send_requires_review')
    return path


class Relay:
    def __init__(self, core, pinned, settings, counters):
        self.core, self.pinned, self.settings, self.counters = core, pinned, settings or {}, counters
        self.path = preflight(core)
        self.request = self.audit = self.assembly = self.ack = None
        self.until = 0

    @property
    def pending(self):
        return self.request is not None

    def check_deadline(self):
        if self.pending and time.monotonic() >= self.until:
            self.audit.update(state='unknown', code='voice_timeout')
            self.core.owner.write_json(self.path, self.audit)
            self.core.require(False, 'voice_send_requires_review')

    def rejected(self, request):
        return {'account': 'wechat-primary', 'destination': request['destination'], 'job_id': '',
                'state': 'rejected', 'server_id': '', 'source_sha256': request['voice']['sha256'],
                'projection_verified': False}

    def handle(self, name, record, peers, *, allowed):
        kind = record.get('kind')
        if kind not in KINDS:
            return False
        require = self.core.require
        if kind in {'send_voice', 'voice_begin'}:
            require(name == 'host' and allowed and not self.pending and self.settings.get('voiceOutputEnabled') is True
                    and set(record) == {'kind', 'requestId', 'destination', 'voice', 'key'}
                    and isinstance(record.get('requestId'), str) and re.fullmatch('[0-9a-f]{32}', record['requestId'])
                    and isinstance(record.get('key'), str) and re.fullmatch('[0-9a-f]{32}', record['key']), 'voice_send_binding')
            assembly = voice.Assembly(record['voice']) if kind == 'voice_begin' else None
            if assembly is None:
                voice.validate(record['voice'])
            if not self.core.reserve_voice_upload(self.pinned, record['voice']):
                peers['host'].send({'kind': 'voice_receipt', 'requestId': record['requestId'], 'receipt': self.rejected(record)})
                return True
            self.audit = {'state': 'submitted', 'key': record['key'], 'requestId': record['requestId'],
                'destinationSha256': hashlib.sha256(record['destination'].encode()).hexdigest(),
                'sourceSha256': record['voice']['sha256'], 'bytes': record['voice']['bytes'],
                'durationMs': record['voice']['duration_ms']}
            self.core.owner.write_json(self.path, self.audit)
            self.request, self.assembly = record, assembly
            self.ack = -1 if assembly else None
            self.until = time.monotonic() + 170
            self.counters['voice_output_requests'] += 1
            peers['native'].send(record)
            return True
        require(self.pending and record.get('requestId') == self.request['requestId'], 'voice_request_binding')
        if kind == 'voice_chunk_ack':
            require(name == 'native' and set(record) == {'kind', 'requestId', 'index'}
                    and self.assembly is not None and self.ack is not None
                    and type(record['index']) is int and record['index'] == self.ack, 'voice_chunk_ack')
            self.ack = None
            peers['host'].send(record)
        elif kind == 'voice_chunk':
            require(name == 'host' and set(record) == {'kind', 'requestId', 'index', 'data'}
                    and self.assembly is not None and self.ack is None, 'voice_chunk_binding')
            self.assembly.add(record['index'], record['data'])
            self.ack = record['index']
            peers['native'].send(record)
        elif kind == 'voice_commit':
            require(name == 'host' and set(record) == {'kind', 'requestId'}
                    and self.assembly is not None and self.ack is None, 'voice_commit_binding')
            self.assembly.check()
            self.assembly = None
            peers['native'].send(record)
        elif kind == 'voice_send_intent':
            require(name == 'native' and set(record) == {'kind', 'requestId', 'jobId'}
                    and self.assembly is None and voice.job_id(record.get('jobId'))
                    and 'jobId' not in self.audit, 'voice_send_intent')
            self.audit['jobId'] = record['jobId']
            self.core.owner.write_json(self.path, self.audit)
        elif kind == 'native_voice_result':
            require(name == 'native' and set(record) == {'kind', 'requestId', 'receipt', 'ok', 'server_confirmed', 'submitted', 'code'}
                    and all(type(record[k]) is bool for k in ('ok', 'server_confirmed', 'submitted'))
                    and isinstance(record.get('code'), str) and re.fullmatch('ok|voice_[a-z_]{1,48}', record['code']),
                    'voice_send_receipt')
            request, receipt = self.request, record['receipt']
            accepted = (record['ok'] and record['server_confirmed'] and record['submitted']
                and receipt.get('job_id') == self.audit.get('jobId')
                and voice.accepted(receipt, 'wechat-primary', request['destination'], request['voice']['sha256']))
            unsubmitted = not record['submitted'] and not record['server_confirmed']
            if unsubmitted:
                receipt = self.rejected(request)
            self.audit.update(state='accepted' if accepted else 'rejected' if unsubmitted else 'unknown', code=record['code'])
            self.core.owner.write_json(self.path, self.audit)
            peers['host'].send({'kind': 'voice_receipt', 'requestId': request['requestId'], 'receipt': receipt})
            self.counters['accepted_parts'] += int(accepted)
            self.counters['voice_output_sent'] += int(accepted)
            self.counters['uncertain'] += int(not accepted and not unsubmitted)
            self.request = self.assembly = self.ack = None
            require(accepted or unsubmitted, 'voice_send_requires_review')
        else:
            require(False, 'voice_frame')
        return True
