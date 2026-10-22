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

group "default" {
  targets = ["skeleton"]
}

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

# The plugin, built against the dedicated server's assemblies. Not in the
# default group: its output is files, not an image.
target "plugin" {
  inherits   = ["_common"]
  context    = "."
  dockerfile = "images/plugin/Dockerfile"
  target     = "plugin"
  output     = ["type=local,dest=dist/plugin"]
}

target "skeleton" {
  inherits = ["_common"]
  context  = "images/skeleton"
  tags     = [for t in split(",", TAGS) : "${REGISTRY}/skeleton:${t}"]
}
