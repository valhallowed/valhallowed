# Design

The single source of the Valhallowed design system: tokens and fonts. The
diagram pages, the valhallowed.com site and the product's interfaces use it.
What the colours mean, and the names and voices, are in
[docs/brand.md](../docs/brand.md).

| Path | Holds |
|---|---|
| [`tokens.css`](tokens.css) | Colour, zone and type tokens for the dark home theme and the derived light theme, and the `@font-face` rules |
| [`fonts/`](fonts/) | Alegreya SC, Alegreya Sans and IBM Plex Mono, Latin subset, from the Fontsource 5.3.0 packages, each with its SIL Open Font License (`OFL-*.txt`) |

## Using it

Link `tokens.css` and keep `fonts/` beside it; the font paths are relative.
Every token is a custom property on `:root`, so a surface styles with
`var(--ink)`, `var(--hearth)` and the rest, and never repeats a colour or a
font name. `data-theme="light"` or `data-theme="dark"` on the root element
overrides the system preference.

## Changing it

A token or font change lands here first, then reaches each surface. A consumer
outside this repo pins a commit and moves the pin to take a change.
