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
  targets = ["skeleton", "game-runtime"]
}

target "_common" {
  platforms = ["linux/amd64"]
  args = {
    VHD_BUILD  = VHD_BUILD
    GAME_BUILD = GAME_BUILD
  }
  labels = {
    "org.opencontainers.image.source"   = "https://github.com/valhallowed/valhallowed"
    "org.opencontainers.image.revision" = VHD_BUILD
    "org.opencontainers.image.licenses" = "MIT"
  }
}

# The plugin, built against the dedicated server's assemblies. Not in the
# default group: its output is files, not an image. The game runtime image
# carries the same build.
target "plugin" {
  inherits   = ["_common"]
  context    = "."
  dockerfile = "images/game-runtime/Dockerfile"
  target     = "plugin"
  output     = ["type=local,dest=dist/plugin"]
}

target "game-runtime" {
  inherits   = ["_common"]
  context    = "."
  dockerfile = "images/game-runtime/Dockerfile"
  target     = "runtime"
  tags       = [for t in split(",", TAGS) : "${REGISTRY}/game-runtime:${t}"]
}

target "skeleton" {
  inherits = ["_common"]
  context  = "images/skeleton"
  tags     = [for t in split(",", TAGS) : "${REGISTRY}/skeleton:${t}"]
}

# SteamCMD alone, loaded locally for scripts/game-build.sh. Never published.
target "steamcmd" {
  platforms  = ["linux/amd64"]
  context    = "."
  dockerfile = "images/game-runtime/Dockerfile"
  target     = "steamcmd"
  tags       = ["vhd-steamcmd:local"]
  output     = ["type=docker"]
}
