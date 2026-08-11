---
name: sn-ppt-web-en
description: Create or edit complete static HTML slide decks from a topic, brief, document, or multiple attachments. Produce one 1600x900 HTML file and render per slide, a page-aligned speaker script, and a portable player. Orchestrate Research, Material, Image, Slide, and Review agents for new decks; route edits to a single Review quick-fix or an Orchestrator-led multi-agent revision according to actual impact.
---

# sn-ppt-web-en

This is the frozen English instruction package for the `sn-ppt-web` workflow. Read it as a route map, not as a request to preload every reference. Select a task mode first, then read only the role cards and references required by that route.

## 0. Select exactly one task mode

| User goal | Mode | Route |
| --- | --- | --- |
| Create a new deck from a topic, brief, or attachments | New deck | Section 2 |
| Change a small number of pages without changing narrative, facts, assets, or the global system | Simple edit | Section 3.2 |
| Change narrative, facts, assets, global style, or multi-page structure | Complex edit | Section 3.3 |

If the impact is unclear, perform a read-only impact analysis first. Do not classify a narrative-level change as simple merely because the user calls it small or it touches few pages.

## 1. Contracts shared by all modes

### 1.1 Orchestrator scope

The Orchestrator only **selects the mode, plans, delegates, merges, validates, and performs deterministic finalization**.

- It may write `plan/`, `base.css`, grounded knowledge, and build artifacts.
- It must not directly write `slides/slide_NN.html`; Slide or Review agents own page HTML.
- Every subagent needs an explicit label. A failed, timed-out, or unnaturally terminated result is not a completed deliverable.
- Treat each delegated structured contract, artifact path, and `handoff_path` as the parent-facing source of truth. Do not poll child `messages.json`, `tool_log.json`, or system snapshots.
- Research and Review are task-level singletons. Do not create `_r2` or `_r3` agents to bypass a failed singleton.

### 1.2 Roles

| Role | Count and timing | Sole responsibility |
| --- | --- | --- |
| Research | At most one | Verify external facts that can change conclusions; write `research/research.md` |
| Material | Parallel by non-overlapping attachment shard | Fully parse assigned attachments; write `research/materials/material_NN.md` |
| Image | Parallel by coherent asset group | Obtain or generate bitmap assets and return real local paths |
| Slide | Parallel by design-affinity Production group | Build only its page group and close the per-page pixel loop |
| Review | At most one per task | Diagnose, centrally fix, rerender, verify final pixels, and close the speaker script |

Before work, every selected role reads its complete `subagents/<role>.md` role card. Continue from any truncation offset until EOF. Do not read unrelated role cards or references.

### 1.3 Language and capability lock

Lock these values before production:

- `response_language`: language for visible progress and final responses;
- `deliverable_language`: language for on-slide copy, planning artifacts, and speaker notes;
- availability of attachments, web retrieval, real-image search, image generation, rendering, Vision, and file writing.

By default use the primary language of the user query. An explicit requested delivery language overrides only `deliverable_language`. Never plan a capability that is unavailable. `vision_analyze` uses the same model as the active role. Its judgments and issue ledger use `response_language`; quotations and proper nouns may stay in their source language.

### 1.4 Workspace sources of truth

```text
research/research.md
research/materials/material_NN.md
plan/grounded-knowledge.md
plan/design-brief.md
plan/deck.md
plan/slide_NN.md
base.css
assets/
slides/slide_NN.html
renders/slide_NN.png
speech.md
present.html
```

- `grounded-knowledge.md` is the factual source of truth.
- `design-brief.md` is the visual source of truth.
- `slide_NN.md` is the page content contract.
- `base.css` is the global design system.
- Final quality judgments use fresh PNG pixels, not HTML inspection alone.

### 1.5 Progressive reference routing

