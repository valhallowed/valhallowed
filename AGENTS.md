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
  dedicated server download into the install, newest first.
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
| The manifest schema, a contract or their examples | `scripts/check-schema.sh` |
| `.github/dependabot.yml` | `scripts/check-schema.sh`, then `scripts/check-images.sh` |
| A Python component | `scripts/check-python.sh <dir>` |
| The plugin | `make plugin` |
| Images or the install | `scripts/check-images.sh`, then `make accept` |
| A pinned download's version (an `ARG` in a Dockerfile) | `scripts/check-upstream.sh` |

CI runs all of these on every pull request and every push to `main`, except
`scripts/check-upstream.sh`, which `nightly.yml` runs. Acceptance runs on the
local build for a pull request, and on `main` from the registry, after CI
pushes the images. CI runs actionlint, shellcheck and Python from the images
pinned in `images/tools/Dockerfile`; locally, run the same images with
`docker run --rm -v "$PWD:/repo:ro" -w /repo "$(scripts/tool-image.sh shellcheck)" scripts/*.sh`.

## Delivering

Work is planned on the [board](https://github.com/orgs/valhallowed/projects/1)
and lands through pull requests.

- **One branch, one pull request.** It opens as a draft when the branch does,
  holds single-purpose commits, and closes its issues (`Closes #n`). Like pull
  requests are combined into one, dependency updates included.
- **A retro before it is marked ready.** Review its whole diff for defects,
  simplification, performance and hygiene, and check every doc and comment it
  touches against the code, the glossary and docs/architecture.md. Its Retro
  section lists each finding and the commit that fixed it.
- **Fix, don't defer.** Every finding is fixed in the pull request. The one
  exception is a gap whose fix is a later slice's deliverable: it becomes a
  task in that slice's issue, which its Retro row names.
- **Progress lives on the board, nowhere else.** Issues exist for the current
  milestone, plus the next one while it is planned; later work stays in
  docs/architecture.md § Delivery order. An issue's tasks are ticked as each
  is done, so a closed issue shows what it delivered. The issues `nightly.yml`
  opens are alerts, not plans, and close themselves on the next pass. Docs say
  what VHD is and why, never where the work stands.
- **Delete merged branches,** on GitHub and in every clone.
