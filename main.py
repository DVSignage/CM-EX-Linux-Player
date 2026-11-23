#!/usr/bin/env python3
"""
CM:EX Linux Player — entry point.

All behaviour lives in the `cmx` package (see README.md "Architecture").
Run directly (`python3 main.py`) or via the systemd unit; the exit status is
0 for a clean stop, including the CMS's restart_service (systemd's
Restart=always starts the player again).
"""

import asyncio
import logging
import sys

from cmx import VERSION
from cmx.config import DEFAULT_CONFIG_DIR, Paths, Settings, load_config
from cmx.telemetry import install_crash_hook, write_crash_marker

log = logging.getLogger("player")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    settings = Settings.from_dict(load_config())
    logging.getLogger().setLevel(getattr(logging, settings.log_level, logging.INFO))
    log.info(f"CM:EX Linux Player {VERSION}")

    paths = Paths(DEFAULT_CONFIG_DIR)
    paths.ensure()
    install_crash_hook(paths.crash_dir, VERSION)

    from cmx.player import Player   # after logging is configured

    try:
        code = asyncio.run(Player(settings, paths).run())
    except KeyboardInterrupt:
        code = 0
    except Exception as e:
        write_crash_marker(paths.crash_dir, VERSION, e)
        log.error(f"Fatal error: {e}", exc_info=True)
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
