# Design rules

These rules define the quality floor for every generated deck. Use `base.css` tokens, the resolved Style Lock, and fresh rendered pixels as the source of truth.

## §T1 · Visual density

Assign each page a deliberate density role:

- **breathing** — one focal idea, limited copy, meaningful negative space;
- **balanced** — one primary visual plus supporting explanation;
- **dense** — structured evidence, still readable at presentation distance.

Negative space must explain hierarchy, focus, or rhythm. A large empty strip caused by a narrow container or misplaced grid is a defect. Do not fill intentional breathing space with decorative cards.

## §T2 · Contrast gate

- Normal text must reach at least 4.5:1 contrast.
- Large text must reach at least 3:1 contrast.
- Captions, chart labels, diagram labels, and source markers must remain readable.
- Do not use low-opacity gray merely to make content look refined.
- Accent color communicates priority or status; it is not a substitute for contrast.

## §T3 · Text over imagery

Text over an image must use at least one reliable protection method:

- a directional scrim;
- a solid or translucent backplate;
- placement over a verified low-detail area;
- a split composition that keeps text off the image.

Validate the final crop in the PNG. Never place essential text over a face, product detail, evidence label, or unstable high-frequency texture.

## 1 · Background and color

- Use one dominant background system per deck and controlled variants for sections.
- A secondary background must have narrative purpose, not random alternation.
- Use semantic CSS tokens; avoid isolated hard-coded hex values in pages.
- Keep one dominant color, one support color, and one signal color.
- Color changes must map to meaning such as category, status, sequence, or emphasis.
- Avoid generic indigo dashboards, rainbow card rows, and decorative gradients without subject relevance.

## 2 · Typography

- Build typography from the occasion instead of flattening every deck into one default pair. Expressive covers, heroes, and dividers may coordinate three or four stable roles—display, Chinese title, body, numeric, or one short accent—while formal decks should converge on two or three. More roles never permit mixed families inside one Chinese phrase. For a modern editorial or experimental direction, Unbounded or Syne may carry Latin display, Archivo may carry numbers and compact Latin labels, and Noto Sans SC should carry Chinese and body copy.
- Keep each Chinese sentence, title, conclusion, button, and label in one family. Emphasis changes color, weight, size, or decoration rather than switching a few glyphs to another family.
- Playful, rounded, handwritten, and calligraphic CJK type is opt-in only when the topic and audience support it. Mark intentional uses with `.is-expressive-type` or `data-type-intent="expressive"`; otherwise use a registered Noto Sans/Serif SC role.
- Chinese eyebrows, departments, sources, footers, and metadata use sans/serif with near-normal tracking. Reserve mono and wide all-caps tracking for genuinely Latin technical content.
- Keep content-page title position, family, and size stable across the deck.
- Use `clamp()` or length-aware size tiers for long titles.
- Chinese heavy display type should not use negative tracking.
- Numbers and units require controlled relative size and alignment; never rely on a raw shared baseline.
- Use `tabular-nums` for comparable numeric columns.
- Avoid manual `<br>` unless the break is semantically intentional.
- Keep names, organizations, dates, numbers plus units, and short labels from breaking.
- Do not use tiny text to rescue an overloaded layout; edit, restructure, or split the page.

## 3 · Hero and anchor pages

A hero page may break the normal grid, but it must preserve legibility and safe margins. Use one strong device:

- a giant number or phrase;
- a full-bleed image with protected type;
- a deliberate asymmetrical crop;
- a vertical title axis;
- a single memorable diagram or object.

Do not combine several dramatic devices on one page. Decorative clipping may affect nonessential display strokes only, never audience-facing information.

## 4 · ECharts

- Use ECharts for quantitative comparison, trend, distribution, or multivariate data.
- Provide explicit dimensions and initialize after the container is measurable.
- Labels, legends, and axes must use deck typography and pass the minimum-size floor.
- Highlight one primary series; reduce supporting series to neutral tones.
- Start quantitative axes at a truthful baseline unless a justified analytical reason is shown.
- Remove chart furniture that does not help interpretation.
- Render the final state without requiring hover.

## 5 · Canvas diagrams

