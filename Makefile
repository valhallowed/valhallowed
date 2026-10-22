# Local build lane: the same bake definition CI uses, loaded into local Docker.
BUILD ?= $(shell git rev-parse --verify -q HEAD || echo dev)
# The dedicated server build to compile against: Steam's current one unless given.
GAME_BUILD ?= $(shell scripts/game-build.sh)

.PHONY: build plugin accept

build:
	VHD_BUILD=$(BUILD) GAME_BUILD=$(GAME_BUILD) TAGS=dev docker buildx bake --load

# The plugin, into dist/plugin. Downloads the dedicated server to build against.
plugin:
	VHD_BUILD=$(BUILD) GAME_BUILD=$(GAME_BUILD) docker buildx bake plugin
	scripts/check-plugin.sh dist/plugin

# Install the local build through the release compose file and accept it.
accept: build
	scripts/render-install.sh dev dist/compose.yaml
	VHD_PULL=never scripts/accept.sh dist/compose.yaml $(BUILD)
