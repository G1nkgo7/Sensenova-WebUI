---
name: sn-ppt-web-en
description: Create or edit complete HTML slide decks (fast variant — the new-creation flow does NOT run a full-deck final Review); supports themes, outlines, documents, and multi-attachment inputs, generating independent HTML per page (1600×900), rendered images, page-by-page presentation scripts, and a player. For new creations, orchestrates Research, Material, Image, and Slide, then delivers directly (skipping final_review); for editing existing PPTs, selects either single Review quick-fix or Orchestrator multi-agent overhaul based on complexity. Applicable for fast producing, revising, extending, reordering, and unifying styles of PPTs, decks, slides, and presentations.
---

# sn-ppt-web-en

Treat this document as a **roadmap**, not as a complete set of specifications to be memorized all at once. First determine the task mode, then read only the references and responsibility cards required by that path.

## 0. Choose Task Mode First

| User Goal | Mode | Execution Entry Point |
| --- | --- | --- |
| Create a new deck from a theme, brief, or attachments | New Creation | Go to "2. New PPT" |
| Modify an existing deck, affecting only a few pages/local presentation | Simple Edit | Go to "3.2 Review Quick-Fix" |
| Modifications that alter narrative, facts, materials, global style, or multi-page structure | Complex Edit | Go to "3.3 Orchestrator Overhaul" |

When in doubt, perform a read-only impact analysis first. **Do not ignore the actual scope of impact just because the user says "make a simple change," nor treat narrative-level changes as simple edits just because the number of modified pages is small.**

## 1. Contracts Shared by All Modes

### 1.1 Orchestrator's Responsibilities and Boundaries

The Orchestrator is solely responsible for: **mode determination, planning, delegation, merging, acceptance, and deterministic finalization**.

- Writable: `plan/`, `base.css`, knowledge synthesis, and build artifacts.
- Does not directly write: `slides/slide_NN.html`; pages are modified by Slide or Review.
- Does not fake tool capabilities, and does not write planned actions as completed actions.
- Each subagent must have an explicit label; results from failures, timeouts, or unnatural terminations must not be treated as finished products.
- The structured contract, artifact paths, and `handoff_path` returned by `delegate_task` serve as the source of truth for parent handovers; the Orchestrator does not read the subagent's `messages.json`, `tool_log.json`, or system/tool snapshots to poll progress. Research is a task-level singleton; Review is executed at most 3 times, and subsequent instances can only be used for controlled re-verification after fixes, not for repeated re-reviews based on aesthetic preferences.

### 1.2 Roles

| Role | Quantity & Timing | Sole Responsibility |
| --- | --- | --- |
| Research | At most 1 | Verify external facts that could alter conclusions, write `research/research.md` |
| Material | Parallelized by attachment | Each instance processes only its own attachment slice, writes `research/materials/material_NN.md` |
| Image | Parallelized by material volume | Retrieve or generate bitmap materials, return actual paths |
| Slide | Parallelized by design-related page groups during new creation or complex edits | Only produce/redo its own page groups and complete the pixel closed-loop within the group |
| Review | **Editing flow only** (this fast variant delegates no Review in the new-creation flow); when editing: 1 for initial acceptance, at most 2 re-verifications after fixes, and also acts as the executor during simple edits | Diagnose first, then perform centralized fixes, batch re-rendering, and final presentation script finalization |

All roles must fully read their own `subagents/<role>.md` before starting work. If any selected file or section displays a truncation prompt, continue reading until the end; **references not hit by routing must not be read**.

### 1.3 Language and Capabilities

Lock before starting:

- `response_language`: Language for processes and final responses;
- `deliverable_language`: Language for screen display, planning, and presentation scripts;
- Capability states for attachments, web, real image retrieval, image generation, rendering, vision, file writing, etc.

Default to the primary language of the user's query; override individually when the user explicitly specifies a delivery language. Plan only capabilities that are actually available.
`vision_analyze` is directly viewed at the pixel level by the same model used by the current role; visual judgments, issue ledgers, and responses after viewing images must use `response_language`, and must not switch due to the model's default language. Original screen text and proper nouns may retain their original language.

### 1.4 Workspace Source of Truth

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

- `grounded-knowledge.md` is the source of truth for facts;
- `design-brief.md` is the source of truth for visuals;
- `slide_NN.md` is the page content contract;
- `base.css` is the global design system;
- Final judgments must be based on the latest PNG, not just the HTML.

### 1.5 Reference Routing

| Scenario | Read |
| --- | --- |
| Scenario Tone Setting | `design-rules.md` §T1–T3, §1–3 + the matched theme section; `design-styles.md` directory + one style family |
| Global & Page-by-Page Planning | `planning-contract.md`; read only the corresponding page type in `layout-patterns.md` for each page |
| Editing Existing Deck | `editing-contract.md` |
| Slide | Own page-by-page plan, `base.css`, `quality-checklist.md` "I. Single-Page Check" and the matched sections of this page |
| Review | Fully read `quality-checklist.md`: first verify visual semantics using "I. Single-Page Check", then perform "II. Full-Deck Check" and "III. Machine-Verifiable Lint Items" |

