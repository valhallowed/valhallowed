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
CONTROL_DIR = Path("/run/vhd-control")
CONTROL_SOCKET = CONTROL_DIR / "control.sock"
# The schemas the release ships, schema/ and contracts/, inside the control plane's image.
SCHEMAS = Path("/opt/vhd")
# Where Docker's socket appears in vhd, and only there.
DOCKER_SOCKET = "/var/run/docker.sock"
# The install's private network: runtimes and the control plane's status listener.
INSTALL_NETWORK = "install"
# Named volumes, which Compose prefixes with the project name.
GENERATIONS_VOLUME = "generations"
CONTROL_VOLUME = "control"
# The file vhd apply renders into the install folder, and the folder it sits in.
RENDERED = Path(".vhd") / "compose.json"