| Situation | Read |
| --- | --- |
| Scene and art direction | `design-rules.md` sections T1–T3 and 1–3 plus the matched theme section; the `design-styles.md` contents and one style family |
| Global and per-page planning | `planning-contract.md`; only the matching page type in `layout-patterns.md` |
| Editing an existing deck | `editing-contract.md` |
| Slide production | Its page plans, `base.css`, the single-page section of `quality-checklist.md`, and routed sections only |
| Final Review | All of `quality-checklist.md` |

Read `fonts.md` only when default role selection is insufficient or the occasion is font-sensitive. Do not scan every design, style, layout, and font document before work begins.

Regardless of whether `fonts.md` is opened, Chinese eyebrows, department names, footers, sources, and metadata must use `--font-sans` or `--font-serif` with `0–0.03em` tracking. Reserve `--font-mono`, `--tracking-caps`, and `.is-latin-label` for genuinely Latin technical labels, code, coordinates, and identifiers.

Keep every Chinese title, conclusion, button, or short label in one font family. Emphasize words with color, weight, size, or an underline—not by switching a few characters to a cartoon, handwritten, or second display family. Let the occasion and Style Lock choose the system: expressive covers, heroes, and dividers may coordinate three or four clearly assigned roles, while formal decks should converge on two or three. Do not flatten every deck into the same sans/serif pair. Playful, rounded, handwritten, and calligraphic CJK fonts remain opt-in when the topic and audience genuinely support them. Mark each intentional use with `.is-expressive-type` or `data-type-intent="expressive"` so deterministic review can distinguish art direction from an accidental fallback.

### 1.6 Visual medium priority

1. Real people, places, products, and events: real photography.
2. Atmosphere, metaphor, story scenes, and hero visuals: generated or high-quality bitmap imagery.
3. Data: ECharts.
4. Large processes, architectures, mechanisms, and relationship diagrams: Canvas geometry plus an HTML text layer, or a text-free image plus HTML labels.
5. SVG: icons, logos, arrows, markers, and small decoration only.

Do not use hand-authored SVG as the default half-page or full-page main visual unless the user explicitly requires vector delivery or provides an exact vector asset.

When a named film, animation, game, artwork, or identifiable character carries recognition, character, shot-analysis, or evidence duties, retrieve official stills, posters, character sheets, production material, or credible editorial imagery first. Use generation for atmosphere, emotion, transitions, and non-evidentiary concepts. Copyright/IP alone is not a reason to omit imagery, but do not keep paraphrasing a request after the provider explicitly rejects it.

## 2. New deck workflow

### Phase 0 — Parse the task

1. Lock languages, capabilities, attachment inventory, and delivery scope.
2. Create an internal scene card: Speaker, Audience, Occasion, Objective, Duration, Page count, Screen versus speech, Core takeaway, and Assumptions.
3. Do not ask about non-blocking preferences. Record reasonable assumptions in planning artifacts.

### Phase 1 — Ground only what is needed

#### Material

When attachments exist, divide them into non-overlapping Material shards, normally one attachment per shard. Every goal includes response and delivery languages, `assignment_id`, exact input paths, its isolated work directory, and its own output file.

Every format enters through `scripts/stage_materials.py`. Read native text in full; actually inspect images; preserve both extracted text and page/embedded-image visuals for PDF and Office files. Use available conversion capabilities for media, archives, and unknown formats. File metadata, archive member names, and representative frames do not count as semantic coverage.

After all Material results return, verify the catalog item by item. Every attachment needs one `coverage_id`, `status: ok`, `coverage: complete`, continuous full text chunks or complete page coverage, and a matching coverage-ledger entry. Any incomplete, truncated, unsupported, failed, or missing semantic coverage blocks downstream work.

#### Research

Delegate the single Research agent only when external facts can change a conclusion. Named real products, clinical or business statistics, external benchmarks, and conclusion-bearing numbers require verification unless the user or attachments already supplied them.

The goal must contain:

