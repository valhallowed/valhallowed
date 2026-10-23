# One build definition for every image, used the same way locally and in CI.

variable "REGISTRY" {
  default = "ghcr.io/valhallowed"
}

# Commit the images are built from. Versions are tags, applied by promotion.
variable "VHD_BUILD" {
  default = "dev"
}

# Comma-separated tags to apply, for example "sha-1a2b3c4,edge".
variable "TAGS" {
  default = "dev"
}

# The dedicated server build the plugin compiles against, as
# scripts/game-build.sh prints it. Required by the plugin and the game runtime.
variable "GAME_BUILD" {
  default = ""
}

group "default" {
  targets = ["skeleton", "game-runtime", "control-plane"]
}

# Debian from its base image's own snapshot, the base of every image built on
# Debian, which names it as the "vhd-debian" context: a name no registry serves,
# so a build outside bake fails instead of pulling an unpinned debian. Never
# published itself.
target "debian" {
  platforms = ["linux/amd64"]
  context   = "images/debian"
}

function "tags" {
  params = [name]
  result = [for t in split(",", TAGS) : "${REGISTRY}/${name}:${t}"]
}

# What every published image records.
target "_common" {
  platforms = ["linux/amd64"]
  args = {
    VHD_BUILD = VHD_BUILD
  }
  labels = {
    "org.opencontainers.image.source"   = "https://github.com/valhallowed/valhallowed"
    "org.opencontainers.image.revision" = VHD_BUILD
    "org.opencontainers.image.licenses" = "MIT"
  }
}

# An image built from the repo root on the snapshot Debian.
target "_on_debian" {
  platforms = ["linux/amd64"]
  context   = "."
  contexts  = { vhd-debian = "target:debian" }
}

# The game runtime's Dockerfile, built against the dedicated server's assemblies.
target "_game" {
  inherits   = ["_common", "_on_debian"]
  dockerfile = "images/game-runtime/Dockerfile"
  args = {
    VHD_BUILD  = VHD_BUILD
    GAME_BUILD = GAME_BUILD
  }
}

# The plugin. Not in the default group: its output is files, not an image. The
# game runtime image carries the same build.
target "plugin" {
  inherits = ["_game"]
  target   = "plugin"
  output   = ["type=local,dest=dist/plugin"]
}

target "game-runtime" {
  inherits = ["_game"]
  target   = "runtime"
  tags     = tags("game-runtime")
}

target "skeleton" {
  inherits = ["_common"]
  context  = "images/skeleton"
  tags     = tags("skeleton")
}

# SteamCMD alone, loaded locally for scripts/game-build.sh. Never published.
target "steamcmd" {
  inherits   = ["_on_debian"]
  dockerfile = "images/game-runtime/Dockerfile"
  target     = "steamcmd"
  tags       = ["vhd-steamcmd:local"]
  output     = ["type=docker"]
}

target "control-plane" {
  inherits   = ["_common", "_on_debian"]
  dockerfile = "images/control-plane/Dockerfile"
  tags       = tags("control-plane")
}
