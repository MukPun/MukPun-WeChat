"""Worker crash accounting, modelled on NapCat's master/worker split.

NapCat keeps a thin master that only remembers how recently its worker died,
and restarts the worker unless it died three times inside ten seconds. The
master owns no forwarding state, so a restart costs one process and one login.

This bridge inverts that split for good reasons: the durable queue, the id
mapping and the send audits all live in SQLite under the controller, and the
workers are stateless pipes. What the controller lacked was NapCat's habit of
assuming a worker death is recoverable at all. Without a window, systemd's
StartLimitBurst=3 / StartLimitIntervalSec=600 gave up after three deaths in ten
minutes -- a threshold a 13.5-crashes-per-day service crosses routinely, and the
reason the bridge once stayed down for 37 hours.

The window here is deliberately short. A worker that dies three times in ten
seconds is not crashing on a transient frame; it is crashing on something
durable, and respawning it again only multiplies the damage. A worker that dies
three times in ten minutes may simply be unlucky, and deserves a restart.

Nothing here starts, stops or inspects a process. The controller already owns
that machinery, and duplicating it would give two writers to one unit name.
"""
import time

WINDOW = 10.0
LIMIT = 3


class Supervisor:
    """Per-worker crash history with a sliding window.

    Instantiate one per worker name. The controller calls `recoverable()` after
    every exit; False means the crash budget for this window is spent and the
    controller must fail as a whole rather than respawn.
    """

    def __init__(self, name, *, window=WINDOW, limit=LIMIT, clock=time.monotonic):
        if type(name) is not str or not name:
            raise ValueError('supervisor_name')
        if not 0 < window or not 0 < limit:
            raise ValueError('supervisor_bounds')
        self.name = name
        self.window = float(window)
        self.limit = int(limit)
        self.clock = clock
        self.crashes = []
        self.restarts = 0

    def _prune(self, now):
        # Keep the list sorted and bounded. Clock going backwards (a test clock
        # or a resumed suspend) must not wedge the window shut, so drop future
        # stamps too.
        self.crashes = [stamp for stamp in self.crashes if 0 <= now - stamp <= self.window]

    def record(self):
        """Note one exit. Returns how many exits fall inside the window."""
        now = self.clock()
        self._prune(now)
        self.crashes.append(now)
        return len(self.crashes)

    def recoverable(self):
        """Answer whether one more restart is still allowed.

        Deliberately does not record: the caller already did, and counting a
        single death twice would halve the budget. A crash budget that fires
        too early is worse than none, because it turns a recoverable blip into
        an outage.
        """
        return len(self.crashes) < self.limit

    def restarted(self):
        self.restarts += 1
        return self.restarts

    def state(self):
        """Bounded, JSON-safe view for the status frame."""
        return {'name': self.name, 'crashes': len(self.crashes), 'restarts': self.restarts,
                'window': self.window, 'limit': self.limit}
