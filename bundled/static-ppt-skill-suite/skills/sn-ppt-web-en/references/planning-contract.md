# Global planning and page handoff contract

## Reading order

1. Finish `plan/grounded-knowledge.md`.
2. Finish `plan/design-brief.md#Style Lock`.
3. Write `plan/deck.md` completely.
4. Write every `plan/slide_NN.md` completely.
5. Run planning preflight and `deck.py prepare` before delegating images or pages.

## 1. `plan/deck.md`

Use this structure:

```markdown
# Deck plan
## Resolved brief
- speaker:
- audience:
- occasion:
- objective:
- duration:
- page_count:
- response_language:
- deliverable_language:
- core_takeaway:
- assumptions:

## Narrative arc
| Page | Role | Non-substitutable takeaway | Evidence/visual | Transition |
| --- | --- | --- | --- | --- |

## Visual storyboard
| Page | Canvas state | Title anchor | Direction | Primary medium | Image share | Density | Motif role |
| --- | --- | --- | --- | --- | --- | --- | --- |

## Special pages
- cover:
- dividers:
- closing:

## Production groups
### <group_id>
- pages: NN,NN
- affinity: <shared production method and composition>
- design_dna: <stable grammar>
- page_variations: <deliberate changes>
- visual_beat: <progression>
- boundary_handoff: <entry, internal, and exit state>

## Repetition & rhythm preflight
## Sources and evidence policy
## Delivery checklist
```

### Production groups

Group by production method and compositional affinity first, narrative continuity second, chapter membership last. Put cover and closing in `bookends`; put all chapter transitions in `dividers`. Every page belongs to exactly one group. A single-page group is valid. Do not split a frozen group merely to increase concurrency.

`boundary_handoff` must describe the canvas family, lightness, color/image field, motif state, and direction entering the group, across its pages, and leaving it.

### Repetition and rhythm preflight

Compare every page across these independent axes: canvas state, title anchor, direction, primary medium, image share, information density, whitespace location, and motif role. Repetition is intentional only when it supports a sequence; otherwise change composition, not just color or copy.

## 2. `plan/slide_NN.md`

Use this exact semantic structure:

```markdown
# Slide NN — <title>

## Page direction
- role:
- audience job:
- first focal point:
- reading path:
- non-substitutable takeaway:
- transition from previous / to next:
- production_group:
- page_type:
- canvas_state:
- spatial_budget: sparse | balanced | dense

## Final on-screen copy
- kicker:
- title:
- subtitle:
- body / data / labels:
- takeaway:
- source line:

## Visual implementation
- image_opportunity: **a single machine-readable enum only, with no explanation** — `real_required` / `generated_ok` / `none` / `chart_only` / `canvas_only` / `typography_only` (`none` and `*_only` are read as no-bitmap). The startup gate and final acceptance parse only this enum line; put the reason on the next `image_opportunity_reason` line, never on this one.
- image_opportunity_reason: one-line human reason. Named real people/works/products/processes default to a real-image opportunity (retrieve portraits/official shots) — "no photo on hand" / "CSS is safer" / "style unity" are not sufficient reasons for `none`.
- primary_medium: real-image | generated-image | echarts | canvas | typography
- composition:
- background_treatment:
- motif_role:
- image_or_asset_ids:
- crop_contract:
- visual_acceptance:

## Image briefs
- asset_id:
  purpose:
  subject:
  medium:
  presentation: subject-only | framed-scene | full-bleed | evidence-crop
  aspect_ratio:
  palette_and_mood:
  crop_contract: fit=...; focal=...; protect=...; allowed=...; object_position=...

> `presentation` is the bitmap **render / background-handling contract** and may
> ONLY be one of the four enums above.  Required on a bitmap page; **omit the line
> entirely on a no-bitmap page** (never write `none` / `not-applicable` / a CJK
> placeholder).  ⛔ `split-media` / `right-half` / `cards` are **layout/arch**, not
> presentation — put those under the layout/composition field.  A real photo used
> in a left/right split is `presentation: framed-scene` (the split is layout).
> `subject-only` also requires `subject_only: true` in the image brief.

## Reference route
- references/<file>.md#<section>

## Sources
- <internal traceability field, for speaker-notes reference and self-check only; **never on screen**. Record the source for each on-screen fact, or user-provided / none. File paths / section anchors (e.g. `research/research.md §2.4`) stay here only, never printed as a page source>

## Speaker notes
- <spoken explanation, evidence boundary, and transition>
```

Visible copy must be exact and audience-facing. Do not place production metadata, file paths, role names, evidence IDs, assumptions, page responsibilities, asset instructions, or speaker-only content under `Final on-screen copy`.

## 3. Page-type selection

- Cover: establish the object, question, or thesis with one dominant visual action.
- Divider: mark a narrative turn with chapter state, promise, and visual counterweight.
- Closing: resolve the opening thesis; do not turn it into references or a dense recap.
- Evidence page: show the decisive image, quote, table, or chart at meaningful scale.
- Comparison page: keep dimensions and baselines aligned.
- Process/mechanism page: make direction and causality explicit.
- Data page: state the conclusion and preserve category/value/source integrity.
- Editorial/quote page: use typography and evidence deliberately rather than as filler.

## 4. Image, chart, and diagram briefs

Every asset brief needs a stable `asset_id`, real purpose, subject, medium, presentation type, aspect ratio, visual recipe, and crop contract. Identity-bearing subjects must request real imagery. A `subject-only` brief requires an alpha cutout. A named paper Figure must specify source page, panel boundary, and `figure-crop` rather than a page screenshot.

Every chart brief must define categories, series, values, units, time basis, source, and intended conclusion. Every Canvas brief must define nodes, relationships, direction, hierarchy, and which labels remain in HTML.

## 5. Special-page contract

Cover, divider, and closing pages use the full canvas and one primary design action. They do not inherit normal title/body/footer furniture. Remove page numbers, runtime labels, fake archive codes, and duplicate title variants unless they add real audience information.

## 6. Planning gate

Planning freezes only when:

- every fact is grounded, explicitly assumed, illustrative, conflicting, or unresolved;
- every page has a unique audience takeaway and enough evidence or explanation;
- exact visible copy, speech, sources, medium, asset paths/briefs, page type, and visual acceptance criteria are complete;
- every page belongs to one Production group with a boundary handoff;
- the repetition preflight shows deliberate rhythm rather than template repetition;
- attachment must-show decisions and paper Figure routes are explicit;
- page count, numbering, closing, references, and delivery scope agree.
