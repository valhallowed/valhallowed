"""Entry point: python -m vhd_control, which the control plane's image runs as vhd."""

import sys

from vhd_control.cli import main

if __name__ == "__main__":
    sys.exit(main())
