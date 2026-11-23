"""vhd, the control plane's CLI. It runs from the control plane's image, so a host needs only Docker."""

import argparse
import logging
import os
import signal
import sys
import threading
from datetime import UTC, datetime
from typing import Any, TextIO

from vhd_control import client, layout, manifest
from vhd_control.apply import Apply, Refused
from vhd_control.daemon import Daemon
from vhd_control.docker import Cli, DockerError
from vhd_control.listeners import control_server, status_server
from vhd_control.manifest import InvalidSettings, duration
from vhd_control.reconcile import last_port, reconcile
from vhd_control.schemas import Schemas
from vhd_control.status import StatusBook
from vhd_control.store import Store


def main(argv: list[str] | None = None, out: TextIO = sys.stdout) -> int:
    parser = argparse.ArgumentParser(prog="vhd", description="Valhallowed's control plane CLI.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="check manifest.toml and secrets.toml, and show what they reconcile to")
    commands.add_parser("status", help="show each world and service as the control plane reports it")
    backups = commands.add_parser("backups", help="list a world's backups, newest first")
    backups.add_argument("world", help="the world's save name")
    restore = commands.add_parser("restore", help="restore one of a world's backups, once no players are online")
    restore.add_argument("world", help="the world's save name")
    restore.add_argument("backup", help="the backup's name, as vhd backups lists it")
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
            validate(schemas, out)
        elif args.command == "status":
            show_status(client.status(layout.CONTROL_SOCKET, schemas), out)
        elif args.command == "backups":
            show_backups(client.status(layout.CONTROL_SOCKET, schemas), args.world, out)
        elif args.command == "restore":
            held = client.restore(layout.CONTROL_SOCKET, schemas, args.world, args.backup)
            print(
                f"vhd restore: the control plane holds restore {held['id']} of {held['world']}'s backup "
                f"{held['backup']} and hands it to the world's runtime, which restores it once no players are "
                "online; vhd apply --disconnect-players restores it now. vhd status shows how it ends.",
                file=out,
            )
        elif args.command == "apply":
            Apply(
                install=layout.INSTALL,
                generations=layout.GENERATIONS,
                control_dir=layout.CONTROL_DIR,
                schemas=schemas,
                docker=Cli(),
                status=lambda: client.status(layout.CONTROL_SOCKET, schemas),
                hostname=os.environ.get("HOSTNAME", ""),
                out=out,
            ).run(args.disconnect_players)
        else:
            daemon(schemas)
    except (InvalidSettings, Refused, DockerError, client.Unreachable, client.Refused, NoSuchWorld) as error:
        print(f"vhd {args.command}: {error}", file=sys.stderr)
        return 1
    return 0


class NoSuchWorld(Exception):
    """vhd names a world the control plane's status does not have."""


def show_backups(status: dict[str, Any], world: str, out: TextIO, now: datetime | None = None) -> None:
    """A world's backups, newest first, each with its age, as its runtime last reported them."""
    now = now or datetime.now(UTC)
    if world not in status["worlds"]:
        raise NoSuchWorld(f"the install runs no world {world}")
    backups = status["worlds"][world].get("report", {}).get("backups")
    if backups is None:
        print(f"{world}: its runtime has not reported its backups yet", file=out)
        return
    for name in backups["names"]:
        age = _age_of(name, now)
        print(_printable(name) if age is None else f"{_printable(name)}, {age} old", file=out)
    if not backups["names"]:
        print(f"{world}: no backup yet", file=out)
    if "failure" in backups:
        print(f"{world}: backups: {_printable(backups['failure'])}", file=out)


def validate(schemas: Schemas, out: TextIO) -> None:
    settings = manifest.load(layout.INSTALL, schemas)
    reconcile(settings, None)
    for world in settings.worlds:
        password = "set" if world.password is not None else "unset"
        print(f"world {world.name}: udp {world.port}-{last_port(world.port)}, password {password}", file=out)
    print(f"web map: tcp {settings.web_map_port}", file=out)
    backups = settings.backups
    where = backups.path if backups.path.startswith("/") else f"{backups.path}/, relative to the install folder"
    print(
        f"backups: into {where}, after a new save at most every {duration(backups.every_s)}, "
        f"keeping the newest {backups.keep} and {backups.keep_daily} daily",
        file=out,
    )
    updates = settings.updates
    print(f"updates: {updates.policy}, checking every {duration(updates.every_s)}", file=out)


def show_status(status: dict[str, Any], out: TextIO, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    print(f"generation {status['generation']}", file=out)
    if "refused" in status:
        print(f"refused: {status['refused']}; generation {status['generation']} keeps running", file=out)
    if "override" in status:
        override = status["override"]
        named = ", ".join(
            f"{world} ({'players unknown' if players is None else f'{players} player(s)'})"
            for world, players in sorted(override["worlds"].items())
        )
        print(f"generation {override['generation']} restarted {named}, as the server manager confirmed", file=out)
    for name, world in sorted(status["worlds"].items()):
        line = f"{name}: {world['state']}"
        report = world.get("report", {})
        if "players" in report:
            line += f", {report['players']} player(s)"
        if "reason" in report:
            # A runtime faces players; its words reach a terminal only as printable text.
            line += f" ({_printable(report['reason'])})"
        if "backups" in report:
            line += _backups(report["backups"], now)
        line += _restore(world.get("restore"), report.get("restore"))
        if world.get("removed"):
            line += "; removed from the manifest, waits for vhd apply"
        elif "pending" in world:
            line += f"; generation {world['pending']['generation']} waits for {world['pending']['waits_for']}"
        print(line, file=out)
    for name, service in sorted(status["services"].items()):
        line = f"{name} service: {service['state']}"
        report = service.get("report", {})
        if "accepted" in report:
            line += f", {report['accepted']} event(s) taken, {report['dropped']} dropped"
        if "reason" in report:
            line += f" ({_printable(report['reason'])})"
        if "pending" in service:
            line += f"; generation {service['pending']['generation']} waits for {service['pending']['waits_for']}"
        print(line, file=out)


def _backups(backups: dict[str, Any], now: datetime) -> str:
    """A world's backups for its status line: how many, the newest one's age from its name, and why
    its runtime could not take or keep one."""
    names: list[str] = backups["names"]
    text = ", no backup yet"
    if names:
        text = f", {len(names)} backup(s)"
        age = _age_of(names[0], now)
        if age is not None:
            text += f", the newest {age} old"
    if "failure" in backups:
        text += f" (backups: {_printable(backups['failure'])})"
    return text


def _restore(held: dict[str, str] | None, acted: dict[str, str] | None) -> str:
    """A world's restore for its status line: the one the control plane holds, and how the runtime
    says the newest one it acted on stands, when that is the one held, or when none is."""
    text = ""
    if held is not None:
        text = f"; restore of backup {held['backup']} asked"
    if acted is not None and (held is None or acted["id"] == held["id"]):
        text += f"; restore {acted['outcome']}"
        if "reason" in acted:
            text += f" ({_printable(acted['reason'])})"
    return text


def _age_of(name: str, now: datetime) -> str | None:
    """The age of a backup named for when it was taken, or None for a name that is no time: a
    runtime's report is a child's words."""
    try:
        taken = datetime.strptime(name, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return _age((now - taken).total_seconds())


def _age(seconds: float) -> str:
    """An age in the largest unit that leaves a number of at least two."""
    whole = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if whole >= 2 * size:
            return f"{whole // size}{unit}"
    return f"{whole}s"


def _printable(text: str) -> str:
    return "".join(c if c.isprintable() else "?" for c in text)


def daemon(schemas: Schemas) -> None:
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
