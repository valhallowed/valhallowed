"""Entry point: python -m vhd_runtime, run by tini as the container's init.

With no arguments it starts the supervisor. vhd runs the same image with an action instead, in a
one-off container that never starts it: import <world>.
"""

import logging
import signal
import sys
from datetime import UTC, datetime

from vhd_runtime import transfer
from vhd_runtime.generation import Reader
from vhd_runtime.supervisor import Paths, Supervisor

USAGE = "usage: python3 -m vhd_runtime [import <world>]"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        return supervise()
    if len(args) == 2 and args[0] == "import" and transfer.is_save_name(args[1]):
        # Logs to standard error: standard output's last line is the result vhd reads.
        logging.basicConfig(level=logging.INFO, format="vhd-runtime %(levelname)s %(message)s", stream=sys.stderr)
        world, mounts = args[1], transfer.Mounts()
        return transfer.run("import", world, lambda: transfer.import_save(mounts, world, lambda: datetime.now(UTC)))
    print(USAGE, file=sys.stderr)
    return 2


def supervise() -> int:
    logging.basicConfig(level=logging.INFO, format="vhd-runtime %(levelname)s %(message)s", stream=sys.stdout)
    paths = Paths()
    supervisor = Supervisor(paths=paths, reader=Reader(paths.generation, paths.contracts))
    signal.signal(signal.SIGTERM, supervisor.request_stop)
    signal.signal(signal.SIGINT, supervisor.request_stop)
    return supervisor.run()


if __name__ == "__main__":
    sys.exit(main())
