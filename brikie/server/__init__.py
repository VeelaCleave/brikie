"""brikie.co server side — the brick registry and installer generator.

This package is the reference implementation of what runs at brikie.co:

- ``store``           — filesystem-backed brick registry storage
- ``website``         — Ninite-style installer generation (HTML page,
                        Build Set JSON, install.sh)
- ``registry_server`` — the HTTP server tying both together

It is deliberately **not** a brick: it is the other side of the wire that
``brikie.bricks.registry`` talks to. Run it with::

    python3 -m brikie.server --port 8321
"""

from brikie.server.registry_server import RegistryServer
from brikie.server.store import RegistryStore, StoreError

__all__ = ["RegistryServer", "RegistryStore", "StoreError"]
