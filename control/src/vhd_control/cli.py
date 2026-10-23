"""vhd, the control plane's CLI. It runs from the control plane's image, so a host needs only Docker."""

import argparse
import logging
import os
import signal
import sys
import threading
from typing import Any, TextIO

from vhd_control import client, layout, manifest
from vhd_control.apply import Apply, Refused
from vhd_control.daemon import Daemon
from vhd_control.docker import Cli, DockerError
from vhd_control.listeners import control_server, status_server
from vhd_control.manifest import InvalidSettings
from vhd_control.reconcile import reconcile
from vhd_control.schemas import Schemas
from vhd_control.status import StatusBook
from vhd_control.store import Store


def main(argv: list[str] | None = None, out: TextIO = sys.stdout) -> int:
    parser = argparse.ArgumentParser(prog="vhd", description="Valhallowed's control plane CLI.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="check manifest.toml and secrets.toml, and show what they reconcile to")
    commands.add_parser("status", help="show each world as the control plane reports it")
    apply = commands.add_parser("apply", help="bring the install's containers in line with its manifest")
    apply.add_argument(
        "--disconnect-players", action="store_true", help="apply even where it restarts a world players may be in"
    )
    commands.add_parser("daemon", help="run the control plane; Compose starts it")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="vhd %(levelname)s %(message)s", stream=sys.stderr)
    schemas = Schemas(layout.SCHEMAS)
    try:
        if args.command == "validate":
            return validate(schemas, out)
        if args.command == "status":
            return show_status(client.status(layout.CONTROL_SOCKET), out)
        if args.command == "apply":
            Apply(
                install=layout.INSTALL,
                store=Store(layout.GENERATIONS),
                control_dir=layout.CONTROL_DIR,
                schemas=schemas,
                docker=Cli(),
                status=lambda: client.status(layout.CONTROL_SOCKET),
                hostname=os.environ.get("HOSTNAME", ""),
                out=out,
            ).run(args.disconnect_players)
            return 0
        return daemon(schemas)
    except (InvalidSettings, Refused, DockerError, client.Unreachable) as error:
        print(f"vhd {args.command}: {error}", file=sys.stderr)
        return 1


def validate(schemas: Schemas, out: TextIO) -> int:
    settings = manifest.load(layout.INSTALL, schemas)
    reconcile(settings, None)
    for world in settings.worlds:
        password = "set" if world.password is not None else "unset"
        print(f"world {world.name}: udp {world.port}-{world.port + 2}, password {password}", file=out)
    print(f"web map: tcp {settings.web_map_port}", file=out)
    return 0


def show_status(status: dict[str, Any], out: TextIO) -> int:
    print(f"generation {status['generation']}", file=out)
    if "refused" in status:
        print(f"refused: {status['refused']}; generation {status['generation']} keeps running", file=out)
    for name, world in sorted(status["worlds"].items()):
        line = f"{name}: {world['state']}"
        report = world.get("report", {})
        if "players" in report:
            line += f", {report['players']} player(s)"
        if "reason" in report:
            line += f" ({report['reason']})"
        if world.get("removed"):
            line += "; removed from the manifest, waits for vhd apply"
        elif "pending" in world:
            line += f"; generation {world['pending']['generation']} waits for {world['pending']['waits_for']}"
        print(line, file=out)
    return 0


def daemon(schemas: Schemas) -> int:
    book = StatusBook()
    control = Daemon(install=layout.INSTALL, store=Store(layout.GENERATIONS), schemas=schemas, book=book)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda signum, frame: stop.set())
    signal.signal(signal.SIGINT, lambda signum, frame: stop.set())
    servers = [
        status_server(book, schemas, control.worlds, ("", layout.STATUS_PORT)),
        control_server(control, layout.CONTROL_SOCKET),
    ]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    control.run(stop)
    for server in servers:
        server.shutdown()
        server.server_close()
    return 0