Fonts should only be read from `fonts.md` when default roles are insufficient or the occasion is sensitive. Do not scan all design, style, layout, and font documents before starting work.

Regardless of whether `fonts.md` is read, Chinese eyebrow tabs, department names, footers, sources, and metadata must not use monospace fonts or Latin ALL CAPS with wide letter-spacing: use `--font-sans` / `--font-serif`, and keep letter-spacing at `0–0.03em`. `--font-mono`, `--tracking-caps`, and `.is-latin-label` are used only for pure Latin technical identifiers, code, coordinates, and actual serial numbers.

The same Chinese title, conclusion, or label sentence must use the same font family; emphasis words should establish hierarchy only through color, weight, size, or underlining. It is forbidden to replace a few of these characters with cartoon, handwritten, or another display font. **The whole deck uses at most 3 font families, applied uniformly**: the same role uses the same family on every page — do not swap fonts per page topic; rigorous decks converge to 1–2 families. Font selection follows the scenario and Style Lock — Chinese body/tables/footers/eyebrows always use `--font-sans` (or `--font-serif` for rigorous serif), and **Chinese must never go into `--font-mono`** (IBM Plex Mono has no CJK glyphs and falls back to a cartoon face); `--font-mono` serves only pure-Latin code, coordinates, and IDs. `playful`, playful/rounded, handwritten, and calligraphic fonts should only be enabled when the theme and audience truly support them (occupying at most 1 of the 3 families as an accent), and the corresponding elements must be marked with `.is-expressive-type` or `data-type-intent="expressive"` so that rendering checks can confirm this is an intentional choice.

### 1.6 Visual Medium Priority

Choose the medium based on the content:

1. Real people, places, products, events: Real photos;
2. Atmosphere, metaphors, story scenes, main visual key visuals: Generated images or high-quality bitmaps;
3. Data: ECharts;
4. Large-scale processes, architectures, mechanisms, relationship diagrams: **Canvas-drawn geometry + HTML text layers**, or "textless images + HTML annotations";
5. SVG: Used only for icons, logos, arrows, markers, and small decorations.

Whenever an ordinary content page has a visible subject—people, places, products, works, activities, experiences, natural or urban environments, story scenes, or an emotional key visual—prefer a substantial real or generated bitmap as the primary visual instead of replacing it with colored blocks, icons, or abstract wireframes. Image count follows the narrative rather than a quota, but a bitmap opportunity that adds recognition, evidence, presence, or emotion must not be silently discarded.

**By default, hand-drawn SVG must not be used as a hero, half-screen/full-screen main visual, or large schematic.** SVG is limited to small supporting graphics. If a complex non-bitmap main visual truly needs programmatic construction, prefer Canvas geometry plus an HTML text layer. The only exceptions are an explicit vector-delivery request or the required reuse of an accurate vector asset supplied by the user. For detailed implementation, see `design-rules.md` §5 and the Canvas diagram section of `layout-patterns.md`.

## 2. New PPT

### Phase 0: Parse Task

1. Lock the language, capabilities, attachment list, and delivery scope.
2. Establish a scenario card: Speaker, Audience, Occasion, Objective, Duration, Page count, Screen vs speech, Core takeaway, Assumptions. The scenario card is used only for internal planning and is not a source for on-screen text.
3. Do not ask the user for non-blocking preferences; explicitly record reasonable assumptions in the plan.

### Phase 1: Grounding

#### Material

When attachments are present, split them into non-overlapping Material slices by attachment and execute them in parallel; default to one slice per attachment. Each goal provides:

- `response_language` and `deliverable_language`;
- `assignment_id`;
- Exact attachment path;
- Independent directory `materials/_work/material_NN/`;
- Independent output `research/materials/material_NN.md`.

All formats must first go through the unified `stage_materials.py` entry point in the Material role card: read full text for text, view actual images for images, and retain both text and page/embedded image visuals for PDFs/Office files; audio, video, compressed archives, or unknown formats should use existing environment conversion capabilities as suggested by the catalog. Do not misrepresent file metadata, archive member names, or media representative frames as semantic content.

After all results are returned, verify against the catalog item by item: each attachment must have a unique `coverage_id`, with status `ok`, coverage `complete`, and text chunk intervals continuously covering the full text or scanned pages covering all pages; the Coverage ledger of each slice summary must contain the corresponding `coverage_id`. Any `semantic_coverage: incomplete`, `truncated`, `unsupported`, `incomplete`, `failed`, or `missing` blocks downstream progress; non-empty summaries, metadata, or representative frames must not be treated as fully read materials.

#### Research

Dispatch the single Research agent only when external facts could alter conclusions. Named real products, clinical/operational statistics, external benchmarks, and specific numbers that bear conclusions are all external facts requiring verification, unless already provided by the user or attachments. The goal uses:

```text
Research:
Response language: <response_language>
Deliverable language: <deliverable_language>
Raw user query (verbatim): <raw_user_query>
Unresolved terms: <unresolved_terms_or_none>
Evidence needed: <gaps_that_would_alter_conclusions>
Parent interpretations are hypotheses, not user claims.
```

