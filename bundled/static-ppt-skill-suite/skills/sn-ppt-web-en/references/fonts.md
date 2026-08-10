# Font system: verifiable open fonts and user-authorized fonts

## 1. License boundary

Use only fonts that are bundled with a redistribution license, installed by this Skill's font installer, or explicitly supplied and authorized by the user. Do not assume a locally installed commercial family may be packaged in delivery artifacts.

`materials/font-config.json`, when present, is authoritative for title, body, number, mono, and annotation roles. Keep the original user font files under materials and let the deterministic font bundler create delivery subsets.

## 2. Role tokens and safe fallbacks

| Role | CSS token | Recommended open families | Purpose |
| --- | --- | --- | --- |
| Display/title | `--font-display` | Noto Sans SC, Noto Serif SC, Source Han Sans/Serif where licensed | Covers, dividers, large statements |
| Body | `--font-body` | Noto Sans SC, Noto Serif SC | Paragraphs, tables, labels |
| Numbers | `--font-number` | Inter, IBM Plex Sans, Archivo | KPIs and numeric hierarchy |
| Mono/data | `--font-mono` | IBM Plex Mono, JetBrains Mono | Code, coordinates, technical labels |
| Annotation | `--font-annotation` | Body or mono role | Captions, sources, notes |

Always include a generic fallback at the end of a stack. A family name alone does not prove the font exists or is distributable.

## 3. Usage rules

- Activate role tokens explicitly in `base.css`; do not rely on browser defaults.
- Use at most two primary families plus an optional mono family.
- Match font personality to the occasion: serif for editorial, cultural, or reflective work; sans for technology, business, teaching, and dense data; mono only for controlled technical accents.
- Keep Chinese and Latin weight visually balanced. Avoid thin CJK weights on projection screens.
- Do not simulate bold or italic when the selected face lacks the required style.
- Verify uncommon names, symbols, and multilingual glyphs in final PNG pixels.
- Package only glyphs actually used by the deck when subsetting is enabled. Keep the license file beside bundled fonts.

## 4. Common pairings

- Research/institutional: Noto Serif SC display + Noto Sans SC body + IBM Plex Mono data.
- Technology/data: Archivo or Inter display + Noto Sans SC body + IBM Plex Mono data.
- Editorial/magazine: Noto Serif SC display + Noto Sans SC body.
- Youthful brand: heavier Noto Sans SC display + Noto Sans SC body + expressive color rather than novelty fonts.

Final render review, not CSS inspection, decides whether the pairing is readable and correctly loaded.
