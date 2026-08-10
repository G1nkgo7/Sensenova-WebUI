# Quality checklist

Use this checklist after every render and again before delivery. Automated lint finds candidates; fresh pixels and the DOM determine whether a defect is real.

## 1. Single-page review

### Content and expression

- The page has one clear job and one dominant takeaway.
- Titles state a conclusion or useful question instead of naming a vague topic.
- Claims, numbers, quotes, names, dates, and sources are accurate.
- Copy is concise enough for a presentation and contains no placeholders or internal production notes.
- Repeated labels, decorative metadata, and redundant subtitles are removed.

### Visual semantics

- The chosen medium fits the content: imagery for atmosphere or identity, ECharts for quantitative data, Canvas plus HTML labels for large diagrams, and HTML/CSS for ordinary structured text.
- Every image, chart, and diagram contributes evidence or meaning.
- Generated or searched imagery matches the planned subject, crop, palette, and intended use.
- Charts have readable axes, legends, labels, units, and a clear highlighted series.
- Diagrams expose the intended relationship at a glance and avoid decorative complexity.

### Layout and reading path

- The title, body, visual, and footer obey the deck grid and safe margins.
- The reading order is obvious within three seconds.
- Repeated units share alignment, spacing, and internal structure.
- The page has a stable visual center; accidental empty bands and crowded corners are fixed.
- No text, image, or decoration overlaps, clips, or escapes its intended container.
- A special cover, divider, hero, or ending page is deliberately different without breaking the deck system.

### Typography and contrast

- Body text, captions, chart labels, and diagram labels remain readable at presentation distance.
- Normal text has at least 4.5:1 contrast; large text has at least 3:1.
- Text over imagery uses a scrim, backplate, or low-detail placement area.
- Long titles wrap at semantic boundaries and never leave a one- or two-character orphan.
- Chinese and Latin type pairings are intentional and all referenced fonts are available to the delivery pipeline.

### Technical checks

- The page renders at the required canvas size with no console error.
- `render.py` reports zero `CJK-TYPE` defects: no mixed-family Chinese sentence, accidental mono/wide tracking, or unmarked novelty CJK font.
- All local assets resolve and no external placeholder URL remains.
- The rendered PNG is fresh and corresponds to the current HTML.
- Browser-only effects degrade safely in headless Chromium.
- A page transition, if present, does not hide or distort the final state.

## 2. Full-deck review

### Narrative

- The opening establishes context and promise.
- Sections progress logically; every page earns its place.
- Dense analysis alternates with breathing or anchor pages where appropriate.
- The ending resolves the story instead of merely repeating the agenda.

### System consistency

- The deck uses one resolved visual system: palette, typography, grid, image treatment, line language, and footer behavior.
- Repeated page roles are related but not mechanically cloned.
- Page titles share a stable anchor and hierarchy.
- Visual motifs recur as meaningful variants, not stickers.
- Sources and speaker notes map to the correct pages.

### Production integrity

- Every planned page has current HTML and PNG output.
- `speech.md`, `present.html`, and asset manifests match the final page count and order.
- Every review issue is either fixed or explicitly accepted with a reason.
- Final delivery contains only required files and portable dependencies.
- Fonts included in the package are limited to the families and glyphs needed by this deck when subsetting is available.

## 3. Automated lint interpretation

Treat these as hard defects when confirmed by fresh pixels or DOM evidence:

- `BROKEN-IMAGE`
- real `OVERFLOW` or `TEXT-OVERFLOW-BOX`
- real `FOOTER-PUSHED` or `FOOTER-COVER`
- missing slide files, stale renders, console errors, or a page-count mismatch

Treat these as review candidates rather than automatic failures:

- `OVERLAP` and `DECOR-OVERLAP`
- `INNER-GAP`, `WIDOW-LINE`, and `IMG-LONELY`
- `VBALANCE` and `COVER-OOB`
- transparent bounding-box collisions and intentional layered compositions

Never damage a good composition merely to silence a heuristic. Verify the actual pixels, identify the root cause, and make the smallest correction that improves the audience-facing result.

## 4. Final acceptance

The deck is ready only when all of the following are true:

- content fidelity: pass
- final pixels inspected: yes
- critical visual defects: none
- page count and order: correct
- speaker notes and sources: synchronized
- `present.html`: built and playable
- delivery package: portable
