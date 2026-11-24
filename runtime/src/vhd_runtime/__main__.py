"""Entry point: python -m vhd_runtime, run by tini as the container's init.

With no arguments it starts the supervisor. vhd runs the same image with an action instead, in a
one-off container that never starts it: import <world>, or export <world> [<backup>].
"""

import logging
import signal
import sys
from datetime import UTC, datetime

from vhd_runtime import transfer
from vhd_runtime.generation import Reader
from vhd_runtime.supervisor import Paths, Supervisor

USAGE = "usage: python3 -m vhd_runtime [import <world> | export <world> [<backup>]]"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        return supervise()
    action, world, rest = args[0], args[1] if len(args) > 1 else "", args[2:]
    if not transfer.is_save_name(world) or (action, len(rest)) not in (("import", 0), ("export", 0), ("export", 1)):
        print(USAGE, file=sys.stderr)
        return 2
    # Logs to standard error: standard output's last line is the result vhd reads.
    logging.basicConfig(level=logging.INFO, format="vhd-runtime %(levelname)s %(message)s", stream=sys.stderr)
    mounts = transfer.Mounts()

    def now() -> datetime:
        return datetime.now(UTC)

    if action == "import":
        return transfer.run("import", world, mounts.contracts, lambda: transfer.import_save(mounts, world, now))
    backup = rest[0] if rest else None
    return transfer.run("export", world, mounts.contracts, lambda: transfer.export_backup(mounts, world, backup, now))


def supervise() -> int:
    logging.basicConfig(level=logging.INFO, format="vhd-runtime %(levelname)s %(message)s", stream=sys.stdout)
    paths = Paths()
    supervisor = Supervisor(paths=paths, reader=Reader(paths.generation, paths.contracts))
    signal.signal(signal.SIGTERM, supervisor.request_stop)
    signal.signal(signal.SIGINT, supervisor.request_stop)
    return supervisor.run()


if __name__ == "__main__":
    sys.exit(main())
