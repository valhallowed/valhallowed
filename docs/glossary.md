# Glossary

The one dictionary for Valhallowed. Every doc, diagram and agent instruction in
this repo uses these terms with exactly these meanings. A new term, or a new
meaning for an old one, lands here first.

## Terms

**Acceptance** — Installing a channel's images from the registry, anonymously,
on a clean runner, then checking that the install works. Every channel passes
it before it publishes.

**Acceptance material** — Recorded sessions, world saves, schema constraints
and chat-command contracts that every capability must keep. Synthesized or
anonymized; never real player data.

**Admin console** — The control plane's web interface for server managers:
status, manifest errors, changes, worlds, backups and logs. Listens locally
unless the manifest opens it. Not the web map.

**Boundary** — A point inside VHD where data or control crosses between two
parts. Each boundary has one typed contract, one validator on the receiving
side and a stated failure behaviour. Not the ingress, which is where things
enter VHD from outside.

**Bring your own (BYO)** — Using your own service in place of a built-in
default, such as an external database. Set through the manifest and secrets;
it never changes a code path.

**Broker** — The data-layer service that creates, queues and audits commands.
Nothing else creates a command.

**Capability** — A unit of function that switches on independently: telemetry,
in-game commands, chat relay, assistant, web map, sign-in. It declares what it
depends on, and its failure degrades only itself.

**Change cost** — What applying a setting change requires: live, service
restart or game-server restart. Every setting carries one in the manifest
schema.

**Channel** — A release stream a consumer follows: commit builds, release
candidates or releases.

**Chat relay** — The capability that mirrors world events to an outside chat
service, such as Discord, and accepts commands from it.

**Child** — A process the control plane configures and manages: a game runtime
or a data-layer service.

**Command** — A request to change the game. Created only by the broker,
executed only by the plugin on the game thread, and audited.

**Consumer** — Anyone running a VHD release, the maintainers included. Every
consumer gets the same artifacts and the same settings.

**Control API** — The control plane's single typed interface. The TUI, the
admin console and the CLI are its clients.

**Control plane** — The part of VHD that owns ingress, reconcile, child
lifecycle, operator actions and status. Needed to start or change an install,
not to keep it running.

**Data layer** — Telemetry intake, persistence, the broker, the chat relay, the
web map and the database. Its placement relative to the control plane is open
(O2).

**Default** — What VHD uses when the manifest says nothing: a value or a
built-in service. Every default works with no setup.

**Effective configuration** — The complete configuration reconcile derives from
the manifest, secrets and defaults. Children read only this.

**Exposure profile** — The set of ports and addresses an install opens beyond
the host: LAN, public direct, public with TLS, or game only. Every port in a
profile is a default the manifest can change.

**Game runtime** — A child that runs one world: the Valheim dedicated server,
BepInEx and the plugin. It runs what the control plane hands it and holds no
policy.

**Generation** — One numbered effective configuration. Applied whole or not at
all; when a new one is rejected, the last good generation keeps running.

**Host check** — The control plane's comparison of what the manifest asks for
(ports, memory and CPU per world, disk) with what the host has. A shortfall is
a warning in status, never a silent failure.

**Ingress** — Where settings, secrets and operator actions enter VHD from
outside. There is exactly one, and the control plane owns it.

**Install** — One running copy of VHD: a control plane and its children.

**Lane** — One of three isolated ways VHD runs on a host. Build makes
artifacts, dev is a full install with a throwaway world, and consumer runs
released artifacts only.

**Manifest** — The one typed file a server manager writes to configure an
install. Checked against the manifest schema; never holds secrets.

**Manifest schema** — The versioned definition of every setting: type, default,
change cost and what it affects. It produces validation, defaults, each child's
configuration and the settings reference.

**Operator action** — A one-off request through the control API, such as
importing a world, taking a backup or reading logs. Validated like the
manifest.

**Pending change** — A validated change waiting until it can apply, typically
a game-server restart while players are online.

**Player** — A person playing in a world. Players use the game and the web map;
they never touch the control plane.

**Plugin** — The VHD BepInEx plugin loaded into each game runtime. It captures
telemetry and executes commands.

**Promotion** — Applying a version tag to images already in the registry.
Candidates and releases are promoted, never rebuilt (G12).

**Reconcile** — The control-plane step that merges the manifest, secrets and
defaults into a new generation, then reports what was set, what defaulted and
what that implies.

**Record** — Player-authored data such as a map marker or a journal entry.
Stored by the data layer; never becomes a command.

**Registry** — The public image store VHD publishes to: GHCR (G10). The
boundary between building VHD and running it.

**Release** — A versioned set of VHD artifacts that share one version: images,
the plugin artifact and the manifest schema.

**Secrets** — Sensitive values a server manager supplies, such as a world's
password or a chat service token, kept apart from the manifest. Key names come
from a closed list in the manifest schema.

**Server manager** — The person who operates an install through the control
plane. Not a player.

**Settings reference** — The user-facing list of every setting with its
default, change cost and effect. Generated from the manifest schema.

**Status** — The control plane's report on an install and its children. It
reports world truth, not container health.

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
| stack | install, or world when it means one game server |
| fleet | consumers, or the worlds of one install |
| operator | server manager |
