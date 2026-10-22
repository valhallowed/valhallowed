"""Names and paths an install's containers share. None of them reaches the host."""

from pathlib import Path

# The control plane's Compose service, which runtimes reach on the install network.
CONTROL_PLANE = "control-plane"
# Where runtimes send status (B1). Internal to the install network.
STATUS_PORT = 9457
# The install folder, read-only in the control plane, read-write in vhd.
INSTALL = Path("/install")
# The generations volume: one directory per child, which mounts only its own.
GENERATIONS = Path("/generations")
# The control API's socket, in a volume only the control plane and vhd mount.
CONTROL_SOCKET = Path("/run/vhd-control/control.sock")
