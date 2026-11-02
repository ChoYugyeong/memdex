# memdex brand

**Ledger Ribbon.** A bookmark left in the file, set in Hahmlet, on the night ledger's ink with one amber rule.

| Asset | File | Use |
|---|---|---|
| Demo (README) | [`demo.gif`](demo.gif) · generator: [`demo/make_demo.py`](demo/make_demo.py) | two agents, one checkout: claim → inbox → handoff, recorded from real CLI output (the frame draws ⚠ and → as `!` and `>` because IBM Plex Mono has no glyph for them; quoted text elsewhere shows the real characters) |
| Logo (full colour, light ground) | [`logo.svg`](logo.svg) · [`logo.png`](logo.png) | README, docs, slides |
| Logo on dark | [`logo-on-dark.svg`](logo-on-dark.svg) · [`logo-on-dark.png`](logo-on-dark.png) | dark docs, terminal screenshots |
| Symbol only | [`symbol.svg`](symbol.svg) | favicon, app icon, avatars |
| Design tokens | [`tokens.css`](tokens.css) | any page that follows the brand |
| Guidelines (13 pages) | [`memdex-guidelines.pdf`](memdex-guidelines.pdf) | logo usage, colours, type, applications, accessibility |

## Colour

| Role | Name | Hex |
|---|---|---|
| Ink | Ledger Ink | `#0e1a1c` |
| Accent | Lamp Amber | `#f0b03f` |
| Ground | Bone | `#ece5d6` |

Light surfaces use Bone as the page and Ledger Ink as the text; the dark surface is Ledger Ink with Bone text. Lamp Amber is the only saturated colour and carries one thing at a time: the rule under the wordmark, a link, a call to action.

## Type

- **Display:** Hahmlet 600 — a contemporary Korean serif; carries both English and Korean at display size.
- **Text:** Gothic A1 400/500 — Korean + Latin, tabular figures on.
- **Mono:** Nanum Gothic Coding 400 — code and figures, Korean included.

All three are Google Fonts under the SIL Open Font License 1.1. The logo's wordmark is outlined type, which the OFL allows. Do not redistribute the font files: link to the sources listed in the guidelines PDF.

## Logo rules

- Clear space: one cap height around the lockup; derived from the wordmark, adjust after the first few uses.
- Minimum size: 22 px for the symbol+wordmark lockup at screen sizes; below that use the symbol only.
- Never fill the cut slot in the ribbon, never stretch it, never swap the ribbon for the wordmark's initial. The guidelines PDF shows each misuse.
- The ribbon may sit on Bone or on Ledger Ink; on anything else use the one-colour version in the ground's ink.

The logo and these files are MIT, like the project. Hahmlet, Gothic A1 and Nanum Gothic Coding remain under the OFL.

## Provenance

Regenerated 2026-11-02 with `brand-identity` 850f215 (`kit --full`, Windows 11, Chrome 156): the guidelines
PDF embeds four real fonts (`/FontFile2`) and zero Type3 — the earlier build carried a synthesised bold of
Gothic A1 Medium, which the skill now prevents with `font-synthesis:none`
([fatihaydost/brand-identity-skill#5](https://github.com/fatihaydost/brand-identity-skill/issues/5)).
