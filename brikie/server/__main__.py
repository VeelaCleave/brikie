"""CLI entry point for the brikie.co server.

Run via::

    python3 -m brikie.server --port 8321 --data-dir ~/.brikie/registry
"""

from __future__ import annotations

import argparse
import logging
import sys

from brikie.server.registry_server import DEFAULT_PORT, RegistryServer

DEFAULT_DATA_DIR = "~/.brikie/registry"


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and serve until interrupted."""
    parser = argparse.ArgumentParser(
        prog="brikie.server",
        description="brikie.co brick registry + installer generator",
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind address")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="bind port")
    parser.add_argument(
        "--data-dir", default=DEFAULT_DATA_DIR,
        help="directory holding published bricks",
    )
    parser.add_argument(
        "--base-url", default=None,
        help="public base URL for download links (e.g. https://brikie.co); "
             "derived from the request Host header when omitted",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s"
    )

    server = RegistryServer(
        data_dir=args.data_dir, host=args.host, port=args.port, base_url=args.base_url
    )
    print("▀▄▀▄▀▄  brikie.co registry  ▄▀▄▀▄▀")
    print(f"  registry : {server.url}")
    print(f"  website  : http://{args.host}:{server.port}/")
    print(f"  data dir : {server.store.data_dir}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down.")
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
