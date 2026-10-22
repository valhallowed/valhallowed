# Valhallowed

A self-hosted Valheim dedicated server with a server-side plugin, telemetry,
governed in-game commands and a web map, configured through one manifest. VHD
for short. Its home is [valhallowed.com](https://valhallowed.com).

**Status: pre-release.** This repository holds the design, the manifest
schema, the release pipeline and a stand-in image. Nothing here is useful to
install yet.

## Docs

| Doc | What it covers |
|---|---|
| [Architecture](docs/architecture.md) | The target, ground rules, the model, boundaries, exposure, releases, open decisions |
| [Glossary](docs/glossary.md) | Every term, with exactly one meaning |
| [Schema](schema/) | Every setting and secret key, with its default and change cost |
| [Brand](docs/brand.md) | The name, the two voices, what the colours mean |
| [Design](design/README.md) | The design system: tokens and fonts |
| [Diagrams](docs/diagrams/README.md) | Interactive pages that draw the architecture |

## Releases

Images live at `ghcr.io/valhallowed/<component>`. They are built once, on
`main`, and a version tag promotes them without a rebuild; candidates and
releases attach `compose.yaml`, pinned by digest, the manifest schema and
`SHA256SUMS`. The channels and gates are in
[Architecture § Releases](docs/architecture.md#releases).

## Building locally

Needs a Unix-like shell (Linux, macOS, or WSL on Windows), Docker with Buildx
and Compose, `make`, `jq` and `curl`.

```bash
make accept
```

Builds the images with the definition CI uses, installs them through the
release compose file and runs acceptance against the local build.

## Contributing

Start with [AGENTS.md](AGENTS.md). Its rules apply to people as much as to
agents.

## License

[MIT](LICENSE).

---

Valhallowed is not affiliated with or endorsed by Iron Gate. Valheim is a
trademark of Iron Gate AB.
