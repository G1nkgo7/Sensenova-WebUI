# Font system: verifiable open fonts and user-authorized fonts

## 1. License boundary

Use only fonts whose license permits packaging, fonts installed by this Skill's installer, or fonts explicitly supplied and authorized by the user. Do not assume a locally installed commercial family may be packaged in delivery artifacts.

`materials/font-config.json`, when present, is authoritative for title, body, number, mono, and annotation roles. Keep the original user font files under materials and let the deterministic font bundler create delivery subsets.

## 2. Role tokens and safe fallbacks

| Role | CSS token | Recommended open families | Purpose |
| --- | --- | --- | --- |
| Display/title | `--font-display` | Noto Sans/Serif SC; Unbounded, Syne, Archivo, Fraunces, or another registered display role when the occasion supports it | Covers, dividers, large statements |
| Body | `--font-body` | Noto Sans SC, Noto Serif SC | Paragraphs, tables, labels |
| Numbers | `--font-number` | IBM Plex Sans, Archivo, DM Sans | KPIs and numeric hierarchy |
| Mono/data | `--font-mono` | IBM Plex Mono | **Pure-Latin** code, coordinates, IDs, real serial numbers; no CJK glyphs — **never use for Chinese** |
| Annotation | `--font-annotation` | Body role; Xiaolai/LXGW WenKai or a registered handwriting role when intentional | Captions, sources, notes, short expressive quotations |

Always include a generic fallback at the end of a stack. A family name alone does not prove the font exists or is distributable.

## 3. Usage rules

- Activate role tokens explicitly in `base.css`; do not rely on browser defaults.
- **Whole-deck font families ≤ 3 (hard limit).** Across the entire deck use at most **3** font families, applied uniformly — the same role uses the same family on every page; do not swap fonts per page topic. A typical split: one Chinese title/body family (Noto Sans SC or an occasion-appropriate Chinese display face) + one Latin/numeric family (e.g. Archivo / IBM Plex Sans) + at most one on-topic accent family (calligraphy/handwriting/display, only when the theme truly needs it). Formal decks should converge to **1–2** families. Fewer-but-uniform beats a new look on every page.
- Chinese body, tables, chart labels, footers, eyebrows, and page numbers always use `--font-sans` (or `--font-serif` for rigorous serif contexts). **Never put Chinese in `--font-mono`** — IBM Plex Mono has no Chinese glyphs, so Chinese falls back to whatever CJK font the system has (often a cartoon/handwriting face). `--font-mono` serves only pure-Latin code, coordinates, APIs, and real serial numbers.
- Match font personality to the occasion: serif for editorial, cultural, or reflective work; sans for technology, business, teaching, and dense data; mono only for controlled technical accents.
- Keep a Chinese sentence, title, conclusion, button, or label in one family. Local emphasis changes color, weight, size, or decoration—not the font family.
- Treat playful, rounded, handwritten, and calligraphic CJK fonts as scene-specific roles rather than global defaults or global bans. Children, comics, craft, classroom, personal journal, travel, and explicitly calligraphic directions may activate them; government, legal, medical, formal academic, and conservative business decks normally should not. Mark each intentional use with `.is-expressive-type` or `data-type-intent="expressive"`.
- Use Noto Sans SC or Noto Serif SC for informational Chinese headings and as the delivery fallback. Do not use a Latin display face, mono face, or novelty CJK face as an implicit fallback for Chinese glyphs.
- Chinese eyebrows, departments, sources, footers, and metadata use sans/serif tracking near `0–0.03em`; Latin all-caps tracking and mono typography do not apply to Chinese labels.
- Keep Chinese and Latin weight visually balanced. Avoid thin CJK weights on projection screens.
- Do not simulate bold or italic when the selected face lacks the required style.
- Verify uncommon names, symbols, and multilingual glyphs in final PNG pixels.
- Package only glyphs actually used by the deck when subsetting is enabled. Keep the license file beside bundled fonts.

## 4. Common pairings

- Research/institutional: Noto Serif SC display + Noto Sans SC body + Archivo numbers; use mono only for genuine technical notation.
- Technology/data: Space Grotesk, Sora, or Archivo display + Noto Sans SC body + IBM Plex Mono technical accents.
- Modern editorial/experimental: Unbounded or Syne Latin display + Noto Sans SC Chinese/body + Archivo numbers and compact Latin labels.
- Editorial/magazine: Fraunces or Playfair Display Latin display + Noto Serif SC Chinese title + Noto Sans SC body.
- Culture/travel: Ma Shan Zheng for one short hero, Xiaolai or LXGW WenKai for chapters/quotations, and Noto Sans SC for body copy.
- Children/comics/craft: ZCOOL KuaiLe display + Noto Sans SC body + Xiaolai or Patrick Hand annotations.

Final render review, not CSS inspection, decides whether the pairing is readable and correctly loaded.
