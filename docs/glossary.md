# Glossary

The one dictionary for Valhallowed. Every doc, diagram and agent instruction in
this repo uses these terms with exactly these meanings. A new term, or a new
meaning for an old one, lands here first.

## Terms

**Acceptance** — Installing a channel's images from the registry, anonymously,
on a clean runner, then checking that the install works. Commit builds are what
it installs; a candidate or a release publishes only after it passes. The same
check runs on a local build for a pull request and in `make accept`.

**Acceptance material** — Recorded sessions, world saves, schema constraints
and chat-command contracts that every capability must keep. Synthesized or
anonymized; never real player data.

**Admin console** — The control plane's web interface for server managers:
status, manifest errors, changes, worlds, backups and logs. Listens locally
unless the manifest opens it. Not the web map.

**Assistant** — The capability that answers players' questions in game, worded
by an LLM provider from facts the data layer computed (B7). Off without its
secret.

**Board** — The `valhallowed` GitHub project, where delivery progress lives:
one issue per slice or other piece of planned work, each in a column from
Backlog to Done. Docs say what VHD is and why; the board says where the work
stands.

**Boundary** — A point inside VHD where data or control crosses between two
parts. Each boundary has one typed contract, one validator on the receiving
side and a stated failure behaviour. Not the ingress, which is where things
enter VHD from outside.

**Bring your own (BYO)** — Using your own service in place of a built-in
default, such as an external database. Set through the manifest and secrets;
it never changes a code path.

**Broker** — The data-layer service that creates, queues and audits commands.
Nothing else creates a command.

**Candidate** — A version on its way to release: a commit build's images
promoted to an `X.Y.Z-rc.N` tag. It runs on the candidate channel before a
release tag promotes it.

**Capability** — A unit of function that switches on independently: telemetry,
in-game commands, chat relay, assistant, web map, sign-in. It declares what it
depends on, and its failure degrades only itself.

**Change cost** — What applying a setting change requires: live, service
restart or game-server restart. Every setting carries one in the manifest
schema, beside what applies it: the control plane or `vhd apply`.

**Channel** — A release stream a consumer follows: commit builds, candidates
or releases.

**Chat relay** — The capability that mirrors world events to an outside chat
service, such as Discord, and accepts commands from it.

**Child** — A container in an install that the control plane configures and
reports on: a game runtime or a data-layer service. Compose runs it (G13).

**CLI** — `vhd`, the control plane's command-line client, run from the control
plane's image. `vhd apply` brings an install's containers in line with its
current generation. It runs as a one-shot container that only the server
manager starts, with the Docker socket for that run only (G13).

**Command** — A request to change the game. Created only by the broker,
executed only by the plugin on the game thread, and audited.

**Consumer** — Anyone running a VHD release, the maintainers included. Every
consumer gets the same artifacts and the same settings.

**Control API** — The control plane's single typed interface. The TUI, the
admin console and the CLI are its clients.

**Control plane** — The part of VHD that owns ingress, reconcile, child
lifecycle, operator actions and status. Needed to start or change an install,
not to keep it running. It holds no Docker socket (G13).

**Data layer** — Telemetry intake, persistence, the broker, the chat relay, the
web map and the database. Each service runs as its own child (G14).

**Default** — What VHD uses when the manifest says nothing: a value or a
built-in service. Every default works with no setup.

**Effective configuration** — The complete configuration reconcile derives from
the manifest, secrets and defaults. Children read only this.

**Exposure profile** — The set of ports and addresses an install opens beyond
the host: LAN, public direct, public with TLS, or game only. Every port in a
profile is a default the manifest can change.

**Game build** — The Steam build id of the Valheim dedicated server: what the
plugin is built against, and what a game runtime installs.

**Game runtime** — A child that runs one world: the Valheim dedicated server,
BepInEx and the plugin, under its supervisor. It runs what the control plane
hands it and holds no policy.

**Generation** — One numbered effective configuration of the whole install.
Reconcile produces it whole or not at all: when a new manifest is rejected,
the last good generation keeps running. Each child receives its own part, its
generation document (B1), and adopts all of it or none.

**Hold** — A game runtime with its game stopped and its container up: after
the world failed world truth, or so its files can be replaced without a
Docker socket (G13).

**Host check** — The control plane's comparison of what the manifest asks for
(ports, memory and CPU per world, disk) with what the host has. A shortfall is
a warning in status, never a silent failure.

**Ingress** — Where settings, secrets and operator actions enter VHD from
outside. There is exactly one, and the control plane owns it.

**Install** — One running copy of VHD: a control plane and its children, run
by Compose as one project.

**Intake** — The data-layer service that receives telemetry from the plugin
(B2) and persists it.

