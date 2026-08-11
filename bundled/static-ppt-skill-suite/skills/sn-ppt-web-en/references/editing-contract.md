# Existing-deck editing contract

## Reading order

Before any edit, inspect `plan/grounded-knowledge.md`, `plan/design-brief.md`, `plan/deck.md`, affected page plans, canonical slide HTML, renders, `speech.md`, `present.html`, and the asset catalog. Do not change files during impact analysis.

## 1. Read-only inventory

Record:

- verbatim user instruction and target pages;
- current claim, page role, narrative dependencies, visual family, assets, and speech for each target;
- cross-page references, numbering, repeated motifs, shared CSS, and player dependencies;
- files that must change and invariants that must remain unchanged.

Classify each requested change by impact: copy, facts, assets, layout, global style, narrative, page sequence, or delivery behavior.

## 2. Route decision

### Review quick-fix

Use `mode=simple_edit` only when all are true:

- the core claim, sequence, page responsibilities, and cross-page narrative stay unchanged;
- no Research, Material, or Image work is required;
- the global Style Lock, font system, and layout families stay unchanged;
- the scope is a small explicit set of pages;
- the change can be validated through fresh renders of that scope.

### Orchestrator revision

Use a complex revision when any fact, evidence, asset, narrative, page sequence, page responsibility, global token, style rule, font role, or multi-page layout family changes. Also use it when the requested scope is ambiguous or a local edit would create downstream inconsistency.

## 3. Review quick-fix contract

Delegate exactly one Review agent with:

```text
Review:
mode=simple_edit
Response language: <language>
Deliverable language: <language>
Instruction: <verbatim request>
Target pages: <NN,NN>
Invariants: <facts, assets, layout, style, and untouched pages to preserve>
```

Review must inspect current pixels, freeze the complete change set once, edit only the bounded scope, update affected plans and speech, batch-render the target pages, inspect fresh pixels, and return `ready` or `blocked`. Do not delegate Slide, Image, Research, or a second Review.

## 4. Orchestrator revision contract

The Orchestrator must:

1. write an impact map;
2. preserve all still-valid research, materials, assets, plans, pages, and speech;
3. delegate only missing Research/Material/Image work;
4. update affected grounded knowledge, Style Lock, deck plan, and page plans;
5. run `deck.py prepare` with the unchanged total count unless the request changes page count;
6. delegate complete affected Production groups to Slide agents;
7. run the single final Review and deterministic build.

The Orchestrator must not directly edit `slides/slide_NN.html`.

## 5. Acceptance

- Every requested change maps to a visible result.
- Unaffected pages, assets, facts, and renders remain unchanged.
- Old and new pages share numbering, Style Lock, speech alignment, and player behavior.
- Every changed page has a fresh inspected final PNG.
- Fonts, render freshness, `speech.md`, and `present.html` pass again.
- A repaired deck is judged by its latest Review contract, never by a stale pre-repair contract.