`Raw user query (verbatim)` must be copied verbatim from the complete user message, without summarizing, rewriting, adding punctuation, or omitting visual requirements. `Unresolved terms` and `Evidence needed` may include candidates that the orchestrator deems worth verifying, but newly added items must be explicitly marked as `orchestrator hypothesis` and must not be faked as dates, numbers, people, places, or viewpoints already declared by the user. A statement is allowed to be called a "user requirement / user brief / user original text" only when it can be located verbatim in the Raw user query or Material source text; otherwise, it must be called a "delegated hypothesis" or "verification candidate."

Research submits the first round of independent queries in parallel within one tool turn, extracts the best sources in parallel in the next tool turn, and performs at most one more round of targeted supplementary searches.

#### Grounding gate

After Material / Research are collected, the Orchestrator's next action must be to write the unique `plan/grounded-knowledge.md`, and then use `read_file` to verify that the file exists and its content is complete; do not enter `design-brief.md`, Style Lock, or page-by-page planning before this is completed. The file must distinguish between user facts, external verifications, orchestrator hypotheses, illustrations, conflicts, and unconfirmed items, without adding source-less new facts. If Research returns `partial`, the `unresolved` in the contract must be written verbatim into `## 未解决与使用边界`, with an explanation that the relevant propositions must not appear on screen as definitive conclusions; failure to propagate this boundary is deemed as incomplete Grounding. If Research mistakenly refers to a delegated hypothesis as the user's original words, the attribution must be corrected according to the Raw user query during merging, and "hypothesis disproven" must not be written as "correcting the user."

Attachments provide **facts and reusable material boundaries, not default design ceilings**. During merging, organize reusable page images, embedded images, chart structures, and brand cues from the materials; then, in `design-brief.md`, clarify `material_visual_mode`: `facts-only`, `visual-reuse`, `style-reference`, or `faithful-restyle` explicitly requested by the user. Write a separate `attachment_visual_map` for each image attachment to decide on must-show / reuse / reference-only / omit, the on-screen page, and the processing method; full-page visuals of papers and in-page named Figures must be distinguished as `page-facsimile` and `figure-crop`. This judgment is independent of `image_opportunity` for external image search/generation. Except for `faithful-restyle`, do not inherit small font sizes, dense tables, ordinary document layouts, or low-quality visuals from attachments; still set the tone based on the audience, occasion, and narrative.

### Phase 2: Scenario Tone Setting and Style Lock
1. Read visual rules according to the Reference route; do not scan the entire library.
2. First lock in the `scene_register` (solemn report / editorial narrative / product launch / instructional explanation / cultural experience, etc.) and a clear primary style; the style must explain "why it is suitable for this audience, occasion, and content", rather than just listing abstract adjectives, and must not combine multiple style numbers into a compromised package. Borrowing at most one auxiliary craft is allowed, but the entire deck must be explainable with a single visual thesis statement.
3. Write the `plan/design-brief.md#Style Lock`:
   - scene;
   - primary_style;
   - supporting_craft (at most one);
   - visual_thesis / signature_visual;
   - palette / typography / numeric_voice;
   - image_language / image_opportunity_map / composition_grammar;
   - background_system: First, based on the scene, explain whether the background should lean toward "restrained order" or "atmospheric expression", then define a `base_canvas_family` that runs through the content pages and its allowed visual state variations (brightness, color field, ambient light, texture, image ratio, density, and chapter state); for each state, clearly write its narrative purpose, applicable pages, and entry/exit transitions. Scenes like academic presentations, group meetings, compliance reviews, and serious evaluations can be quieter, but still require typography and evidence visuals; for other scenes, do not treat a single solid color background across the entire deck as a safe default. Unity does not mean the same background color for the entire deck; variations must not deviate from the same canvas family;
   - motif_role: Explain on which pages the thematic motif serves as the primary visual, on which pages it serves only as a secondary clue, and on which pages it is actively absent. The same decorative motif must not bear the primary visual responsibility for the cover, chapter pages, and most content pages; consistency comes primarily from typography, color semantics, image treatment, and composition grammar. Technical notes, coordinates, field logs, archive numbers, etc., can become motifs only when they convey real and useful information; do not fabricate fake metadata to create a false sense of "premium quality";
   - special_pages;
   - avoid;
   - spatial_rhythm: how content pages unfold, how breathing pages focus, and where the peak pages are located;
   - special_page_system: what design DNA the cover, chapter pages, and ending pages share, and what composition actions each uses.
   - material_visual_mode (when attachments exist): which elements serve only as facts, which images/charts can be directly reused, and which style clues are worth retaining.
   - attachment_visual_map (when image attachments exist): original path, must-show / reuse / reference-only / omit, `material_asset_type`, formal asset path, on-screen page, cropping/full-image/cutout/color-grading and reasons. For papers, `Figure N` must record the source page and boundaries of the figure-crop, and must not directly reuse the entire page PDF PNG.
4. When the user does not specify a style, proactively determine it based on topic × audience × occasion. Do not enter planning without a Style Lock; do not treat generic color palettes and font lists as a completed tone-setting without visible `signature_visual` fulfillment pages.

