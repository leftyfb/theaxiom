"""Compatibility settings for Kolibri 0.19.5 behind a same-host port proxy.

Upstream's origin_or_port option correctly supports numeric ports in the
frontend, but its CSP construction inserts that integer into a string tuple.
Keep separate-origin sandboxing and restrict the CSP to the configured port.
"""
from kolibri.deployment.default.settings.base import *  # noqa: F401,F403
from kolibri.deployment.default.settings import base as upstream

_port = upstream.conf.OPTIONS["Deployment"]["ZIP_CONTENT_ORIGIN"]
if type(_port) is int:
    if not 1 <= _port <= 65535:
        raise ValueError("Invalid alternate origin port")
    _sources = (f"http://*:{_port}", f"https://*:{_port}")
    CSP_FRAME_SRC = upstream.CSP_DEFAULT_SRC + _sources
    CSP_MEDIA_SRC = upstream.CSP_DEFAULT_SRC + _sources
