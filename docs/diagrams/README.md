# Diagrams

Interactive pages that draw [architecture.md](../architecture.md). Where a page
and the doc disagree, the doc wins and the page is out of date. Open a page in
any current browser; it needs no build step.

| Page | Shows |
|---|---|
| [pipeline.html](pipeline.html) | The release pipeline: stages, the three gates, channels and what triggers each run |
| [architecture.html](architecture.html) | The ingress and every internal boundary, with its contract and failure behaviour |
| [control-plane.html](control-plane.html) | Control-plane flows, and wireframes of the TUI and the admin console |
| [stack.html](stack.html) | The dependency surface: what each part is built on and what breaks if it changes |
| [network.html](network.html) | The exposure profiles: what each opens beyond the host, all of it defaults |

## Standard

- **One stylesheet.** [`vhd.css`](vhd.css) holds the design tokens
  ([brand.md](../brand.md)) and the shared figure vocabulary. A page defines no
  colour of its own.
- **One script.** [`vhd.js`](vhd.js) gives every page the same detail panel and
  step-through behaviour.
- **Both themes.** Dark is the home theme; every page also reads correctly in
  light, and at phone width without horizontal scrolling.
- **Glossary terms only,** and the trademark line from brand.md in every
  footer.
- **Self-contained.** No build step and no outside requests. The fonts ship in
  [`fonts/`](fonts/): Alegreya SC, Alegreya Sans and IBM Plex Mono, Latin
  subset, from the Fontsource 5.3.0 packages, each under the SIL Open Font
  License (`fonts/OFL-*.txt`).