- Use Canvas for large geometry, connections, stages, and spatial relationships.
- Use HTML overlays for precise text, numbers, and citations.
- Set explicit CSS dimensions and DPR-scaled internal dimensions.
- Read colors from CSS tokens.
- Draw background and connectors before nodes; keep labels above the canvas.
- Convert diagram coordinates and label positions through one shared coordinate system.
- Render a complete static state on load; animation may enhance but cannot be required to understand the page.

## 6 · Imagery

- Every image must have an intended use, crop contract, and provenance.
- Use `cover` only when the required subject remains intact; otherwise use `contain` or redesign the slot.
- Never stretch imagery.
- Maintain visual kinship across a section: lighting, palette, rendering style, and subject scale.
- Portraits must preserve identity and avoid text across faces.
- Do not substitute unrelated stock, AI lookalikes, or empty avatar placeholders for missing evidence.
- Small decorative images must not become postage stamps with no semantic weight.

## 7 · Layout diversity

Vary page structure according to content, not a quota. Useful families include split media, comparison, process, timeline, data focus, quote, full bleed, image sequence, and concept diagram.

Adjacent pages should not repeat the same title-plus-card-wall composition unless repetition is an intentional sequence. Diversity must remain inside the shared grid, typography, palette, and motif system.

## 8 · Density and balance

- Use safe margins consistently.
- Align parallel units and make equivalent structures equal in height where that improves scanning.
- Let content determine container height; avoid fixed-height cards that create holes or overflow.
- Do not use `margin-top:auto` to create unexplained internal voids.
- Ordinary content pages should occupy the useful canvas evenly.
- Covers and breathing pages may use large negative space when the focal point remains clear.
- If content does not fit, reduce copy, change structure, or split the page before shrinking type.

## 9 · Alignment and spacing

- Use a small number of shared alignment axes.
- Snap titles, text blocks, images, cards, and footers to the same grid.
- Use the spacing scale from `base.css`; same-level gaps must match.
- Group-internal spacing should be tighter than spacing between groups.
- Parallel modules must share internal row slots and baseline logic.
- Large indices next to smaller titles use top alignment, not baseline alignment.
- Decorative lines must align to structure and recur consistently or be removed.
- Every asymmetry needs a clear visual center and reading path.

## 10 · Minimum type size

- Body, caption, source, chart, and diagram text should normally remain at or above the deck's minimum token, approximately 18–20 px on a 1600×900 canvas.
- Secondary text still uses sufficient contrast.
- If labels do not fit, shorten them or reduce the number of items.
- Never declare an artificially tiny minimum merely to pass automated checks.

## 11 · Anti-template and anti-slop rules

Remove the following unless the subject explicitly requires them:

- emoji used as functional icons;
- fabricated metrics, coordinates, dates, catalog numbers, or system labels;
- placeholder text or empty data slots;
- repeated rounded cards with a colored strip;
- icon tiles above every heading;
- generic glow grids, glass panels, and random floating particles;
- gradient-clipped title text;
- nested cards without hierarchy;
- fake archival metadata;
- decorative em dashes and lines that carry no structure;
- repeated “centered title + equal cards” pages;
- exposed internal tool status in audience-facing slides.

Use approximately 80% mature restraint and 20% one memorable, subject-specific decision. If the subject can be replaced without changing the deck, the art direction is too generic.

## 12 · Rhythm and design concept

Write one sentence in `plan/deck.md` describing the design concept. Every major formal choice should support it.

- `anchor` pages start a section or carry a decisive point.
- `dense` pages organize evidence.
- `breathing` pages create focus and pacing.

Repeat a motif through meaningful variations, not mechanical frequency. Review the contact sheet at thumbnail scale: the deck should feel related, paced, and unmistakably about its subject.

## 13 · Rendering diagnostics

Diagnostics identify candidates, not truth. Confirm each warning against a fresh PNG and the DOM.

- Fix confirmed clipping, missing assets, footer collisions, and unreadable overlaps.
- Allow intentional layering when information remains legible.
- Transparent line boxes and decorative bounding boxes can produce false overlap warnings.
- Never remove useful content, shrink a hero image, or add an awkward backplate solely to silence a heuristic.
- After any change, rerender the affected page and inspect the new pixels.
