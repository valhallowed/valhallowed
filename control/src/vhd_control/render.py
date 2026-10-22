"""The install's compose file, rendered by vhd apply from the generation and the release's images.

Every bind mount names a host path, because the Compose that runs this file runs inside
vhd and hands the paths to the host's Docker.
"""

from dataclasses import dataclass

from vhd_control import layout
from vhd_control.reconcile import WORLD_PORTS, Generation

STOP_GRACE = "150s"
# Neither the control plane nor a runtime needs a Linux capability or a way to gain one.
LOCKED_DOWN = {"cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"]}
NOTE = "Rendered by vhd apply from manifest.toml and the release's compose.yaml. vhd apply rewrites it."


@dataclass(frozen=True)
class Host:
    """What vhd apply learned about the install from Docker."""

    project: str
    install: str
    docker_socket: str
    owner: tuple[int, int]
    images: dict[str, str]


def render(generation: Generation, host: Host) -> dict[str, object]:
    control_plane = host.images["control-plane"]
    install = {"type": "bind", "source": host.install, "target": str(layout.INSTALL)}
    generations = {"type": "volume", "source": layout.GENERATIONS_VOLUME, "target": str(layout.GENERATIONS)}
    control = {"type": "volume", "source": layout.CONTROL_VOLUME, "target": str(layout.CONTROL_DIR)}
    services: dict[str, object] = {
        layout.CONTROL_PLANE: {
            "image": control_plane,
            "command": ["daemon"],
            # The install folder's owner, so the secrets file can stay readable by its owner alone.
            "user": f"{host.owner[0]}:{host.owner[1]}",
            "restart": "unless-stopped",
            **LOCKED_DOWN,
            "networks": [layout.INSTALL_NETWORK],
            "volumes": [{**install, "read_only": True}, generations, control],
        },
        # Kept, so --remove-orphans never removes the one-shot running this file.
        "vhd": vhd_service(control_plane, host.install, host.docker_socket),
    }
    volumes: dict[str, object] = {layout.GENERATIONS_VOLUME: {}, layout.CONTROL_VOLUME: {}}
    for cid, child in sorted(generation.children.items()):
        first, last = child.port, child.port + WORLD_PORTS - 1
        services[cid] = {
            "image": host.images["game-runtime"],
            "restart": "unless-stopped",
            # The supervisor gives the game this long to save on a stop.
            "stop_grace_period": STOP_GRACE,
            **LOCKED_DOWN,
            "networks": [layout.INSTALL_NETWORK],
            "ports": [f"{first}-{last}:{first}-{last}/udp"],
            "volumes": [
                {
                    **generations,
                    "target": "/generation",
                    "read_only": True,
                    "volume": {"subpath": f"{cid}/{child.port}"},
                },
                {"type": "volume", "source": f"{cid}-data", "target": "/data"},
            ],
        }
        volumes[f"{cid}-data"] = {}
    return {
        "name": host.project,
        "x-vhd-generated": NOTE,
        "services": services,
        "networks": {layout.INSTALL_NETWORK: {}},
        "volumes": volumes,
    }


def vhd_service(image: str, install: str, docker_socket: str) -> dict[str, object]:
    """The one-shot that runs vhd: the only container with Docker's socket, and only for its run."""
    return {
        "image": image,
        "profiles": ["tools"],
        "user": "0",
        "network_mode": "none",
        "volumes": [
            {"type": "bind", "source": docker_socket, "target": layout.DOCKER_SOCKET},
            {"type": "bind", "source": install, "target": str(layout.INSTALL)},
            {"type": "volume", "source": layout.GENERATIONS_VOLUME, "target": str(layout.GENERATIONS)},
            {"type": "volume", "source": layout.CONTROL_VOLUME, "target": str(layout.CONTROL_DIR)},
        ],
    }