```text
Research:
Response language: <response_language>
Deliverable language: <deliverable_language>
Raw user query (verbatim): <complete original query>
Unresolved terms: <targets or none>
Evidence needed: <gaps that could change a conclusion>
Parent interpretations are hypotheses, not user claims.
```

Copy the raw query verbatim. New candidates added by the Orchestrator must be labeled as hypotheses, never as user claims. Research should submit independent first-pass queries in one tool round, extract the best sources in the next, and use at most one targeted follow-up round.

#### Grounding gate

Immediately after Material and Research return, write the sole `plan/grounded-knowledge.md`, then read it back and verify completeness. Do not begin art direction or page planning before this gate.

Separate user facts, externally verified facts, Orchestrator assumptions, illustrative values, conflicts, and unresolved items. Propagate every unresolved boundary from a partial Research contract. Do not promote a hypothesis into a user claim.

Attachments constrain facts and reusable assets, not the quality ceiling. Record `material_visual_mode` as `facts-only`, `visual-reuse`, `style-reference`, or explicit `faithful-restyle`. For every image attachment, record an `attachment_visual_map` decision: must-show, reuse, reference-only, or omit; destination page; asset type; and treatment. Distinguish a paper page facsimile from a cropped named Figure.

### Phase 2 — Scene direction and Style Lock

1. Read only routed visual references.
2. Lock one `scene_register`, one primary style, and at most one supporting craft. The style must explain why it fits the audience, occasion, and content.
3. Write `plan/design-brief.md#Style Lock` with:
   - scene, primary style, and supporting craft;
   - visual thesis and signature visual;
   - palette, typography, and numeric voice;
   - image language, image opportunity map, and composition grammar;
   - a coherent `background_system` and `base_canvas_family`, with controlled states and narrative entry/exit rules;
   - `motif_role`, including where the motif leads, supports, or intentionally disappears;
   - special pages, avoid list, spatial rhythm, and special-page system;
   - attachment visual mode and map when applicable.

Style Lock defines visual language and decision boundaries, not a fixed HTML template. Keep typography roles, color semantics, spacing rhythm, image treatment, and graphic grammar stable while varying focal point, direction, medium proportion, information density, whitespace location, and chapter state.

Perform a subject-first image-opportunity scan before choosing implementation. Named real people, products, works, interfaces, places, and events usually create real-image opportunities. Do not replace identifiable real subjects with anonymous generated lookalikes. Generated imagery is appropriate for atmosphere, visions, fictional characters, unbuilt spaces, and conceptual scenes; accurate facts and labels remain in HTML or charts.

When image search or generation is available, an all-`none` deck—or a deck with only one cover image—is an exception that requires evidence, not the default safe route. Data, business, technical, academic, and code-heavy topics may keep factual slides in charts or Canvas, but should still choose at least two image-worthy moments among the cover, chapter transitions, cases, scenarios, vision, or conclusion and provide executable asset briefs; a short deck still needs at least one content moment beyond the cover. An image-free deck is allowed only when the user explicitly requests pure typography/charts or when every slide explains why a bitmap would reduce accuracy or readability, with those reasons recorded in `plan/deck.md`. This is a minimum guard against false negatives, not a decorative quota; separate factual real imagery from non-evidentiary generated atmosphere.

Every normal content slide needs one substantial primary visual carrier: a real/generated image, chart, explanatory Canvas, or intentionally composed typographic visual. Borders, empty panels, tiny icons, and decorative lines are not primary visual carriers. Backgrounds should form one coherent family rather than either a random reskin or a uniform flat-color fallback.

Continue only through this dependency chain:

`Style Lock -> complete plan + prepare -> parallel Image groups -> one asset-path fillback -> parallel Slide groups -> single Review -> build`

### Phase 3 — Global planning and font preflight

Read all of `references/planning-contract.md`. If `materials/font-config.json` exists, treat its title/body/number/annotation assignments as Style Lock inputs. User-authorized fonts take priority; do not substitute an unprovided local font merely by family name.

