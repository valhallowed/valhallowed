# Brand

The Valhallowed name, its forms, the two voices and the seed of the design
system. Terms follow [glossary.md](glossary.md).

## Name

| Form | Use |
|---|---|
| **Valhallowed** | Product name: titles, the first mention in any doc, marketing, the admin console header. |
| **VHD** | Short form after the first mention, and in compact UI where the full name does not fit. |
| **valhallowed.com** | Home of the product. It links to the docs and releases in the public repo, which stay their only source. |

- Identifiers follow G11 in [architecture.md](architecture.md): the GitHub org
  `valhallowed`, repo `valhallowed/valhallowed`, images
  `ghcr.io/valhallowed/<component>`, CLI `vhd`.
- The product name and its marks never include "Valheim" or imitate the game's
  logo.
- Every public page, one made for people outside the project (the README, the
  diagram pages, release notes, valhallowed.com), carries one line:
  *Valhallowed is not affiliated with or endorsed by Iron Gate. Valheim is a
  trademark of Iron Gate AB.* One exemption: the valhallowed.com landing page,
  which stays almost wordless.

## Voices

| Voice | Where | Register |
|---|---|---|
| **Product** | Docs, settings reference, admin console, TUI, CLI, release notes | Plain, exact, technical. States what happens and what it costs. No in-world flavour. |
| **In-world** | Lines players read in game, help pages, journal and lore | A voice inside Valheim: the world's own nouns, one line of character, then exact facts. |

A surface uses one voice. The admin console never speaks in-world; a line a
player reads in game always does.

## Design system

- **Tokens:** [`design/tokens.css`](../design/tokens.css), with its fonts, is the
  single source ([design/README.md](../design/README.md)). Every surface uses
  it: the diagrams, the site and the product's interfaces.
- **Palette:** night surfaces with a hearth-gold accent. Dark is the home theme;
  the light theme is derived from it.
- **Zone colours:** one colour per part of the architecture, identical in every
  diagram.

| Zone | Colour | Covers |
|---|---|---|
| Ingress | hearth gold | Manifest, secrets, operator actions, the validator |
| Control | blue | Control plane, control API, TUI, admin console, CLI |
| Runtime | green | Game runtimes: server, BepInEx, plugin |
| Data | orange | Data layer: intake, broker, persistence, chat relay, web map, database |
| People | pink | Server managers and players |
| External | grey | Outside services and third-party dependencies |
| Neutral | the line colour | The pipeline's own steps and gates, and the host |

- **Type:** Alegreya SC for display, Alegreya Sans for text,
  IBM Plex Mono for code and identifiers. The Alegreya family's book-and-saga
  feel fits the subject while staying readable in dense technical pages.
