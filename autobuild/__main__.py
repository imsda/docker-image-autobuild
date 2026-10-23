"""CLI entry point: `python -m autobuild [run|once|status|check-config]`."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys

from . import __version__, docker_ops
from .builder import Builder
from .config import ConfigError, load_config
from .service import Service
from .state import State
from .webhook import start_server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="autobuild", description=__doc__)
    p.add_argument("-c", "--config", default=os.environ.get("AUTOBUILD_CONFIG", "autobuild.toml"))
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("run", help="watch repos forever (default)")
    once = sub.add_parser("once", help="check every repo once, build what changed, exit")
    once.add_argument("--force", action="store_true", help="rebuild even if unchanged")
    once.add_argument("repos", nargs="*", help="limit to these repo names")
    sub.add_parser("status", help="print saved build state")
    sub.add_parser("check-config", help="validate the config file")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        config = load_config(args.config)
    except ConfigError as e:
        logging.error("%s", e)
        return 2
    state = State(config.settings.state_file)
    cmd = args.cmd or "run"

    if cmd == "check-config":
        for r in config.repos:
            print(f"{r.name}: {r.url}@{r.branch} -> {r.image} tags={r.tags}")
        return 0
    if cmd == "status":
        print(json.dumps(state.all(), indent=2))
        return 0

    builder = Builder(config, state)
    if cmd == "once":
        unknown = [n for n in args.repos if config.repo(n) is None]
        if unknown:
            logging.error("unknown repo(s): %s", ", ".join(unknown))
            return 2
        for reg in config.registries:
            docker_ops.login(reg)
        ok = True
        for repo in config.repos:
            if args.repos and repo.name not in args.repos:
                continue
            builder.check(repo, force=args.force)
            ok &= state.get(repo.name).get("status") != "failed"
        return 0 if ok else 1

    service = Service(config, builder)
    httpd = start_server(config, service, state)

    def shutdown(signum, _frame):
        logging.info("received signal %d, shutting down", signum)
        service.stop()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    service.run()
    if httpd:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
