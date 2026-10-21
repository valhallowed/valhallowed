# Valhallowed: agent entry point

For anyone changing this repo, agents and people alike. It says where things
are, which rules hold everywhere, and how to verify a change. It does not
restate the docs.

## Read first

| Question | Where |
|---|---|
| What VHD is, its ground rules and open decisions | [docs/architecture.md](docs/architecture.md) |
| What a word means | [docs/glossary.md](docs/glossary.md) |
| Names, voices, design tokens | [docs/brand.md](docs/brand.md) |
| The diagram pages and their standard | [docs/diagrams/README.md](docs/diagrams/README.md) |

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
- **No hard-coded ports, addresses or resource sizes (G9).** Each is a default
  the manifest can change.
- **Only CI on `main` pushes images (G12).** Versions are promoted by tag, never
  rebuilt. Never push to the registry by hand.
- **Pin every dependency:** actions by commit SHA, base images by digest,
  packages by exact version.
- **Everything here is public:** the repo, its history and its images. Never
  commit host names, private addresses, personal paths, credentials or real
  player data. Acceptance material is synthesized or anonymized.
- **Every public page carries the trademark line** in docs/brand.md.

## Verify

| Change | Run |
|---|---|
| Anything | `scripts/check-public.sh` |
| Workflows | `actionlint` |
| Shell scripts | `shellcheck scripts/*.sh` |
| Images or the install | `make accept` |

CI runs the same checks on every pull request and every push to `main`.