`image_language` First explain which colors themselves carry identification, evidence, or instructional information, and then decide on unified processing. Real subjects such as people, animals, plants, works, products, venues, and experimental outputs should retain their meaningful original colors by default; a sense of unity comes primarily from image selection, cropping, color temperature, local color overlays, borders, and backgrounds. Use full-image grayscale or duotone only when the user explicitly requests black-and-white/duotone, or when the visual thesis of this deck truly relies on such treatment and it does not compromise identification and evidential value; "academic feel", "premium quality", or "for the sake of unity" do not themselves constitute reasons to desaturate real images across the entire deck. For images that bear identification, evidence, or primary visual responsibilities, define a lightweight `crop_contract` at the same time: focus point, essential subject parts/in-image information to retain, background allowed to be cropped out, and recommended fit; do not just write an aspect ratio and let Slide guess the cropping.

Style Lock locks the **visual language and judgment boundaries**, not a set of fixed HTML templates, nor does it lock down the geometry of every page. You must clearly distinguish between:

- Stable language: font roles, color semantics, spacing rhythm, background syntax, image cropping/color-grading, graphic syntax, and special page kinship;
- Controlled variations: primary focus of each page, composition direction, media ratio, information density, whitespace placement, and chapter state;
- Prohibited items: temporarily introducing new fonts, new color schemes, theme-less decorations, or copying the geometry of the previous page and only changing the copy.

It should act like executable Art Direction: specific enough so that different Slides can produce pages belonging to the same world, yet retaining enough space for each page to choose the optimal composition based on its content. There is no need to create separate template files or share decorative assets.

`image_opportunity_map` must first perform a realization-independent "visible subject scan": whether this page has people, venues, products, works, activities, experience scenes, fictional characters, or emotional key visuals that deserve to be seen; first explain the evidence, identification, presence, or emotional value that images can add, and then choose real images, generated images, or code-based visuals. Do not favor CSS/Canvas first and then reason backward that "there is no image opportunity".

When the core content of a page is a group of **named real people, creators, guests, or team members**, treat "person identifiability" as a real image opportunity by default, and hand it over to Image to batch retrieve portraits, official bio photos, event photos, or team group photos. "Should not generate fake real people" only means that generated portraits cannot be used to impersonate the actual person; you cannot use this to reclassify the page as `image_opportunity: none`. If only some of the people's photos can be reliably obtained, prioritize using a credible team/institutional scene image paired with a few key portraits, or reduce the number of people and restructure the narrative; do not fill in the gaps with unidentified similar faces. Only when real retrieval still yields no identifiable, downloadable, and screen-suitable assets, and images would not add identification value, should you use pure typography, and record the gap and downgrade reasons in the plan.

Similarly examine named works, software/products, production processes, and case studies: retrievable official visuals, interfaces, behind-the-scenes production shots, process breakdowns, physical objects, or on-site photos usually establish identification and credibility much better than small icons and empty cards. Every ordinary content page should have a primary visual carrier commensurate with its content—real/generated images, charts, explanatory Canvas, or a typographic key visual that can truly stand on its own. Borders, empty panels, micro-icons, and decorative lines do not count as primary visual carriers; pure typography is valid only when the text itself is intentionally enlarged, organized, and forms a clear focus. Adding images does not mean adding scattered points: prioritize one substantial main image or a set of assets with a consistent visual caliber, letting other elements quietly serve it.

When attachments exist, perform a complete scan in the same way: prioritize reusing images therein that are truly informative and clear; if the attachments lack real-world scenes, people, or brand images, merely stating "no real images in attachments" does not equate to "generated images will fabricate things, so images are prohibited". Generated images used for atmosphere, vision, conceptual experience, or non-specific scenes can be used as the expressive layer, while precise facts, numbers, and relationships remain in the HTML / chart layer. Separate factual fidelity from visual imagination, and do not mechanically turn material summaries into card walls.

Image attachments must not default to disappearing simply by being "understood and redrawn". When the user explicitly requests production based on a certain image, or when the image itself is the sole product, person, venue, work, evidence, before-and-after comparison, or process overview map, mark it as `must-show`, and ensure it appears on at least one page as an identifiable full image or a faithful crop; complex flowcharts can first use the original image to establish the full picture, and then be redrawn step-by-step using Canvas/HTML. Omit only when it is repetitive, irrelevant, unreadable, or the user explicitly does not want it displayed. `image_opportunity: none` only means not adding new external/generated bitmaps, and cannot override `attachment_visual_map`.

The identification and evidence of real objects, the presence of scenes, the credibility of people and products, and the anchors of stories and emotions all constitute valid image opportunities. Fictional characters, conceptual scenes, unbuilt spaces, and stylized key visuals are precisely the suitable subjects for generated images; they should not be replaced by colored squares, abstract symbols, or pure CSS placeholders just because they are not real objects. "CSS is more controllable", "no user-provided real photos", "fear of AI generation errors", or "for style consistency" cannot individually serve as reasons for `none`; these issues should be resolved through real/generated image routing, prompt constraints, unified cropping, and color-grading. Choose `none` only when bitmaps indeed add no value to the audience, or would be more ambiguous than charts, Canvas, or typography. If a deck contains multiple obvious visible subjects but is globally judged as having no bitmaps or only one cover image, this scan must be redone before freezing the plan. No image quantity quotas are set here, nor are images provided for mere decoration.

