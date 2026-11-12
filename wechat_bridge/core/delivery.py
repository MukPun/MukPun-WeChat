"""Durable admission/ACK order and queue consumption over opaque platform data.

Assembly injects policy.checkpoint/project_message and stopped_error. Storage
retains the existing atomic admit_checkpoint and recovery protection behavior.
"""
import time
from ..storage import StateError


class DurableInbox:
    def _admit(self,value,data):
        """Internal admission of an already validated/staged source."""
        saved=self.policy.checkpoint(value)
        try:
            inserted=self.state.admit_checkpoint(data['id'],data,self.generation,saved)
        except StateError as error:
            if str(error)!='recovery_checkpoint_required': raise
            self.recovery_pending={**value,'data':data}
            self.report('recovery',code='recovery_checkpoint_required')
            return
        self.recovery_pending=None
        if not inserted:
            self.report('loop_guard',reason='duplicate_input')
        self.emit({'kind':'ack','cursor':value['cursor']})
        self.wake.set()

    def checkpoint(self, value):
        self.state.cursor(self.generation, self.policy.checkpoint(value))

    async def next_message(self):
        await self.activation.wait()
        while self.online:
            self.wake.clear()
            row = self.state.next_pending()
            if row:
                if 'image' in row[1] and not -5 <= time.time() - row[1]['timestamp'] <= 300:
                    self.state.expire(row[0])
                    self.report('image_input', outcome='expired')
                    continue
                return self.policy.project_message(row[1])
            await self.wake.wait()
        raise self.stopped_error('stopped')
