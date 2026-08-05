#!/usr/bin/env python3
"""Replika control-plane entry point."""

import logging
import os

from waitress import serve

from tower.control_app import create_control_app


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)


def main():
    host = os.environ.get("TOWER_HOST", "0.0.0.0")
    port = int(os.environ.get("TOWER_PORT", "8080"))
    threads = int(os.environ.get("TOWER_THREADS", "8"))
    serve(create_control_app(), host=host, port=port, threads=threads)


if __name__ == "__main__":
    main()