When image search or generation capabilities are available, **having the entire deck be completely `none` or having only one cover image is an anomaly that requires justification, not a default safe route**. Data, business, technical, academic, or code topics cannot revert the entire deck to a card wall because of this: factual pages can use charts/Canvas, but at least two nodes among the cover, chapter transitions, cases, scenes, visions, or conclusions that can truly benefit from images should be selected to provide executable search/generation briefs; short decks must guarantee at least one content node, not just the cover. An entirely bitmap-free deck is allowed only when the user explicitly requests pure typography/pure charts, or when it is proven page-by-page that bitmaps would reduce accuracy and readability, with the page-by-page exception reasons clearly written in `plan/deck.md`. This is the minimum coverage line to prevent misjudgment, not for padding numbers; factual real images and non-evidential atmospheric generated images must be clearly routed.

The visible-subject scan must also be persisted as `plan/image-strategy.json` for deterministic workflow validation before any Slide delegation. When image opportunities exist, write `status: images_required`, `visible_subject_scan_complete: true`, and the complete `image_opportunity_pages`. For a fully bitmap-free deck, write `status: bitmap_exception`, `visible_subject_scan_complete: true`, `reviewed_pages` covering every planned page, an `exception_reason` of at least 20 characters, and one `exception_basis` from `explicit_user_request | pure_typography | pure_chart | wireframe | accuracy_critical`. A prose summary cannot replace this file.

Background does not equal a solid color block, nor does it mean randomly changing skins on every page. Scenes like academic presentations, group meetings, compliance reviews, and serious evaluations can use quiet canvases to support facts; expressive scenes such as products, brands, investment promotion, cultural tourism, culture, stories, course introductions, events, and mass communication should proactively consider an environmental design layer compatible with the theme, rather than reverting the entire deck to solid colors: this can be a directional soft light field, local glows, thematic textures like low-contrast grain/halftone/paper texture/topography, image backgrounds, or backgrounds uniformly generated by Image. Glows are valid only when they can explain the light source, theme, and visual focus, and when their shape and position are related to the composition; a circular blurred light spot reflexively copied behind a title still counts as a theme-less glow. First determine the base canvas family that runs through ordinary content pages, and then select a few compatible techniques to form the background syntax. Chapter differences should be expressed primarily through local large color fields, image color-grading, bands, or motif states; replace the entire page canvas only when chapter pages, hero pages, endings, or narratives truly require a global scene change, and retain transitions of color, texture, image treatment, or composition direction in the preceding or succeeding page. Image or generated backgrounds must enter the `image_opportunity_map` and asset briefs, and paths must not be temporarily invented by Slide. Avoid situations where several pages suddenly look like another deck and then switch back without transition, and avoid treating deep navy, neon blue-purple gradients, or generic tech glows as the default "premium quality".

There is only one subsequent main chain: `Style Lock → full-deck plan + prepare → Image slices in parallel → asset paths backfilled once → Slide groups in parallel → script sync + build → deliver` (**this fast variant has no Review step**). If the source of truth of the preceding node is not frozen, downstream tasks dependent on it will not be started; independent tasks at the same level are dispatched in parallel at once.

### Phase 3: Global Planning and Font Pre-loading

Completely read `references/planning-contract.md`, and then in order:

