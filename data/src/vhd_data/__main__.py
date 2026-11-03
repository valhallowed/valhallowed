"""Entry point: python -m vhd_data <service>, run by tini as the container's init."""

import argparse
import logging
import signal
import sys
from pathlib import Path

from vhd_data import database, intake
from vhd_data.documents import Reader

GENERATION = Path("/generation")
CONTRACTS = Path("/opt/vhd/contracts")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vhd-data", description="Run one of Valhallowed's data-layer services.")
    parser.add_argument("service", choices=[database.SERVICE, intake.SERVICE])
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format=f"vhd-{args.service} %(levelname)s %(message)s", stream=sys.stdout)
    reader = Reader(args.service, GENERATION, CONTRACTS)
    if args.service == database.SERVICE:
        service = database.Database(paths=database.Paths(), reader=reader)
        signal.signal(signal.SIGTERM, service.request_stop)
        signal.signal(signal.SIGINT, service.request_stop)
        return service.run()
    taker = intake.Intake(reader=reader, contracts=CONTRACTS)
    signal.signal(signal.SIGTERM, taker.request_stop)
    signal.signal(signal.SIGINT, taker.request_stop)
    return taker.run(intake.server(taker, ("", intake.PORT)))


if __name__ == "__main__":
    sys.exit(main())
