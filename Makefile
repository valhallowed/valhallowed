# Local build lane: the same bake definition CI uses, loaded into local Docker.
BUILD ?= $(shell git rev-parse --verify -q HEAD || echo dev)

.PHONY: build plugin accept

build:
	VHD_BUILD=$(BUILD) TAGS=dev docker buildx bake --load

# The plugin, into dist/plugin. Downloads the dedicated server to build against.
plugin:
	VHD_BUILD=$(BUILD) docker buildx bake plugin
	scripts/check-plugin.sh dist/plugin

# Install the local build through the release compose file and accept it.
accept: build
	scripts/render-install.sh dev dist/compose.yaml
	VHD_PULL=never scripts/accept.sh dist/compose.yaml $(BUILD)
