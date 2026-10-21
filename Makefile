# Local build lane: the same bake definition CI uses, loaded into local Docker.
BUILD ?= $(shell git rev-parse --verify -q HEAD || echo dev)

.PHONY: build accept

build:
	VHD_BUILD=$(BUILD) TAGS=dev docker buildx bake --load

# Install the local build through the release compose file and accept it.
accept: build
	scripts/render-install.sh dev dist/compose.yaml
	VHD_PULL=never scripts/accept.sh dist/compose.yaml $(BUILD)
