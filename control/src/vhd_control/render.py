"""The install's compose file, rendered by vhd apply from the generation and the release's images.

Every bind mount names a host path, because the Compose that runs this file runs inside
vhd and hands the paths to the host's Docker.
"""

from dataclasses import dataclass

from vhd_control import layout
from vhd_control.reconcile import WORLD_PORTS, Generation

# The release's images, by their keys in compose.yaml's x-vhd block.
CONTROL_PLANE_IMAGE = "control-plane"
GAME_RUNTIME_IMAGE = "game-runtime"
SKELETON_IMAGE = "skeleton"
IMAGES = (CONTROL_PLANE_IMAGE, GAME_RUNTIME_IMAGE, SKELETON_IMAGE)
# Where the skeleton listens inside its container; the host port is the web map's.
SKELETON_PORT = 8456
STOP_GRACE = "150s"
# Neither the control plane nor a runtime needs a Linux capability or a way to gain one.
LOCKED_DOWN = {"cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"]}
# Docker's local driver rotates by default, so no container's log can fill the host's disk.
LOGGING = {"logging": {"driver": "local"}}
NOTE = "Rendered by vhd apply from manifest.toml and the release's compose.yaml. vhd apply rewrites it."


@dataclass(frozen=True)
class Host:
    """What vhd apply learned about the install from Docker."""

    project: str
    install: str
    owner: tuple[int, int]
    images: dict[str, str]


def render(generation: Generation, host: Host, web_map_port: int) -> dict[str, object]:
    control_plane = host.images[CONTROL_PLANE_IMAGE]
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
            **LOGGING,
            "networks": [layout.INSTALL_NETWORK],
            "volumes": [{**install, "read_only": True}, generations, control],
        },
        # Named, so --remove-orphans never removes the one-shot running this file. Only
        # compose.yaml's definition, with Docker's socket, ever runs.
        "vhd": {"image": control_plane, "profiles": ["tools"]},
        # The release pipeline's stand-in for the web map, on the web map's port.
        "skeleton": {
            "image": host.images[SKELETON_IMAGE],
            "restart": "unless-stopped",
            **LOCKED_DOWN,
            **LOGGING,
            "ports": [f"{web_map_port}:{SKELETON_PORT}"],
        },
    }
    volumes: dict[str, object] = {layout.GENERATIONS_VOLUME: {}, layout.CONTROL_VOLUME: {}}
    for cid, child in sorted(generation.children.items()):
        first, last = child.port, child.port + WORLD_PORTS - 1
        services[cid] = {
            "image": host.images[GAME_RUNTIME_IMAGE],
            "restart": "unless-stopped",
            # The supervisor gives the game this long to save on a stop.
            "stop_grace_period": STOP_GRACE,
            **LOCKED_DOWN,
            **LOGGING,
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