Then:

1. Complete `design-brief.md`.
2. Write `plan/deck.md`.
3. Copy `references/base-template.css` to `base.css` and fill its tokens.
4. Write all `plan/slide_NN.md` files, each with its Reference route.
5. Define Production groups by **production method and compositional affinity** first, narrative continuity second, and chapter membership last. Put all divider pages in `dividers`, and cover plus closing in `bookends`. Every slide belongs to exactly one group. Give each group a `boundary_handoff`.
6. Keep references and closing pages separate. Closing resolves the presentation; it is not a bibliography or dense recap.
7. Add a `Repetition & rhythm preflight` comparing canvas state, title anchor, direction, medium, image share, density, and motif role across all pages and chapters.
8. Check content sufficiency and visible semantic duplication. Every normal page must provide a non-substitutable audience takeaway plus suitable evidence, mechanism, comparison, example, action, or boundary. Merge or redesign thin pages instead of stretching them with empty containers.
9. Apply a screen-copy firewall: production metadata, file names, evidence IDs, internal labels, assumptions, and speaker-only content do not appear on slides. Do not use emoji or Unicode pictograms as slide icons.
10. Prepare the workspace deterministically:

```bash
python ${SKILL_DIR:-skills/sn-ppt-web-en}/scripts/deck.py prepare . --expected <slide-count>
```

Planning freezes only when facts, sequence, exact visible copy, media, image opportunities, asset briefs, backgrounds, sources, speech, page types, fonts, spatial budgets, and pixel acceptance criteria are complete and mutually consistent.

### Phase 4 — Assets and page production

1. Aggregate all real/generated image briefs and delegate coherent Image groups. Every goal includes stable `group_id`, response language, delivery language, asset purposes, preferred source/fallback order, and plan paths. Do not paste a batch of generation prompts into the goal or turn a frozen real-image-first route into “generate everything.”
2. Register must-show or reusable attachment images first. Bind each candidate to a stable `asset_id`, make an asset contact sheet, inspect the group once with Vision, and write back the decision. A `subject-only` asset requires a verified real alpha cutout; CSS masks and blend modes are not substitutes. Retry an explicitly non-retryable generation rejection with one materially safer prompt at most, then switch to real retrieval or the planned non-bitmap fallback. When the tool asks for local-candidate inspection, complete the contact sheet and catalog decision before any more acquisition. If an asset remains unavailable, update the affected page plan to a viable fallback instead of redelegating the same image task. Before Slide starts, every image need has either a real local path or an explicit non-bitmap replacement in the frozen plan.
3. Delegate one Slide agent per frozen Production group. The first goal line must be exactly `Slide Group <group_id> [NN,NN]:`. Include languages and `boundary_handoff`. Do not split a frozen multi-page group merely to increase concurrency.
4. Within each group, complete pages sequentially: full first draft -> render one page -> inspect the PNG with Vision -> merge all confirmed hard-defect fixes -> rerender and re-inspect. A page must be ready before the next begins. Allow at most one refine round per page. If fresh pixels still show real clipping, overlap, unreadable content, runtime errors, or stale output, return `blocked`; treat `cjkTypography`, crowdedness, bbox/contrast candidates, mild wrapping, punctuation, and aesthetic preferences as advisory unless pixels or DOM evidence prove a real audience-facing defect.
5. After every group finishes and reviews its final group contact sheet, delegate the single Review agent.

### Phase 5 — Final Review and delivery

The sole Review goal begins with:

```text
Review:
Response language: <response_language>
Deliverable language: <deliverable_language>
mode=final_review
```

Review then:

1. Diagnoses the full deck before modifying anything: inspect the overview, every review-contact batch, and required focus pages; freeze one issue ledger.
2. Verifies content fidelity against grounded knowledge and Material coverage whenever attachments or Research exist.
3. Performs one centralized repair pass, rerenders the full affected scope, and inspects fresh focus pixels. Review may use at most one refine round; it must not open a second round or edit merely to silence advisory heuristics.
4. Resynchronizes the speaker script after pixels are final.
5. Returns `ready` only after final pixels were actually inspected and build succeeded.

