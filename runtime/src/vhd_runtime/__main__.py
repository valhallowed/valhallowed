"""Entry point: python -m vhd_runtime, run by tini as the container's init."""

import logging
import signal
import sys

from vhd_runtime.generation import Reader
from vhd_runtime.supervisor import Paths, Supervisor


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="vhd-runtime %(levelname)s %(message)s", stream=sys.stdout)
    paths = Paths()
    supervisor = Supervisor(paths=paths, reader=Reader(paths.generation, paths.contracts))
    signal.signal(signal.SIGTERM, supervisor.request_stop)
    signal.signal(signal.SIGINT, supervisor.request_stop)
    return supervisor.run()


if __name__ == "__main__":
    sys.exit(main())
