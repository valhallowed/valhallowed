# Valhallowed

A self-hosted Valheim dedicated server with a server-side plugin, telemetry,
governed in-game commands and a web map, configured through one manifest. VHD
for short. Its home is [valhallowed.com](https://valhallowed.com).

The [board](https://github.com/orgs/valhallowed/projects/1) shows what is
built.

## Docs

| Doc | What it covers |
|---|---|
| [Architecture](docs/architecture.md) | What VHD is, the rules it is built under, and the decisions still open |
| [Glossary](docs/glossary.md) | Every term, with exactly one meaning |
| [Schema](schema/) | Every setting and secret key, with its default and change cost |
| [Brand](docs/brand.md) | The name, the two voices, what the colours mean |
| [Design](design/README.md) | The design system: tokens and fonts |
| [Diagrams](docs/diagrams/README.md) | Interactive pages that draw the architecture |

## Releases

Images live at `ghcr.io/valhallowed/<component>`. How they are built and
promoted, and what a release attaches, is in [Architecture §
Releases](docs/architecture.md#releases).

## Installing

Needs Docker Engine 26 or newer with Compose, on an x86-64 host. In a folder
of its own:

1. Download a release's `compose.yaml`.
2. Write `manifest.toml`. One world on every default takes two lines; see
   [`schema/examples/valid`](schema/examples/valid/).
3. For a world password, write `secrets.toml` beside it, readable by you
   alone.
4. Run `docker compose run --rm vhd apply`, and
   `docker compose run --rm vhd status` to see each world.

`vhd apply` adds `COMPOSE_IGNORE_ORPHANS=true` to the folder's `.env`: the
host's Compose would otherwise call the install's own containers orphans of
`compose.yaml` and suggest `--remove-orphans`, which would delete them.

Edit either file and the control plane applies the change, keeping a game-server
restart pending while players are online. A change to the containers themselves,
such as a new world or port, waits for the next `vhd apply`.

## Building locally

Needs Linux, or WSL on Windows (the scripts use bash 4 and GNU tools), and
Docker with Buildx and Compose, `make`, `git`, `jq` and `curl`.

```bash
make accept
```

Builds the images with the definition CI uses, installs them through the
release compose file and runs acceptance against the local build: its world
downloads the dedicated server and must load with the plugin. It uses ports
away from the defaults, so it never meets an install on them.

```bash
make plugin
```

Builds the plugin into `dist/plugin`. The build downloads the dedicated server
to compile against its assemblies, and keeps none of its files.

## Contributing

Start with [AGENTS.md](AGENTS.md). Its rules apply to people as much as to
agents.

## License

[MIT](LICENSE).

---

Valhallowed is not affiliated with or endorsed by Iron Gate. Valheim is a
trademark of Iron Gate AB.
