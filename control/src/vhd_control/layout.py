"""Names and paths an install's containers share."""

from pathlib import Path

# The control plane's Compose service, which every child reaches on the install network.
CONTROL_PLANE = "control-plane"
# Where every child sends status (B1). Internal to the install network.
STATUS_PORT = 9457
# The data layer's services (G14), by their Compose service names, which Docker's DNS answers to.
DATABASE = "database"
INTAKE = "intake"
SERVICES = (DATABASE, INTAKE)
# Where each service listens on the install network. No service publishes a host port.
DATABASE_PORT = 5432
INTAKE_PORT = 9458
# The database the services share, and the role intake signs in as.
DATABASE_NAME = "vhd"
INTAKE_ROLE = "intake"
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
# The install's private network: every child and the control plane's status listener.
INSTALL_NETWORK = "install"
# Named volumes, which Compose prefixes with the project name.
GENERATIONS_VOLUME = "generations"
CONTROL_VOLUME = "control"
# In the install folder: the compose file the install runs, which vhd apply replaces with the
# next one only once Compose has run it, so it always names what last ran.
RENDERED = Path(".vhd") / "compose.json"
NEXT = Path(".vhd") / "compose.next.json"
