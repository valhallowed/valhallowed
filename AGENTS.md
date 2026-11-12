# Valhallowed: agent entry point

For anyone changing this repo, agents and people alike. It says where things
are, which rules hold everywhere, and how to verify a change; the docs hold
the reasons.

## Read first

| Question | Where |
|---|---|
| What VHD is, its ground rules and open decisions | [docs/architecture.md](docs/architecture.md) |
| What a word means | [docs/glossary.md](docs/glossary.md) |
| Every internal boundary's contract, with examples | [contracts/](contracts/) |
| Every setting and secret key, with its default and change cost | [schema/](schema/) |
| Names, voices, what the colours mean | [docs/brand.md](docs/brand.md) |
| Design tokens and fonts | [design/README.md](design/README.md) |
| The diagram pages and their standard | [docs/diagrams/README.md](docs/diagrams/README.md) |
| Where the work stands | The [board](https://github.com/orgs/valhallowed/projects/1) |

## Rules that hold everywhere

- **Glossary terms, glossary meanings.** A new term, or a new meaning, lands in
  docs/glossary.md first. Its "Words we don't use" table lists what to avoid.
- **Decisions live in docs/architecture.md.** A ground rule binds every change.
  An open decision is settled only when it moves to the ground rules with its
  reason.
- **One way in (G6).** Settings, secrets and operator actions enter only
  through the control plane and are validated there. Nothing past it reads raw
  input.
- **Every boundary** has one typed contract, validated by the receiver, one
  owner and a stated failure behaviour. Observations fail soft; actions fail
  closed.
- **No hard-coded ports, addresses or resource sizes (G9).** Each one the host
  sees is a default the manifest can change; wiring between VHD's own
  containers stays a named constant.
- **Only CI on `main` pushes images (G12).** Versions are promoted by tag, never
  rebuilt. Never push to the registry by hand.
- **Pin every dependency:** actions by commit SHA, base images by digest,
  packages by exact version. The one exception is G16's: SteamCMD and the
  dedicated server are downloaded at their newest, into the install and into
  the plugin's build.
- **Everything here is public:** the repo, its history and its images. Never
  commit host names, private addresses, personal paths, credentials or real
  player data. Acceptance material is synthesized or anonymized.
- **Colours and type come from design/tokens.css.** A surface never hard-codes
  a colour or a font.
- **Public pages carry the trademark line.** docs/brand.md says which pages
  are public, and the one it exempts.

## Verify

| Change | Run |
|---|---|
| Anything | `scripts/check-public.sh` |
| Workflows | `actionlint` |
| Shell scripts | `shellcheck scripts/*.sh` |
| The delivery check | `scripts/test-delivery.sh` |
| The manifest schema, a contract or their examples | `scripts/check-schema.sh` |
| `.github/dependabot.yml` | `scripts/check-schema.sh`, then `scripts/check-images.sh` |
| A Python component | `scripts/check-python.sh <dir>` |
| The data layer (`data/`, `images/data-layer/`) | `scripts/check-python.sh data`, then `scripts/check-data-layer.sh` |
| The plugin | `make plugin`, then `make accept` |
| Images or the install | `scripts/check-images.sh`, then `make accept` |
| A pinned download's version (an `ARG` in a Dockerfile) | `scripts/check-upstream.sh` |

CI runs all of these on every pull request and every push to `main`, except
`scripts/check-upstream.sh`, which `nightly.yml` runs. Acceptance runs on the
local build for a pull request and in `nightly.yml`, and from the registry on
`main`, after CI pushes the images, and before a tag moves them. CI runs
actionlint, shellcheck and Python from the images pinned in
`images/tools/Dockerfile`; locally, run the same images, for shellcheck with
`docker run --rm -v "$PWD:/repo:ro" -w /repo "$(scripts/tool-image.sh shellcheck)" scripts/*.sh`.

## Delivering

Work is planned on the [board](https://github.com/orgs/valhallowed/projects/1)
and lands through pull requests.

- **Three tracks run side by side.** Each owns its directories, so slices in
  different tracks can be open at the same time.

  | Track | Owns |
  |---|---|
  | Pipeline | `.github/`, `scripts/`, `images/debian/`, `images/tools/`, `Makefile`, `docker-bake.hcl` |
  | Control plane | `control/`, `runtime/`, `schema/`, `install/`, their images, and the contracts of B1 and B12 |
  | Capability | `plugin/`, `data/`, `web-map/`, their images, and the contracts of every other boundary |

  A slice stays inside one track, and takes the docs and diagrams that
  describe what it changes. A new child, setting or secret is a control-plane
  slice, and merges before the capability slice that uses it. No two open pull
  requests change the same file, docs apart: a slice that must reach outside
  its track says so in its issue, and starts once no open pull request holds
  those files.
- **A slice has a size.** S changes one component and no contract or schema.
  M changes one contract or schema, and the code on each side of it. L adds a
  child, a boundary or a capability, and is split into smaller slices before
  any of it starts.
- **Contract first.** A slice that changes a contract or a schema commits it
  first, with its valid and invalid examples. The code on each side is written
  against that commit.
- **One branch, one pull request, and one open pull request per track.** It
  opens as a draft when the branch does, holds single-purpose commits, and
  closes its issues (`Closes #n`). Related pull requests are combined into one.
  A dependency update is not a slice: the week's Dependabot pull request and a
  Debian base update each merge on their own once their gates pass. A Debian
  base update whose build fails has its pins moved by hand, in a branch of our
  own that replaces it.
- **Up to date before it merges.** `main` takes a pull request only when its
  checks passed on a branch up to date with `main`. Bring a branch up to date
  by rebasing it, so its commits stay single-purpose.
- **A pull request merges itself.** Once it is ready, auto-merge is switched on
  for it, and GitHub merges it when its gates pass: `checks`, `build` and
  `delivery`, on a branch up to date with `main`. `delivery` holds what a
  machine can check of this section: the pull request closes an issue, its
  Retro section is filled, a second reader is named where one is due, and no
  older open pull request changes the same file. It runs from `main`'s own
  definition, so a pull request cannot change the check that judges it.
- **A change to a gate waits for a maintainer.** `.github/CODEOWNERS` gives
  `.github/workflows/`, the delivery check's script and tests, and itself to the
  maintainers, and a pull request that changes any of them merges only once one
  of them has approved it.
- **A retro before it is marked ready.** Review its whole diff for defects,
  simplification, performance and hygiene, and check every doc and comment it
  touches against the code, the glossary and docs/architecture.md, and that
  each issue it closes has its delivered tasks ticked. A pull request that
  changes a contract, a schema, the control plane, the game runtime, an image
  or the pipeline is also read whole by a reviewer who wrote none of it, named
  on the Retro section's `Second reader:` line. Its Retro section names the
  reviews, then lists each finding and the commit that fixed it, or says
  "No findings".
- **Fix, don't defer.** Every finding is fixed in the pull request. The one
  exception is a gap whose fix is the deliverable of a later slice that has an
  issue: it becomes a task in that issue, and its Retro row reads `task in #n`.
  A gap whose slice has no issue yet is fixed in the pull request.
- **Progress lives on the board, nowhere else.** Issues exist for the current
  milestone, plus the next one while it is planned; later work stays in
  docs/architecture.md § Delivery order. An issue's track and size are fields
  on the board; its body lists its tasks, and names the contract it changes and
  the slices it waits on. Its tasks are ticked as each is done, so a closed
  issue shows what it delivered. The issues `nightly.yml`
  opens are alerts, not plans, and close themselves when a pass succeeds. Docs
  say what VHD is and why, never where the work stands.
- **Delete merged branches,** on GitHub and in every clone.