If `materials/font-config.json` exists, read it once first and use its title/body/number/annotation roles as the font inputs for Style Lock; user-uploaded fonts take precedence over automatic selection, and uncovered characters will automatically fall back to the delivery font package. It is prohibited to switch to unuploaded local fonts based solely on font names.
1. Complete `design-brief.md`;
2. Write `plan/deck.md`;
3. Copy `base-template.css` to `base.css` and fill in the token;
4. Write all `plan/slide_NN.md` at once, with each page attaching its own Reference route;
5. Define Production groups in `plan/deck.md`: all transition pages as `dividers`, cover and ending as `bookends`; content pages must first be grouped by **production method and compositional affinity**, then consider narrative continuity, and finally consider chapter affiliation. A group should share the same type of production problem, rather than cramming different media such as cards, complex Canvas, data charts, and real photos into a single Agent just because they belong to the same chapter; identical chapter names do not constitute a reason for grouping. For each group, write `boundary_handoff` at the same time, describing the state of the canvas, lightness, color field, and motif before entering and after leaving this group; once grouping is complete, cross-check against the page-by-page table once to ensure that each page belongs to exactly one group, and that page types such as chapter pages and interactive pages are not misnumbered.
6. References and ending pages must bear separate responsibilities: sources that need to be displayed on screen must use independent references pages or preceding content pages; closing is only responsible for wrapping up the proposition, action, or question, and must not be merged with long references, detailed reviews, or multi-column summaries.
7. Before starting Image or Slide, write a brief `## Repetition & rhythm preflight`: compare page-by-page canvas states, title anchors, compositional directions, media, image ratios, information densities, and motif roles; simultaneously compare the page scripts of each chapter vertically, and do not mechanically copy the same set of "pain point - before case - after case - steps - tools" to different chapters. Shared rhythm can form affinity, but each chapter should still have its own problem perspective, evidence task, and reading action; when a page has no independent responsibility, merge or reconstruct it. If repetition or flat rhythm is found, modify the page map, Style Lock, or Production groups first before freezing the plan.
8. Perform a **content sufficiency and on-screen semantic deduplication**: for each ordinary content page, first write down the irreplaceable audience takeaway, and then continue to explain using the evidence, mechanism, comparison, case, action, or boundary most suitable for that page; do not set a fixed number of items, but a page with only a topic sentence, synonymous subtitle, and status badge does not count as valid content. If there is no new supporting layer, merge pages, change narrative responsibilities, or convert it into a transition/breathing page with a true single focus, rather than stretching thin content into a page using large areas of unassigned blank space or repetitive labels. Confirm page-by-page that the primary visual carrier matches `spatial_budget`; do not rely on large borders, equal-height cards, or empty panels to geometrically "fill up" the space while pinning short text to the edges and leaving large blocks of unengaged internal blank space. Compare page-by-page the titles, kickers/subtitles, image badges, badges, callouts, legends, and footers; usually, only one strongest carrier is chosen for the same phrase, while other areas supplement the object, reason, change, or result. Repeat only when navigation or on-screen comparison is absolutely necessary, and each occurrence must serve a different purpose. `dense` is not just a label: if the main content is squeezed into only half of the canvas or a narrow strip, and the remaining space has no focus or direction, the spatial plan must be redone.
9. Perform a `screen-copy firewall`: distinguish page-by-page between "what the audience must see" and "for production use only". Speaker/Audience/Occasion/Objective, page responsibilities, production group, visual acceptance, asset routes, evidence numbers, assumptions, filenames, and Research/Material sources must remain in the plan or script, and must not automatically enter `## 最终屏显文案`. Only when the page topic itself indeed discusses the target audience, project objectives, or research methods should the relevant content be rewritten into a narrative understandable by the audience, rather than displaying internal labels such as `受众：…`, `主体：…`, `页面角色：…`, etc. On-screen copy and HTML must not use emojis / Unicode icons (such as `👀 ✋ 💡 ✨ ★ ✦`); when icons are needed, use local small SVGs, CSS shapes consistent with the Style Lock, or express them directly in text. Generic decorations such as stars, hearts, and fireworks must not be scattered across most pages as "deck-wide embellishments", and should only appear on pages where they serve a genuine compositional role.
10. Use a deterministic command to synchronize the script and pre-load the font package from the plan:

```bash
python ${SKILL_DIR:-skills/sn-ppt-web-en}/scripts/deck.py prepare . --expected <总页数>
```

Plan freezing conditions: facts, page order, on-screen copy, visual media, page-by-page illustration opportunities, asset briefs, background processing, sources, scripts, page types, and fonts are all finalized; `plan/image-strategy.json` has been written; and the plan has passed content sufficiency, on-screen semantic deduplication, and the screen-copy firewall. Before freezing, specifically disprove all `image_opportunity: none`: if a page already has a visualizable subject or scene, it cannot be excluded solely because "code is more controllable". When on-screen copy or font tokens change, synchronize the plan first before re-running `deck.py prepare`.

The page-by-page plan must also complete a spatial rehearsal and a visual acceptance rehearsal: clearly write down which areas the main visual and text occupy, how the main information utilizes the safe zone, why the remaining space exists, and what objects, directions, domain evidence, and conclusions the audience should read from the final pixels. `deck.md` and the media in the page-by-page plan must not contradict each other. For ordinary content pages, if the rehearsal result is "the subject is shrunk in the middle, with large areas of unassigned blank space around the periphery", "can only fit using small text", or "can only use generic geometry instead of domain evidence", modify the plan first, and do not leave the problem to Slide. For chapter transition pages, rehearse "main information cluster + visual counterweight + negative space responsibility": keep the content concise, but do not just place a small cluster of text in one area, leaving the rest of the canvas as undesigned pure blank space.

### Phase 4: Asset and Page Production

