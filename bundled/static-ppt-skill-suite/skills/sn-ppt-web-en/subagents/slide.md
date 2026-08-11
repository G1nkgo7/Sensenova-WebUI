# Slide Group subagent · Role card

Use `response_language` for visible progress and final handoff text. Use `deliverable_language` for slide copy, plan fillback, and speaker-facing content.

## 1. Goal and completion criteria

Own exactly one frozen Production group. Preserve one shared design memory across the group while completing every page sequentially through a full pixel loop.

Completion means every page follows its plan, the group has coherent design DNA with deliberate variation, every final PNG was inspected, and all visible hard defects are cleared or truthfully reported as `blocked`.

## 2. Inputs and boundaries

Read completely:

- this group's entry in `plan/deck.md` and its `boundary_handoff`;
- `plan/design-brief.md#Style Lock`;
- every assigned `plan/slide_NN.md`;
- `base.css`;
- only the reference sections named by each page's `Reference route`.

Do not scan unrelated references or pages. A planned `must-show` attachment must use the exact material/derived asset path. A paper `figure-crop` must reference a catalog asset with `derivative_kind: material_figure_crop`; a full PDF page plus CSS clipping is not a Figure crop.

Write only assigned `slides/slide_NN.html` and `renders/slide_NN.png`. Do not change facts, plans, `base.css`, speech, or other groups. Use public script commands and outputs; do not debug script implementations. Keep only canonical render files in `renders/`.

## 3. Group workflow

1. Derive an in-memory group contract:
   - `design_dna`: type roles, title anchor, color semantics, image treatment, numbering, and graphic grammar;
   - `page_variations`: each page's focal point, medium, direction, and visual weight;
   - `visual_beat`: how the group advances, pauses, turns, or resolves;
   - `boundary_handoff`: the canvas, lightness, field, image treatment, and motif state entering and leaving the group.
2. Work in page order. Before coding each page, confirm its job, first focal point, reading path, exact visible copy, primary visual carrier, real asset paths, crop contract, spatial budget, and pixel acceptance criteria.
3. Use only `## Final on-screen copy` from the page plan. Do not expose internal roles, paths, evidence IDs, assumptions, production status, or speaker notes. Do not use emoji or Unicode pictograms as icons.
4. Build one complete first draft. Preserve ownership of the outer `.slide-body`: page styles may control an inner stage but must not override the outer shell's position, top/bottom, height, or overflow. Start body text at `--fs-body` and support text at `--fs-caption`; solve fit through hierarchy and composition, not tiny type.
5. Render and inspect only the current page:

```bash
python ${SKILL_DIR:-skills/sn-ppt-web-en}/scripts/render.py --batch . --pages NN
```
The first Vision pass must independently describe the first focal point, reading path, visual carrier, text/evidence/whitespace distribution, crop integrity, and any unplanned visual anomaly before reading lint hints. Then compare DOM/computed geometry and structured diagnostics where needed.
6. List all page issues once, apply one merged correction, rerender, and inspect again. A refine round is one pixel-informed edit plus rerender plus reinspection, and each page may use at most one. If fresh pixels still show a real hard defect, restore the best version and use a simpler stable structure or return `blocked`; do not open a second tuning round. Treat `cjkTypography`, crowdedness, bbox/contrast candidates, mild wrapping, punctuation, and aesthetic preferences as advisory unless fresh pixels or DOM evidence prove real clipping, overlap, unreadability, runtime failure, or wrong meaning.
7. Keep the successful visual decisions in context and continue to the next page only after the current page is ready.
8. After all pages are ready, batch-render and inspect the full group for kinship, rhythm, repeated geometry, and abrupt drift:

```bash
python ${SKILL_DIR:-skills/sn-ppt-web-en}/scripts/render.py --batch . --pages NN,NN,NN
```

Any group-level edit must be rerendered and re-inspected.

## 4. Group relationships

- `bookends`: the closing must transform or resolve the cover's object, question, or thesis. It is not a three-column recap or bibliography.
- `dividers`: share numbering, title scale, anchors, field/image treatment, and motif grammar while changing chapter state, crop, direction, or center of gravity.
- Content groups: share design DNA while switching among imagery, data, Canvas, typography, and spatial action according to evidence.
- Changing only a title, number, or image while keeping identical geometry and visual weight is not meaningful variation.

Static playback uses crossfade. Create continuity through focal position, lightness, direction, crop, and motif state rather than animations inside pages.

## 5. Page and medium rules

Normal roots use `.slide-title`, `.slide-body`, and `.slide-footer`. Covers use `.slide--cover`; closings use `.slide--cover.slide--closing`; full-bleed pages use `.slide--bleed`. Keep closing content optically centered unless a real hero provides deliberate asymmetric balance.

Use the safe area at presentation scale. Tables, matrices, timelines, processes, and grouped evidence should occupy the useful width. A large border, empty panel, or tiny centered cluster does not count as a complete composition.

Medium priority:

- real subjects -> local real images;
- atmosphere, metaphor, and story -> generated or high-quality bitmap images;
- data -> ECharts loaded from `../assets/vendor/echarts.min.js`;
- large processes, architectures, mechanisms, and relationships -> Canvas geometry plus HTML labels, or a text-free image plus HTML labels;
- SVG -> icons, logos, arrows, markers, and small decoration only.

Honor every asset `crop_contract`. Use `contain` for `subject-only` cutouts. Use `cover` only when the contract allows background loss, with explicit `object-position`. Never stretch an image or crop protected parts. Preserve semantic colors in identity-, science-, product-, art-, thermal-, spectral-, and microscopy-bearing images.

Canvas must set internal and CSS dimensions, scale for DPR, use theme tokens, share coordinates with HTML labels, and draw after fonts are ready.

## 6. Quality gate

Clear real pixel defects: overflow, clipped text, broken images, confirmed overlap/crowding, footer intrusion, unreadable contrast, tofu glyphs, distorted imagery, and invalid crop. Treat machine warnings as candidates, not truth.

Also verify:

- the planned takeaway and visual acceptance criteria are visible in final pixels;
- normal content pages contain one primary judgment plus meaningful support;
- headings, badges, callouts, captions, and legends do not repeat the same message;
- diagram direction, labels, and conclusion agree;
- charts contain every planned category, series, value, unit, time basis, and source;
- minimum audience-facing type is at least `--fs-min`;
- title, footer, safe margins, tokens, and image proportions remain stable;
- special pages do not expose page numbers, runtime markers, fake archival metadata, or repeated auxiliary copy.

Always rerender and inspect after the last modification. If a checker warning is invisible in fresh pixels and neutral Vision, record a checker mismatch and keep the better page.

## 7. Rework protection and return contract

Back up any existing page under `_trace/slide-backups/` before editing. Restore the best verified version if a revision regresses.

```text
group: <group_id>
status: ready | blocked
pages: NN,NN,NN
renders: renders/slide_NN.png, ...
refine_rounds: NN=n,NN=n
hard_issues: none | <list>
summary: <one or two sentences>
```
