"""CLI entry point: `python -m autobuild [run|once|init|add-repo|status|check-config]`."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
from pathlib import Path

from . import __version__, docker_ops
from .builder import Builder
from .config import ConfigError, load_config
from .setup_wizard import SetupAborted, Wizard, load_secrets, secrets_path
from .service import Service
from .state import State
from .webhook import start_server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="autobuild", description="Watch git repos, build Docker images, and push them.")
    p.add_argument("-c", "--config", default=os.environ.get("AUTOBUILD_CONFIG", "autobuild.toml"))
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("run", help="watch repos forever (default)")
    once = sub.add_parser("once", help="check every repo once, build what changed, exit")
    once.add_argument("--force", action="store_true", help="rebuild even if unchanged")
    once.add_argument("repos", nargs="*", help="limit to these repo names")
    sub.add_parser("init", help="interactive setup: choose repos and where to push")
    sub.add_parser("add-repo", help="interactively add another repo to watch")
    sub.add_parser("status", help="print saved build state")
    sub.add_parser("check-config", help="validate the config file")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    cmd = args.cmd or "run"
    config_path = Path(args.config)
    load_secrets(secrets_path(config_path))

    if cmd == "init" or (cmd in ("run", "once") and not config_path.exists()):
        if not sys.stdin.isatty():
            logging.error(
                "no config at %s. Run the interactive setup first:\n"
                "  python -m autobuild init\n"
                "  (Docker: docker compose run --rm autobuild init)", config_path)
            return 2
        try:
            if not Wizard(config_path).run():
                print("Nothing saved.")
                return 1 if cmd == "init" else 2
        except (SetupAborted, KeyboardInterrupt):
            print("\nSetup cancelled; nothing saved.")
            return 1
        if cmd == "init":
            return 0
        print("\nStarting...\n")

    try:
        config = load_config(config_path)
    except ConfigError as e:
        logging.error("%s", e)
        return 2

    if cmd == "add-repo":
        reg = config.registries[0] if config.registries else None
        namespace = None
        if config.repos and "/" in config.repos[0].image:
            namespace = config.repos[0].image.rsplit("/", 1)[0]
        elif reg:
            namespace = reg.server
        try:
            Wizard(config_path).add_repo({r.name for r in config.repos}, push=reg is not None,
                                         namespace=namespace)
        except (SetupAborted, KeyboardInterrupt):
            print("\nCancelled; nothing saved.")
            return 1
        return 0

    state = State(config.settings.state_file)

    if cmd == "check-config":
        for r in config.repos:
            print(f"{r.name}: {r.url}@{r.branch} -> {r.image} tags={r.tags}")
        return 0
    if cmd == "status":
        print(json.dumps(state.all(), indent=2))
        return 0

    if not state.acquire_lock():
        logging.error("another autobuild instance is already using %s", config.settings.state_file)
        return 3
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