1. Aggregate all image briefs determined to be real photos or generated images, and then start the Image subagent; each goal must explicitly carry stable `group_id`, `response_language`, and `deliverable_language`. **Before the first Image delegation**, every `plan/slide_NN.md` must contain exactly one complete one-line machine field `- image_opportunity: <enum>` inside its sole `## Visual implementation` section; bitmap pages add a separate sibling line `- presentation: <one of the four enums>`. Never create an empty `image_opportunity:` parent block, put `full-bleed` / `framed-scene` in `image_opportunity`, or put layout values such as `split-media` in `presentation`. If the gate reports a missing field, fix the plan directly and retry; do not search for or edit runtime Skill/Harness code. As long as a valid illustration opportunity exists in the plan, the Image phase must not be silently skipped; assets with the same visual recipe that can be reviewed together on a single contact sheet are grouped into the same slice, and multiple generated images are submitted in parallel within the same tool round. Image and Slide must not be dispatched in the same `delegate_task`: complete and validate the asset phase before page production starts.
2. First copy and register the sources of must-show / reuse images in `attachment_visual_map`, then hand them over to the corresponding Image group; paper-named Figures must first be processed by Image using `deck.py material-figure` to generate independent, traceable Figure crops from page images, with the full-page PNG serving only as positioning context. Each Image group binds candidate paths to stable `asset_id`, and `deck.py asset-contact` generates an asset contact sheet with IDs, performing only one full-group Vision check by default; single-image review is opened only for assets that are flagged in red, require background removal, have suspicious aspect ratios, or whose subject integrity cannot be determined from thumbnails. After Image writes back the final state using `asset-review`, Orchestrator backfills the page-by-page plan only according to the `asset_id → actual path + origin + crop_contract` of `ready`; candidate, replaced, and discarded images do not count as official assets. `assets/catalog.json` is the sole source of truth for assets and must retain the download URL, generative model, user attachment path, and derivation relationships; Image's prose summary cannot replace the catalog. Page-by-page images must first lock `presentation: subject-only | framed-scene | full-bleed | evidence-crop` (this is the bitmap render/background-handling contract and may **only** be one of these four enums; `split-media`/`right-half`/`cards` are layout, not presentation — put them under layout; **a no-bitmap page omits presentation entirely**, never writing `none`/`not-applicable`): any image that needs to float, overlay across color fields, or act as an independent character/object belongs to `subject-only`, and Image must complete transparency checks, subject background removal, final Alpha checks, and necessary single-image Vision checks before backfilling usable `*-cutout.png`; ordinary RGB images must not be returned to `ready` as transparent assets. Images with backgrounds can only serve as intentional framed scenes, full-bleed crops, or evidence crops, and their white/cream-background rectangles must not be accidentally pasted onto another canvas. Slide does not perform temporary background removal, nor does it fake it using CSS mask/multiply. If there is indeed a mapping issue, return it to the same Image for review. For failed assets, first switch to a viable real photo or generated image route; only when truly unavailable should it be downgraded to Canvas or typography, with the reason clearly written and no placeholders left. Before Slide starts, Image must have a `status: ready` completion contract, every planned `asset_id` in the catalog must be `ready` with an existing file, and the actual path plus crop contract must already be backfilled into the page plan.
3. One Production group delegates one Slide, which can be executed in parallel; the first line of the goal must be written precisely as `Slide Group <group_id> [NN,NN]:`, for example, `Slide Group bookends [01,20]:`. Page number ownership is based on the frozen `production_group`; do not replace group IDs and standard page number headers with descriptions like "responsible for cover and ending" or "first group of pages". Explicitly include `response_language`, `deliverable_language`, and the group's `boundary_handoff`. Do not split a frozen multi-page group into "one Slide per page" just to increase concurrency; delegate as a single-page group only when the plan itself indeed defines it as such. The same group must simultaneously satisfy narrative affinity, design affinity, and production load compatibility; complex Canvas, independent data charts, or heavy image composition pages should be grouped separately when they do not truly share a composition system. Grouping provides shared design memory, not batch precision reduction: the same Slide completes the closed loop of each page serially according to the page order within the group.
4. Slide first reads the Style Lock and group contract, and then sequentially executes "complete first draft → single-page render → `vision_analyze` → at most one merged fix → re-render and re-review" for each page; enter the next page only after the current page reaches ready. After all pages are completed, batch render this group and review all final PNGs within the group to confirm affinity and obvious regressions, but do not open new cycles for aesthetic preferences. The cover, each chapter page, and the ending page must all complete their own single-page closed loops. The "merged modification → re-render → re-review" after the first image viewing is recorded as one round of refine, with a maximum of 1 round per page; if real critical flaws remain, switch to a more stable structure or return blocked. If there is no re-rendering and image viewing after the last modification, it must not return ready.
5. After all pages complete, proceed directly to Phase 5 delivery (**this fast variant delegates no Review in the new-creation flow**). Orchestrator must not chase advisories such as `cjkTypography`, `crowded`, bbox/contrast candidates, or minor line breaks/punctuation, nor open aesthetic cleanup cycles. Page quality is made solid once in the Slide phase; any genuine hard flaw should be resolved within the Slide group, not by introducing a Review at delivery.

### Phase 5: Script Sync and Delivery (this version skips the full-deck Review)

**This skill is the "fast variant": the new-creation flow does NOT delegate a final_review subagent and does NOT do full-deck pixel closeout.** Pages are considered final once each Slide group produces them; the Orchestrator dispatches no Review diagnosis/rework and does not view pages before delivery. The goal is significant speed-up and token savings, at the cost of one visual self-check — so make every page solid during the Slide phase.

After all Slide groups complete, the Orchestrator closes out delivery directly:

1. **Sync the script:** write a natural, speakable `## Spoken script` for every page into `plan/slide_NN.md` (do not mechanically enumerate on-screen labels; do not leak internal planning fields/sources/filenames).
2. **Deterministic build:** run once

```bash
python ${SKILL_DIR:-skills/sn-ppt-web-en}/scripts/deck.py build . --expected <total_pages>
```

   to produce the final `base.css`/font subsetting, full-deck renders, and `present.html`/delivery package.