**Lane** — One of three isolated ways VHD runs on a host. Build makes
artifacts, dev is a full install with a throwaway world, and consumer runs
released artifacts only.

**Manifest** — The one TOML file a server manager writes to configure an
install. Checked against the manifest schema; never holds secrets.

**Manifest schema** — The versioned JSON Schema for the manifest,
`schema/manifest.schema.json`: every setting with its type, default, change
cost and what applies it. The secrets file has its own beside it, with the
closed list of secret keys. Together they produce validation, defaults, each
child's configuration and the settings reference.

**Map export** — The file the plugin writes with a world's map, the input to
world render (B5).

**Milestone** — A set of slices that together reach a point in the delivery
order, tracked as a GitHub milestone. It lands in one or more pull requests.

**Nightly game check** — The pipeline run, every night, that builds the
plugin against the newest game build. A failure blocks the next candidate.
Beside it, the same run compares the pinned downloads Dependabot cannot watch
with their newest releases, which blocks nothing.

**Operator action** — A one-off request through the control API, such as
importing a world, taking a backup or reading logs. Validated like the
manifest.

**Pending change** — A validated change waiting until it can apply: a
game-server restart while players are online, or a change to the containers
that waits for `vhd apply`.

**Player** — A person playing in a world. Players use the game and the web map;
they never touch the control plane.

**Plugin** — The VHD BepInEx plugin loaded into each game runtime. It captures
telemetry and executes commands.

**Promotion** — Applying a version tag to images already in the registry.
Candidates and releases are promoted, never rebuilt (G12).

**Reconcile** — The control-plane step that merges the manifest, secrets and
defaults into a new generation.

**Record** — Player-authored data such as a map marker or a journal entry.
Stored by the data layer; never becomes a command.

**Registry** — The public image store VHD publishes to: GHCR (G10). The line
between building VHD and running it.

**Release** — A versioned set of VHD artifacts that share one version: the
images, `compose.yaml`, the plugin artifact and the manifest and secrets
schemas.

**Retro** — The review a pull request passes before it is marked ready: its
whole diff for defects, simplification, performance and hygiene, and every doc
and comment it touches against the code, this glossary and the architecture.
Every finding is fixed in that pull request, except a gap whose fix is a later
slice's deliverable.

**Secrets** — Sensitive values a server manager supplies, such as a world's
password or a chat service token, kept in a TOML file apart from the manifest.
Key names come from a closed list in the manifest schema.

**Server manager** — The person who operates an install through the control
plane. Not a player.

**Settings reference** — The user-facing list of every setting with its
default, change cost, what applies it and its effect. The manifest and
secrets schemas are its source, so it cannot disagree with them.

**Sign-in** — The capability that signs a player in to the web map through
Steam (B9). Only people the game has seen can sign in.

**Skeleton** — The release pipeline's stand-in image for the web map: it
serves the commit it was built from on the web map's port. Not a child.

**Slice** — One deliverable of a milestone that keeps `main` green on its own,
tracked as one issue on the board.

**Status** — The control plane's report on an install and its children. It
reports world truth, not container health.

**Supervisor** — The game runtime's own process. It reads the generation,
downloads SteamCMD and the dedicated server, starts the server with BepInEx
and the plugin, stops it with a save, holds, and reports status. It restarts
the game for a new generation only once it counts no players, unless the
server manager confirmed the restart.

**Telemetry** — What the plugin observes in a world and sends to intake (B2):
chat, presence, positions and world events. Observations, so a failure drops
them and the game never waits.

**TUI** — The control plane's terminal interface. Same control API as the admin
console; for hosts where a browser is unavailable or unwanted.

**Valhallowed (VHD)** — The published product: a self-hosted Valheim server
with the plugin, telemetry, commands and the web map, configured through one
manifest. VHD is the short form. Home: valhallowed.com.

**Walking skeleton** — The first end-to-end release: built, published,
installed from the registry on a clean host, with one live player on the web
map.

**Web map** — The player-facing web app: world map, journal and chat. Not the
admin console.

**World** — One Valheim save and the game runtime that serves it.

**World ports** — The UDP ports a world reserves on the host: its port through
port+2. Worlds that share a host need ranges that do not overlap.

**World render** — Turning a world's map export into the web map's base image.
Runs on the install, so no image carries a world's map.

**World truth** — Whether a world actually loaded, checked against its save.
The opposite of trusting container health.

## Words we don't use

Common words that VHD docs avoid, and what to say instead. CI rejects the first
three in docs and agent instructions.

| Avoid | Say |
|---|---|
| sidecar | data layer |
| basemap | world render output |
| write path | commands (broker plus plugin execution) |
| stack, for a running copy of VHD | install, or world when it means one game server |
| fleet | consumers, or the worlds of one install |
| operator, for the person | server manager ("operator action" keeps its name) |
