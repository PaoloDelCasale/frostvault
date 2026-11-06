"""Exercise the image's Uvicorn proxy configuration without opening sockets."""
from __future__ import annotations

import os
from unittest.mock import patch

from uvicorn import Config


def uvicorn_proxy_app(app, forwarded_allow_ips: str):
    # Deliberately use the environment, not the constructor override: this is
    # the path used by the image CMD. Never derive this from TRUSTED_PROXIES.
    with patch.dict(os.environ, {"FORWARDED_ALLOW_IPS": forwarded_allow_ips}):
        config = Config(app, log_config=None, lifespan="off", ws="none")
        config.load()
    return config.loaded_app
