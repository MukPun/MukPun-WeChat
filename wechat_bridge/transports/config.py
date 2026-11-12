"""Internal transport configuration; public forwarder config is unchanged."""
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Endpoint:
    url: str
    token: str = field(repr=False)

    def __post_init__(self):
        parsed = urlsplit(self.url)
        if (parsed.scheme != 'ws' or parsed.hostname != '127.0.0.1' or parsed.username or parsed.password
                or parsed.path not in {'/ws', '/ws/'} or parsed.query or parsed.fragment
                or parsed.port is None or not 1 <= parsed.port <= 65535
                or not isinstance(self.token, str) or not 16 <= len(self.token) <= 1024
                or any(not 33 <= ord(c) <= 126 for c in self.token)):
            raise ValueError('invalid_endpoint')