3. **Deliver:** after confirming all slides, non-empty final renders, `speech.md`, and an openable `present.html`/delivery package exist, return `ready`. Only unusable technical failures (missing pages, missing/blank renders, a player or package that cannot be built or opened) are marked as failed.

Do not delegate a Review subagent; do not open extra image-viewing cycles for aesthetic preferences. Any genuine hard flaw is something the Slide phase should have resolved, and no Review is introduced at this stage.

## 3. Editing PPT

First, completely read `references/editing-contract.md`, perform a read-only check of the existing plan, HTML, assets, scripts, and rendered images, establish a list of affected files/pages, and then select the editing path.

### 3.1 Determination of Simple vs. Complex

**Simple editing** satisfies all of the following simultaneously:

- Does not change core arguments, page order, page responsibilities, or cross-page narrative;
- Does not require new Research, Material, or Image;
- Does not change the global Style Lock, font system, or multiple archs;
- Can be safely completed within a small number of pages, with clear impact boundaries.

If any condition is not met, it is treated as complex editing. The number of pages is merely a signal, not the sole criterion.

### 3.2 Review Quick Fix

Simple editing delegates only one Review, with the goal specifying `mode=simple_edit`, the user's original modification requirements, target pages, and immutable items.

Review:

1. Inspect the existing overview and the final PNG of the target pages;
2. List all modification items for this round at once;
3. Read the target page plans and HTML, and perform centralized modifications;
4. Update the affected plans/scripts;
5. Re-render once using `render.py --batch --pages`;
6. Inspect the focus contact sheet and return ready/blocked.

Do not dispatch Slide, Image, Research, or a second Review.

### 3.3 Orchestrator Restructuring

Complex editing is handled by the Orchestrator:

1. Write an impact map: how facts, narrative, page order, Style Lock, base.css, assets, pages, and scripts are respectively affected;
2. Reuse only the existing results that remain valid, without overwriting unrelated pages from scratch;
3. Delegate a single Research, multiple Material/Image tasks, and multiple affected Slide page groups based on gaps; run mutually independent tasks in parallel;
4. Update affected plans and run `deck.py prepare`;
5. Redo only the affected pages; batch re-render the entire deck when global tokens change;
6. Finally, delegate a single Review to perform entire-deck consistency and script finalization using `mode=final_review`.

Complex editing does not allow the Review to rewrite the narrative alone or supplement assets out of thin air, nor does it allow the Orchestrator to directly modify page HTML.

### 3.4 Editing Delivery Gates

- User requirements must be traceable item-by-item to the modification results;
- Unaffected pages and assets must remain unchanged;
- Style, page numbers, scripts, and player must be consistent between new and old pages;
- Final pixels of all changed pages must have been inspected;
- Font packages, render freshness, `speech.md`, and `present.html` must pass verification again.

## 4. Hard Red Lines

- Charts must use ECharts; do not use generated images to fake data charts.
- AI-generated images must not carry text that needs to be accurately presented; place text in the HTML layer.
- SVG is only used for small elements, not for large structural diagrams or key visuals.
- `slides/` must only retain formal `slide_NN.html`, with no backups or temporary pages.
- Fixed page skeleton, footer safe zone, minimum font size, contrast, and zero overflow are hard gates.
- Internal planning tags, sources, file paths, production status, and pseudo-metadata with no audience value must not appear in the screen display content.
- The final judgment relies on PNGs; if pages are not re-rendered after modification or new pixels are not inspected, completion must not be claimed.
- Review is limited to a maximum of 3 controlled instances; re-verification is allowed only after pages are actually modified and re-rendered. Once the budget is reached, stop rework and deliver the usable completed draft with warnings.

## 5. Deterministic Scripts

| Script | Purpose |
| --- | --- |
| `stage_materials.py` | Retain: Unified processing of parsing, visual derivatives, and coverage catalog for text, PDF, Office/ODF, images, media, compressed archives, and unknown formats |
| `font_bundle.py` | Retain: OFL whitelist, official sources, license-with-package, character subsetting, delivery validation, and render freshness belong to independent high-risk capabilities |
| `render.py` | Retain: Single-page diagnostics; `--batch` reuses the same Chromium instance to render the entire deck or specified pages |
| `image_cutout.py` | Retain: Check Alpha, clear baked checkerboard/solid color backgrounds, and generate independent subject PNGs using GrabCut when needed; do not overwrite source original images |
| `deck.py` | Retain: `prepare` completes plan, script, and font pre-requisites in one go; `asset-register` registers sources; `asset-assign / asset-contact / asset-review` manages semantic assets, group contact sheets, and final states; `contact` generates page contact sheets; `build` validates sources and generates the player; `sync` is for use only when modifying scripts exclusively |
| `install.sh` | Retain: Cross-environment dependencies, fonts, and Chromium installation cannot be reliably replaced by running scripts; the dependency manifest is inlined |

First-time deployment depends on resolving venv, PyMuPDF, distributable fonts, FontTools/Brotli, and Playwright Chromium; install using `scripts/install.sh`. If the skill mount path differs when running scripts, use the actual skill root.
