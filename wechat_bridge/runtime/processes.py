"""Only owner of worker unit names, captures and per-generation crash budgets."""
import secrets
import time


class Workers:
    def __init__(self, core, state):
        self.core, self.state = core, state
        self.peers, self.units, self.started = {}, {}, []
        self.generation = 0
        self.counted = set()
        self.supervisors = {name: core.supervisor.Supervisor(name) for name in ('native', 'host')}

    def launch(self, name, properties, entry, config):
        core = self.core
        core.require(name not in self.peers and name not in self.units, 'worker_ownership')
        unit = (core.groups.READER_PREFIX if name == 'native' else 'astrbot-wechat-private-host-') + secrets.token_hex(6) + '.service'
        core.tap.require_unit_absent(unit)
        arguments = ['/usr/bin/systemd-run', '--system', '--quiet', '--collect', '--wait', '--pipe', '--unit=' + unit,
                     *properties, '/usr/bin/python3', '-I', '-B', entry]
        self.started.append(unit)
        self.units[name] = unit
        self.peers[name] = core.Duplex(arguments, config)
        if name == 'host':
            self.state.host_wait['last'] = time.monotonic()
        return unit

    def count(self, names):
        for name in names:
            key = (self.generation, name)
            if key not in self.counted:
                self.supervisors[name].record()
                self.counted.add(key)
        return all(self.supervisors[name].recoverable() for name in names)

    def stop(self):
        """Stop only owned exact units, retaining any unconfirmed ownership."""
        failure = None
        for name, unit in tuple(self.units.items()):
            try:
                # stop_unit verifies inactive/MainPID=0 AND empty owned cgroup,
                # including --collect's explicit LoadState=not-found case.
                self.core.tap.stop_unit(unit, True)
            except Exception as error:
                failure = failure or error
            else:
                del self.units[name]
                self.started.remove(unit)
        if failure is not None:
            raise failure

    def close(self):
        for name, capture in tuple(self.peers.items()):
            capture.close()
            del self.peers[name]

    def next_generation(self):
        self.core.require(not self.peers and not self.units, 'worker_ownership')
        self.generation += 1
        self.counted.clear()

    def native_options(self):
        core, s = self.core, self.state
        # Rebuilt on every spawn: the token is re-read rather than retained,
        # so a restart does not depend on a value we already dropped.
        value = {'account': 'wechat-primary', 'token': s.pinned.token(), 'runtimeSeconds': s.seconds}
        if s.settings:
            value.update(privateName=s.settings['privateContact'], groupName=s.settings['groupName'],
                         roomId=s.room_id, groupPrefix=s.settings['groupPrefix'], sendInterval=s.settings['sendIntervalSeconds'],
                         groupWakeMode=s.settings['groupWakeMode'], selfProof=s.recovery_seed, imageInput=s.settings['imageInputEnabled'],
                         stickerInput=s.settings['stickerInputEnabled'], linkInput=s.settings['linkInputEnabled'],
                         groupContext=s.settings['groupContextEnabled'], imageOutput=s.settings['imageOutputEnabled'], voiceOutput=s.settings['voiceOutputEnabled'],
                         allGroups=s.settings['allGroupsEnabled'] and not s.settings.get('privateOnly', False),
                         privateOnly=s.settings.get('privateOnly', False))
            if s.live_only:
                value['liveOnly'] = True
                value['liveSince'] = s.live_since
        return value
    def spawn_native(self):
        core, s = self.core, self.state
        self.launch('native', core.groups.reader_properties(s.bundle, 'awpf-' + secrets.token_hex(4),
               s.pinned.host.parent, s.pinned.netfd, s.seconds, large_images=bool(s.settings)),
               '/opt/forward_native_worker.py', self.native_options())
    def spawn_host(self, peer, self_sender):
        core, s = self.core, self.state
        value = {'account': 'wechat-primary', 'peer': peer, 'generation': s.generation,
                 'runtimeSeconds': s.seconds, 'onebotToken': core.trial.read_onebot_token(),
                 'compatibility': core.wire.compatibility(s.host_digest)}
        if s.settings:
            value.update(roomId=s.room_id, groupName=s.settings['groupName'], privateName=s.settings['privateContact'],
                         groupPrefix=s.settings['groupPrefix'], sendInterval=s.settings['sendIntervalSeconds'],
                         segmentBytes=s.settings['segmentBytes'], legacyGeneration=s.legacy_generation,
                         groupWakeMode=s.settings['groupWakeMode'], selfSenderId=self_sender,
                         imageInput=s.settings['imageInputEnabled'], stickerInput=s.settings['stickerInputEnabled'], groupContext=s.settings['groupContextEnabled'],
                         imageOutput=s.settings['imageOutputEnabled'], voiceOutput=s.settings['voiceOutputEnabled'], allGroups=s.settings['allGroupsEnabled'] and not s.settings.get('privateOnly', False),
                         privateOnly=s.settings.get('privateOnly', False))
            if s.live_only:
                value['liveStart'] = core.wire.live_start({'runId': s.live_run_id,
                    'checkpoint': {'cursor': s.boundary, 'anchor': s.boundary_anchor}})
        self.launch('host', core.trial.host_properties(core.paths.VERIFICATION, s.seconds, hd_images=s.export_state is not None),
               '/opt/bridge/scripts/forward_host_worker.py', value)
