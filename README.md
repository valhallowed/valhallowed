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
   `docker compose run --rm vhd status` to see each world and the data
   layer's services.

`vhd apply` adds `COMPOSE_IGNORE_ORPHANS=true` to the folder's `.env`: the
host's Compose would otherwise call the install's own containers orphans of
`compose.yaml` and suggest `--remove-orphans`, which would delete them.

Edit either file and the control plane applies the change, keeping a game-server
restart pending while players are online. A change to the containers themselves,
such as a new world or port, waits for the next `vhd apply`.

## A held world

`vhd status` shows a world as `held`, with the reason, when it fails world truth:
it loaded empty where it held objects or had a save, or the plugin never reported
a world that held objects before. A world with nothing to lose runs `unverified`
instead. The game stops before its first autosave, so nothing empty is saved over the
real world. A restart checks the world again, so fix the cause first. `docker ps`
names each world's container, and `docker volume ls` its data volume.

- **After a game update the plugin cannot load on:** download the `compose.yaml`
  of the release that fixes it over the install's own, and run `vhd apply`.
- **A missing or damaged save:** run `docker stop` on the world's container, put a
  good save in its data volume under `saves/worlds_local/<save name>/`, and run
  `docker start`.
- **A new world stopped during its first load:** its save directory holds only
  `_main.0.fwl2`, which looks like a save. Run `docker stop`, remove that file
  and its directory, and run `docker start`, and the game creates the world
  again. If `ls` shows anything but that one file, stop: it is a real save.

  ```bash
  docker run --rm -v <data volume>:/data busybox ls "/data/saves/worlds_local/<save name>"
  docker run --rm -v <data volume>:/data busybox rm "/data/saves/worlds_local/<save name>/_main.0.fwl2"
  docker run --rm -v <data volume>:/data busybox rmdir "/data/saves/worlds_local/<save name>"
  ```

## Building locally

Needs Linux, or WSL on Windows (the scripts use bash 4 and GNU tools), and
Docker with Buildx and Compose, `make`, `git`, `jq` and `curl`.

```bash
make accept
```

Builds the images with the definition CI uses, installs them through the
release compose file and runs acceptance against them: the install's world
downloads the dedicated server from Steam and must load with the plugin, which
can take up to 20 minutes. It uses ports away from the defaults, so it never
meets an install on them.

```bash
make plugin
```

Builds the plugin into `dist/plugin`. The build downloads the dedicated server
to compile against its assemblies, and ships none of its files.

## Contributing

Start with [AGENTS.md](AGENTS.md). Its rules apply to people as much as to
agents.

## License

[MIT](LICENSE).

---

Valhallowed is not affiliated with or endorsed by Iron Gate. Valheim is a
trademark of Iron Gate AB.
