# Layout pattern library

These are structural starting points inside `.slide-body`, not finished templates. Keep the shared title, footer, page number, safe margins, and CSS tokens intact. Adapt proportions to the content and record the selected pattern and rationale in `plan/slide_NN.md`.

## 1. Timeline or milestones

Use one horizontal or vertical axis, three to six evenly spaced nodes, and a consistent node schema: phase, time or milestone, and outcome.

```html
<div class="timeline">
  <section class="timeline-node"><b>01</b><h3>Discover</h3><p>Evidence and constraints</p></section>
  <section class="timeline-node"><b>02</b><h3>Design</h3><p>System and prototype</p></section>
  <section class="timeline-node"><b>03</b><h3>Deliver</h3><p>Launch and measure</p></section>
</div>
```

Split the page or group phases if more than six nodes are required.

## 2. KPI row

Use a single row of two to four important values. Keep units subordinate and highlight only the lead metric.

```html
<div class="kpi-row">
  <div><div class="num accent">68<span class="unit">%</span></div><p>Primary outcome</p></div>
  <div><div class="num">3.2<span class="unit">×</span></div><p>Efficiency gain</p></div>
  <div><div class="num">4.1<span class="unit">M</span></div><p>Audience reached</p></div>
</div>
```

This is not a generic card wall. The values and comparison basis must be real and sourced.

## 3. Two-column comparison

Use matched structures and aligned dimensions. Highlight only meaningful differences.

```html
<div class="compare-grid">
  <section><h3>Option A</h3><ul><li>Dimension one</li><li>Dimension two</li></ul></section>
  <section><h3>Option B</h3><ul><li>Dimension one</li><li>Dimension two</li></ul></section>
</div>
```

## 4. Process or logic flow

Use three to five steps and put connectors in dedicated grid cells so they align to module centers.

```html
<div class="process-row">
  <section class="step"><span>01</span><h3>Input</h3></section>
  <div class="connector" aria-hidden="true">→</div>
  <section class="step"><span>02</span><h3>Transform</h3></section>
  <div class="connector" aria-hidden="true">→</div>
  <section class="step"><span>03</span><h3>Outcome</h3></section>
</div>
```

For complex branching, use the Canvas pattern in section 10.

## 5. Split media

Use text on one side and one substantial image on the other. Set crop focus from the image contract.

```html
<div class="split-media">
  <div><h2>Conclusion-led title</h2><p>Evidence and interpretation.</p></div>
  <img class="img-cover" src="../assets/selected-image.png" alt="Descriptive alternative text">
</div>
```

Reverse the order when it improves narrative rhythm. Use `contain` when the full object is evidence.

## 6. Portrait ensemble

- For two to four people, use large recognizable portraits with name and role.
- For a larger team, use one authentic group scene plus a few highlighted people, or a compact portrait grid.
- Keep crop, eye line, lighting, and color treatment related.
- Do not invent missing faces or fill gaps with unrelated imagery.

## 7. Quote or statement

Use one verified statement with a concise attribution. The quote itself is the visual focus.

```html
<blockquote class="quote-hero">
  <p>“A memorable, verified statement.”</p>
  <footer>— Speaker or source</footer>
</blockquote>
```

## 8. Full-bleed cover or divider

```html
<section class="slide slide--bleed">
  <div class="bleed"><img class="bleed-cover" src="../assets/cover.png" alt=""></div>
  <header class="slide-title"><span class="kicker">Chapter</span>Main title</header>
</section>
```

Protect text with a scrim and validate the crop. Covers and endings normally omit the standard footer unless real metadata is required.

## 9. Hero or focal page

Use at a narrative high point. Choose one dominant device: a giant verified number, one memorable sentence, one object, or one strong image. Keep supporting copy to one short explanation and source it in `speech.md`.

## 10. Large concept diagrams

### Medium selection

| Content | Preferred medium |
|---|---|
| precise nodes, connections, stages, hierarchy | Canvas geometry + HTML labels |
| spatial metaphor or atmospheric mechanism | image + HTML annotation |
| quantitative comparison, trend, distribution | ECharts |
| icons, logos, arrows, small marks | small SVG |
| ordinary points or categories | HTML/CSS |

### Canvas skeleton

```html
<div class="diagram-stage">
  <canvas width="1200" height="560" aria-label="Relationship diagram"></canvas>
  <div class="diagram-label label-a">Input</div>
  <div class="diagram-label label-b">Process</div>
  <div class="diagram-label label-c">Output</div>
</div>
```

Canvas draws geometry; HTML carries exact language. Give the container explicit dimensions, scale for DPR, read colors from CSS tokens, and render the final state immediately.

### Common archetypes

- **Layered architecture:** three to five layers along one axis; equal sibling sizes and spacing.
- **Relationship network:** one center and three to six primary branches; no decorative constellation effect.
- **Roadmap:** NOW / NEXT / LATER or another real sequence, with one to three actions per phase.
- **Funnel:** broad inputs narrowing to a verified outcome; label stages outside the geometry.
- **Cycle:** three to six stages; use only when feedback genuinely returns to the beginning.
- **Pyramid:** three to five levels; area and hierarchy must match the argument.
- **Radar:** four to seven normalized dimensions in ECharts.
- **Trade-off matrix:** two truthful axes and clearly placed options.
- **Image-led concept:** one wordless or low-text image plus precise HTML labels.

Keep the main relationship visible at thumbnail scale. Nodes should usually remain at seven or fewer.

## 11. Editorial design actions

For a cover, divider, hero, or emphasis page, select at most one action:

- oversized cropped display type;
- vertical title at one edge;
- italic Latin display line crossing the composition;
- authentic specimen or technical annotation;
- hard image/type split;
- large quiet field with one strong collision;
- nested image window or deliberate framing.

The action must serve the subject, recur as a controlled family, and never sacrifice essential information.

## 12. Pattern adaptation rules

- Do not repeat the same pattern on adjacent pages without a narrative reason.
- Do not force content into a pattern whose proportions are wrong.
- Keep equal structures aligned and semantically parallel.
- Let content determine height; avoid fixed boxes that create empty interiors.
- Prefer fewer, larger, clearer elements to many small cards.
- Inspect the page at full size and in the contact sheet before acceptance.