After Review returns ready, the Orchestrator only verifies that delivery files exist. It must not rerender, rebuild, rediagnose unchanged results, or chase advisory typography/geometry/aesthetic hints. If any page changes after Review, send it back to the same Review agent for revalidation.

Build with:

```bash
python ${SKILL_DIR:-skills/sn-ppt-web-en}/scripts/deck.py build . --expected <slide-count>
```

Deliver only when Review is ready, final pixels were inspected, fonts and render freshness pass, `speech.md` aligns with pages, and `present.html` exists.

## 3. Editing a presentation

First read all of `references/editing-contract.md`. Inspect the existing plan, HTML, assets, script, and renders without changing them; build an impact map; then choose one edit route.

### 3.1 Simple versus complex

An edit is simple only if it does not change the core claim, page sequence, page responsibilities, cross-page narrative, Research/Material/Image needs, global Style Lock, font system, or multiple layout families, and can be safely completed within a small explicit page scope. Otherwise it is complex.

### 3.2 Single Review quick-fix

Delegate exactly one Review agent with `mode=simple_edit`, the verbatim instruction, target pages, and invariants. It inspects current pixels, lists the complete change set once, edits target pages, updates affected plans and speech, batch-renders target pages once, inspects the focus contact sheet, and returns ready or blocked. Do not delegate Slide, Image, Research, or another Review.

### 3.3 Orchestrator-led complex revision

The Orchestrator writes an impact map, preserves every still-valid artifact, delegates only missing Research/Material/Image work and complete affected Slide groups, updates affected plans, runs `deck.py prepare`, rerenders the appropriate scope, and delegates the single final Review. It never directly edits page HTML.

### 3.4 Edit delivery gate

- Every requested change maps to a visible result.
- Unaffected pages and assets remain unchanged.
- Old and new pages share style, numbering, speech, and player behavior.
- Fresh final pixels were inspected for every changed page.
- Font bundle, render freshness, `speech.md`, and `present.html` pass again.

## 4. Hard red lines

- Use ECharts for data charts; never fabricate a chart as a generated image.
- Generated imagery must not carry accurate text, dates, addresses, brand names, or numeric facts; put those in HTML.
- SVG is for small elements, not large diagrams or hero visuals.
- Keep only canonical `slide_NN.html` files in `slides/`.
- The fixed page skeleton, footer safe area, minimum type size, contrast, and zero visible overflow are hard gates.
- Never expose internal planning labels, file paths, production status, or valueless pseudo-metadata on slides.
- Final judgment uses fresh PNG pixels. Never claim completion after an unrendered or uninspected edit.
- Only one Review instance is permitted per task; do not bypass blocked status with a replacement Review.

## 5. Deterministic scripts

| Script | Purpose |
| --- | --- |
| `stage_materials.py` | Parse and catalog text, PDF, Office/ODF, images, media, archives, and unknown attachments with coverage evidence |
| `font_bundle.py` | Package allowlisted distributable fonts, licenses, character subsets, and freshness validation |
| `render.py` | Render one page or reuse one Chromium process for a batch; produce structured diagnostics |
| `image_cutout.py` | Inspect alpha and create a new verified subject cutout without overwriting the source |
| `deck.py` | Prepare plan/speech/fonts; register, assign, review, and contact-sheet assets; build contact sheets and the final player |
| `install.sh` | Install cross-platform runtime dependencies, fonts, and Chromium |

On first deployment, install the attachment-normalization environment, PyMuPDF, redistributable fonts, FontTools/Brotli, and Playwright Chromium with `scripts/install.sh`. If the Skill is mounted at a nondefault location, use the actual Skill root in commands.
