# fancy technique recipes (dazzle-deck)

> **Usage notes**
> 1. At Phase 0 selection, each deck takes only: **one global background + one entrance choreography system + one shared transition**, plus optionally ≤2 interaction easter eggs; do not stack techniques of the same kind.
> 2. Set density by scenario tier: **formal tier** (business/academic/reporting) uses only recipes marked "formal-tier OK" with parameters at their lower bounds; **free tier** (creative/thematic/launch) may use all recipes with theme graphics swapped in.
> 3. Recipes are patterns, not templates: colors, easings, and particle shapes must be replaced with this deck's design tokens and motifs; copying the example colors verbatim is strictly forbidden.
> 4. Three global iron rules: animations hang under `.slide.active` (auto-replay on returning to a page); every overlay gets `pointer-events:none`; canvas `pixelRatio` capped at 2, fixed 1280×720 scaling with the deck as a whole.
> 5. **Pick the technical vehicle by subject; the background should be "alive" by default**: CSS/SVG/Canvas-2D/shader/3D each have their strengths — pure 2D/CSS done creatively and bound tightly to the subject is equally full-score fancy. **Distinguish the two uses of 3D**: ① a **lively 3D/shader background layer** (node sphere / Fresnel sphere / shader flow field / particle stream) — broadly applicable, a particular plus for tech/abstract/data/network topics, and not "forcing 3D"; ② an **immersive roaming scene** (a navigable 3D world + page-turn camera moves) — only for subjects with a spatial main body (architecture/celestial bodies/terrain; molecular crystals and physical products in 3D also count). **Avoid all three collapses**: don't always default to canvas particles, don't always default to immersive roaming, and don't leave the background a static dead plane. When the subject suits it, use heavy techniques boldly — don't dodge them for being hard to debug, but serve the subject rather than dazzling for its own sake.
> 6. **Chosen means delivered**: if the plan says immersive_3d / code 3D, you must truly build a 3D scene with Three.js — no mid-course downgrade to 2D particles as filler (see SKILL.md §5 signature delivery).
> 7. **Grid-texture backgrounds forbidden**: graph paper / blueprint grids / perspective grid floors / regular dot matrices / `GridHelper` are never used as backgrounds (an overused AI trope); for depth use gradient glows / shader noise / particle flow fields.

## Entrance choreography (entrance)

### Class-based staggered reveal (the all-purpose foundation)
Page elements float up and fade in one after another. **Universal across all styles; the formal tier's default first pick**.

```css
.slide .reveal{opacity:0;transform:translateY(18px)}
.slide.active .reveal{animation:rev .8s cubic-bezier(.2,.7,.2,1) both}
.slide.active .reveal.d1{animation-delay:.1s}
.slide.active .reveal.d2{animation-delay:.25s}
.slide.active .reveal.d3{animation-delay:.4s}
.slide.active .reveal.d4{animation-delay:.55s}
@keyframes rev{to{opacity:1;transform:translateY(0)}}
/* Variant: numbering-free container, children auto-stagger */
.slide.active .stagger>*{opacity:0;animation:rev .8s cubic-bezier(.2,.7,.2,1) both}
.slide.active .stagger>*:nth-child(1){animation-delay:.1s}
.slide.active .stagger>*:nth-child(2){animation-delay:.22s}
.slide.active .stagger>*:nth-child(3){animation-delay:.34s}
```

Parameters: duration .6–1.1s; step .08–.18s; offset 12–24px; ≤6 tiers, total chain ≤1.2s; easing fast-then-slow.
Pitfalls: fill-mode must be both (the from frame covers the delay period, otherwise elements flash in then vanish); animations must hang under `.slide.active` to replay on return; nth-child counts all sibling nodes — tier count must cover the actual child count (a `:nth-child(n+5)` catch-all helps).

### SVG path draw-in (--len variable)
Line-chart strokes/routes/illustration strokes are drawn out as if by a pen, with labels fading in afterwards. **Formal-tier OK** (data reveals, diagrams, maps); a bonus for hand-drawn/ink styles.

```css
.draw{stroke-dasharray:var(--len,1500);stroke-dashoffset:var(--len,1500);
  transition:stroke-dashoffset 2.2s cubic-bezier(.6,0,.3,1)}
.slide.active .draw{stroke-dashoffset:0}          /* the transition form auto-rewinds off-page and replays on re-entry */
.late{opacity:0}
.slide.active .late{animation:lateFade .8s ease forwards}
.slide.active .late.l1{animation-delay:.7s}
.slide.active .late.l2{animation-delay:1.3s}
@keyframes lateFade{to{opacity:1}}
```
```html
<path class="draw" style="--len:1300" d="…" fill="none" stroke="url(#grad)"
      stroke-width="2.3" stroke-linecap="round"/>
```

Parameters: --len ≥ `path.getTotalLength()` (add 5–10% headroom; long lines 800–1500, small dots 30–80); single stroke 1.5–2.6s; multi-stroke stagger .3–.5s; labels start .6s after the strokes.
Pitfalls: too small a --len means "a segment is already drawn at the start" or dashed segments repeating midway; the shape must be fill:none or low fill-opacity(.18), otherwise color blocks flash in before the lines; the animation form must use forwards.

### Data motion: count-up + bar reset-and-grow
Big KPI numbers ease-out roll to their targets, bars grow with a stagger. **Standard equipment for formal-tier data pages**.

```js
function countUp(el, target, dur=1400, fmt=v=>v.toFixed(1)){
  const t0=performance.now();
  (function tick(t){
    const k=Math.min(1,(t-t0)/dur), e=1-Math.pow(1-k,3);   // cubic ease-out
    el.textContent=fmt(e*target);
    if(k<1) requestAnimationFrame(tick); else el.textContent=fmt(target); // final frame snaps to the exact value
  })(performance.now());
}
// Bars replay on each page entry: zero out first, wait a beat, then assign target widths with stagger (width animates via CSS transition)
const obs=new MutationObserver(()=>{ if(!s.classList.contains('active'))return;
  bars.forEach(el=>el.style.width='0%');
  setTimeout(()=>bars.forEach((el,i)=>setTimeout(()=>el.style.width=el.dataset.w+'%',i*120)),100);
});
obs.observe(s,{attributes:true,attributeFilter:['class']});
```

Parameters: count-up 1.2–1.8s; bar transition 0.8–1.2s, per-bar stagger 100–150ms; target values live in data-* decoupled from display; thousands separators via toLocaleString.
Pitfalls: trigger on page-turn timing, not load; the number container must have `font-variant-numeric:tabular-nums` to prevent per-frame width jitter reflowing the layout; setting width directly doesn't replay — you must zero out and wait ≥100ms before assigning.

### Springy popIn stagger (CSS-variable parameterized + idle wiggle)
Cards/stickers pop in one by one with overshoot and tilt, then settle into an infinite gentle sway to stay "alive". **Free tier only** (stickers/comics/children/Memphis/pop).

```css
.reveal{opacity:0}
.slide.active .reveal{
  animation:popIn .7s cubic-bezier(.34,1.56,.64,1) var(--d,0s) both,
            wiggle 3.5s ease-in-out calc(var(--d,0s) + .8s) infinite}
@keyframes popIn{
  0%{opacity:0;transform:translateY(40px) scale(.55) rotate(calc(var(--rot,0deg) - 18deg))}
  100%{opacity:1;transform:translateY(0) scale(1) rotate(var(--rot,0deg))}}
@keyframes wiggle{
  0%,100%{transform:rotate(var(--rot,0deg)) translateY(0)}
  50%{transform:rotate(calc(var(--rot,0deg) + 2.5deg)) translateY(-5px)}}
```
```html
<div class="card reveal" style="--d:.25s;--rot:-3deg">…</div>
```

Parameters: duration .5–.8s; step .08–.15s; final tilt ±2–8deg alternating sign between neighbors; overshoot bezier second parameter 1.3–1.7; wiggle period 3–5s, amplitude ±2–3deg / lift 3–6px.
Pitfalls: both animations must write `rotate(var(--rot))` in every keyframe, or the angle jumps at handoff; wiggle's delay must be ≥ popIn's completion time; ≤3 infinitely looping elements per page.

### Heavy-object slam (seal stamp / giant character drop + screen shake)
A seal/giant character slams down from above with overshoot, micro-tremor, and a glow burst. **Theme-oriented free tier** (Chinese style/retro/epic/launch highlight pages).

```css
.slide.active .stamp{animation:drop 1.8s cubic-bezier(.34,1.56,.64,1) both}
@keyframes drop{
  0%{opacity:0;transform:rotate(-18deg) scale(3)}
  45%{opacity:1;transform:rotate(-5deg) scale(1.08)}
  58%{transform:rotate(-9deg) scale(.97) translate(1.5px,-1.5px)}   /* micro-tremor on landing */
  100%{opacity:.85;transform:rotate(-8deg) scale(1)}}               /* .85 reads more like real seal ink */
/* Giant-character version: 0%{translateY(-220px)} → hits at 65% with overshoot and a three-layer gold text-shadow burst → settles;
   when the last character lands, JS adds a shake class to the .slide */
@keyframes shake{0%,100%{transform:none}22%{transform:translate(-5px,3px)}
  48%{transform:translate(4px,-2px)}72%{transform:translate(-3px,1px)}}
```

Parameters: initial scale 2.5–3.5 or drop height 180–260px; per character .4–.7s, inter-character stagger .3–.4s; micro-tremor 1–2px, screen shake 3–6px/0.4s; delay the whole thing to land after the page's other animations as the finale.
Pitfalls: replay requires the remove class → `void el.offsetWidth` forced reflow → add class triple; shake hangs on the slide container and the slide itself must carry no other transform; per-frame text-shadow animation is expensive — only on a few large characters, with will-change.

### Character-level split ceremonial title
The title splits into per-character spans entering one by one: tech tier blur depth-of-field focus, culture tier calligraphic stroke outlines, document tier character-by-character light-up. **Used with restraint it can enter formal-tier covers**.

```js
[...title.textContent].forEach(ch=>{ const sp=document.createElement('span');
  sp.innerHTML = ch===' ' ? '&nbsp;' : ch; title.appendChild(sp); });   // spans must be inline-block
function play(){ title.querySelectorAll('span').forEach((sp,k)=>{
  sp.style.transition='none';
  sp.style.opacity='0'; sp.style.transform='scale(2)'; sp.style.filter='blur(8px)';
  void sp.offsetWidth;                                  // forced reflow is required to replay
  sp.style.transition=`opacity .8s ease ${k*.04}s, transform .8s cubic-bezier(.2,.8,.2,1) ${k*.04}s, filter .8s ease ${k*.04}s`;
  setTimeout(()=>{ sp.style.opacity='1'; sp.style.transform='none'; sp.style.filter='none'; },30);
});}
```

Parameters: per-character stagger .03–.08s (Chinese titles of ≤8 characters may relax to .2–.8s); blur 6–12px, starting scale 1.5–2.5; calligraphy-outline version stroke-dasharray 2500–3500, with fill-opacity fading in as the stroke nears completion.
Pitfalls: split by code point `[...]` to avoid breaking Chinese/emoji; replace spaces with `&nbsp;` to prevent span collapse; before replay you must set transition:none + force reflow, otherwise the browser merges styles and nothing plays.

## Cross-page transitions (transition)

### Directional blur dissolve (the visibility-delay trick)
The new page dissolves in with directional shift + blur while the old page exits in reverse. Zero JS animation code. **The formal tier's default first pick, universal across styles**.

```css
.slide{position:absolute;inset:0;opacity:0;visibility:hidden;pointer-events:none;
  transform:translateX(40px);filter:blur(8px);
  transition:opacity .6s ease,filter .6s ease,transform .6s ease,
             visibility 0s linear .6s}      /* truly hide only after the exit finishes playing */
.slide.active{opacity:1;visibility:visible;pointer-events:auto;
  transform:none;filter:none;
  transition:opacity .6s ease,filter .6s ease,transform .6s ease,
             visibility 0s linear 0s}       /* visible immediately on entrance */
.slide.prev{transform:translateX(-40px)}    /* already-seen pages exit to the left */
```
```js
slides.forEach((s,i)=>{ s.classList.remove('active','prev');
  if(i===idx) s.classList.add('active'); else if(i<idx) s.classList.add('prev'); });
```

Parameters: offset 16–60px; blur 6–8px; duration .5–.9s; dark/underwater themes may stack `brightness(.3)` with filter lagging opacity by .2–.4s for a two-stage depth effect.
Pitfalls: the visibility delay on `.active` must be zeroed or the new page just waits; the delay on non-active must not be dropped either or the old page vanishes instantly; full-page blur ≤8px to control GPU cost.

### zoom-through (three-state scale)
The camera pushes forward through every page: future pages shrink in the distance, passed pages enlarge, fade, and fly past the lens. **Tech/pitch/data styles**, formal-tier OK.

```css
.slide{position:absolute;inset:0;opacity:0;visibility:hidden;pointer-events:none;
  transform:scale(.5);
  transition:opacity .7s ease,transform .95s cubic-bezier(.45,.05,.2,1),visibility 0s .7s}
.slide.active{opacity:1;visibility:visible;pointer-events:auto;transform:scale(1);z-index:2;
  transition:opacity .7s ease,transform .95s cubic-bezier(.45,.05,.2,1),visibility 0s 0s}
.slide.prev{opacity:0;visibility:visible;transform:scale(1.65);
  transition:opacity .7s ease,transform .95s cubic-bezier(.45,.05,.2,1),visibility 0s .7s}
```

Parameters: entrance start scale .4–.6; exit end 1.5–1.8; opacity .2–.3s shorter than transform, layering "see it clearly first, then it settles".
Pitfalls: the exiting page's visibility switch must be delayed, otherwise there is no fly-past moment; `.active` needs z-index to sit above the enlarging `.prev`; recompute the prev set from `idx<i` each time so backward paging stays correct.

### Three-state directional push / 3D page flip
Future pages wait tilted off the right edge, read pages get pushed off-screen left — like comic panels advancing sideways or flipping a magazine. **Free tier** (comic/pop); the page-flip variant may enter the formal tier (art books/editorial).

```css
.slide{position:absolute;inset:0;opacity:0;visibility:hidden;
  transform:translateX(115%) rotate(6deg);
  transition:transform .55s cubic-bezier(.55,.05,.3,1.05),opacity .35s .05s,visibility .35s}
.slide.active{opacity:1;visibility:visible;transform:none}
.slide.prev{transform:translateX(-115%) rotate(-6deg);opacity:0}
/* Page-flip variant (formal tier): transform:perspective(2200px) rotateY(-14deg) translateX(60px),
   .prev uses rotateY(8deg) + transform-origin:right center, spines swapped left/right */
```

Parameters: offset 110–120% (with rotation the diagonal pokes past 100%); tilt 4–8deg; bezier last parameter 1.0–1.05 for a slight rebound; page-flip version perspective 1800–2600px, asymmetric in/out angles (-14/+8deg).
Pitfalls: must be three-state (default/active/prev) — two states reverse the backward direction; the deck container must be overflow:hidden; exiting pages' opacity must reach 0 so off-screen pages don't show when sweeping past.

### Overlay masked page change (curtain / palace gates / lights-out)
Two themed door panels close over the screen, the page changes backstage, then they pull open; or a global brightness lights-out/lights-on. Makes the transition itself a narrative element. **Theme-oriented** (theater/palace/museum/cinematic); the lights-out variant may enter the formal dark tier.

```js
let busy=false;
function go(n){
  if(busy) return; busy=true;
  curL.style.transform=curR.style.transform='translateX(0)';     // close
  setTimeout(()=>{
    setSlide(n);                                                 // change page backstage
    requestAnimationFrame(()=>requestAnimationFrame(()=>{        // double rAF waits for the closed state to commit
      curL.style.transform='translateX(-101%)';                  // ±101% prevents sub-pixel gaps
      curR.style.transform='translateX(101%)';
      setTimeout(()=>busy=false,1100);
    }));
  },700);
}
/* Lights-out variant: background canvas gets .dim{filter:brightness(.04)} synced with slide brightness(.06);
   change the page at ≈80% of the transition (the black point); keep low brightness at .03–.08 to preserve a residual glow */
```

Parameters: door-panel transition 0.9–1.2s, heavy-object easing cubic-bezier(.55,0,.25,1); the page can change after ≈0.6×transition of close time; unlock delay ≈ the transition duration; gold trim + drop shadow + fabric texture on the panels raises quality a lot.
Pitfalls: before pulling open you must double-rAF (or force reflow), otherwise the two transforms merge and the curtain never moves; the busy lock prevents rapid presses jamming it half-open; the overlay z-index sits above all slides with pointer-events:none.

### Themed particle transition curtain + white-flash pulse
On each page turn, scatter a wave of themed particles on the top layer (falling leaves/petals/bubbles/pixel bursts), optionally stacking a 0.15s accent-color flash. **Free tier only**; the theme shapes must be replaced with the deck's motif.

```js
function spawnParticles(){                       // call inside the page-turn function
  for(let i=0;i<7;i++){
    const el=document.createElement('div'); el.className='fall-p';
    el.style.left=Math.random()*1280+'px'; el.style.top='-40px';
    el.style.setProperty('--xEnd',(Math.random()*200-100)+'px');
    el.style.setProperty('--yEnd','780px');                       // drifts out past the canvas
    el.style.setProperty('--rotEnd',(Math.random()*720-360)+'deg');
    el.style.animationDelay=Math.random()*.4+'s';
    el.innerHTML='<svg viewBox="0 0 30 30">…theme shape…</svg>';
    layer.appendChild(el); setTimeout(()=>el.remove(),2200);      // self-cleanup as double insurance
  }
}
/* @keyframes fall{15%{opacity:1}100%{transform:translate(var(--xEnd),var(--yEnd))
   rotate(var(--rotEnd));opacity:0}}  forwards
   White-flash variant: .flash.pulse{animation:goldFlash .15s} (alpha 0→.32→0); re-trigger via the reflow triple */
```

Parameters: 5–10 DOM particles per wave (cap ~15); duration 1.4–2s; cleanup time > animation duration + max delay; the canvas version's bubbles/pixel bursts may reach 70–800 particles, total 500–700ms.
Pitfalls: the overlay z-index sits above all slides, pointer-events:none; rapid page turns rely on the setTimeout remove to prevent DOM pile-up; the canvas version's final frame must clearRect to avoid residue.

### Single 3D world camera waypoint moves
The whole deck shares one persistent Three.js scene; each page has a {pos, look} rig; a page turn = the camera swooping over along an arc with a raised midpoint, while the HTML content only fades in/out. **The standard transition paired with "Code-built 3D scene"** — any deck using a 3D scene background should use it (architecture/culture & tourism/tech/geography/astronomy/product), turning "page turns" into "moving the camera through a 3D world". Worth adopting eagerly; don't avoid it just because it's 3D.

**★Iron rule: the look-at point anchors to the current page's subject; no numbers made up off the top of your head.** The camera `look`'s target must be the **real coordinates of the very subject this page's copy is discussing** (that mesh/Group) — reference its `.position` directly, never hand-write a set of similar-looking `V(x,y,z)`. This rule is the **only correct posture** for "the background moving along with page turns" in 3D roaming decks, and it generalizes to any spatial subject: discussing the Sun → aim the camera at the Sun sphere; discussing dougong brackets → push the camera toward that building's bracket assembly; discussing some molecular group → aim at that atom cluster. Hand-copied coordinates inevitably drift ("looking into the void" from a dozen units off); half-filling two subjects also jams the camera into **the gap between them**, aimed at neither — this is exactly the root of decks looking like "the background spinning aimlessly, unrelated to content".

The method in three steps: ① when building the scene, register every subject that might be discussed as a **named anchor** (see `ANCHORS` in "Code-built 3D scene"); ② rig-table `look`s **reference anchors** instead of copying coordinates; ③ derive `pos` from the anchor + a view direction/distance — don't invent it in isolation. When discussing a **local detail** of a subject, move the camera in (reduce the distance to the anchor / narrow the fov) and push the lens onto it, rather than idling far away in a wide shot.

```js
// Prerequisite: ANCHORS is filled while building the scene (see "Code-built 3D scene"); key=subject name, value=mesh/Group
// A rig = a record of "anchor name + view direction dir + distance dist + look offset"; coordinates are evaluated live from the anchor at page-turn time
const V=(x,y,z)=>new THREE.Vector3(x,y,z);
const SLIDES=[
  {a:'earth', dir:V(1,.4,1.2), dist:24},                 // discussing Earth: stand 24 units diagonally above, looking at Earth itself
  {a:'moon',  dir:V(.8,.3,1),  dist:10},                 // discussing the landing: push in to 10 units from the Moon (detail → closer)
  {a:'sun',   dir:V(.6,.5,1),  dist:40},                 // discussing the Sun: aim at the Sun sphere (no longer never-in-frame)
  /* …one per page; look always comes from a real anchor, never a bare V(x,y,z) coordinate */
];
// Lazy evaluation: read the anchor's current .position at page-turn time (tracks a moving subject too), producing {look,pos}
function shot(s){
  const p=ANCHORS[s.a].position;                         // ★the look-at point = the anchor's real coordinates themselves, not a copy
  return { look:p.clone().add(s.off||new THREE.Vector3()),
           pos: p.clone().add(s.dir.clone().normalize().multiplyScalar(s.dist)) };
}
let camAnim=null; const curLook=new THREE.Vector3();
function tweenTo(idx){
  if(camAnim) camAnim.cancelled=true;                  // the old tween must be interruptible
  const dst=shot(SLIDES[idx]);                          // derive the target rig from the anchor right now
  const from=camera.position.clone(), to=dst.pos;
  const mid=from.clone().lerp(to,.5);
  mid.y=Math.max(mid.y, Math.max(from.y,to.y)+18);     // raised midpoint → swooping arc
  const curve=new THREE.QuadraticBezierCurve3(from,mid,to);
  const fromLook=curLook.clone(), t0=performance.now(), anim={cancelled:false};
  camAnim=anim;
  (function step(){
    if(anim.cancelled) return;
    const t=Math.min(1,(performance.now()-t0)/2000);
    const e=t<.5 ? 2*t*t : 1-Math.pow(-2*t+2,2)/2;     // easeInOutQuad
    camera.position.copy(curve.getPointAt(e));
    curLook.lerpVectors(fromLook,dst.look,e);          // the look-at point must interpolate in sync
    camera.lookAt(curLook);
    if(t<1) requestAnimationFrame(step); else camAnim=null;
  })();
}
```

Parameters: flight 1.5–2.3s; HTML slide opacity transition 0.6–0.9s overlapping the camera-move start; after settling, idle breathing drift `sin(t*.18)*0.5` (only when !camAnim); routes that would clip through geometry may use a forceStart teleported origin for a fly-through dolly. If the subject itself moves (planetary orbits, rotating parts), a `look` referencing `.position` tracks it and naturally never goes off-target; `SLIDES` only needs the current coordinates read once at `tweenTo` time.
Pitfalls: ① **`look` copying a coordinate instead of referencing the anchor → drift/off-target** (discussing a subject while the camera floats beside empty space, or two subjects half-filled jamming the camera into the gap between them) — the number-one root of "the background spinning aimlessly"; always use `ANCHORS[name].position`; ② lerping only position without interpolating look snaps the head instantly; ③ rapid consecutive page turns need the cancelled flag to stop old and new tweens fighting over the camera; ④ progress based on performance.now, not frame counts; ⑤ hard-code the layering: canvas 1 < slide 5 < HUD 50.

## Global dynamic backgrounds (background)

> **The background should be "alive" by default** (continuous micro-motion; a pure static gradient loses points unless the foreground carries persistent motion). **Pick the type by subject**: tech/AI/abstract/data/network → "3D hero celestial body (node sphere / Fresnel sphere)" or "shader flow field" — **these are lively background layers, need not be navigable scenes, and are broadly applicable**; subjects with a spatial main body (architecture/celestial bodies/terrain) → "Code-built 3D roaming scene"; nature/humanities/data → "particle flow field", "Canvas-2D constellation", "shader noise", "CSS procedural". **Don't do static gradients, and don't always do particles / always do 3D roaming** — pick whichever expresses this subject best.

### Code-built 3D scene (architecture / terrain / celestial bodies — Forbidden-City-grade signature) ★high ceiling
Use parametric geometry to build a **real 3D scene** in code as the show's background spine: BoxGeometry lays platforms/walls, PlaneGeometry spreads water/ground, Cone/Cylinder make roofs/bamboo, Torus makes arched bridges, Sphere makes rocks/celestial bodies, lit by the Ambient+Directional+Point trio; the whole deck shares this one persistent scene, and page turns drive the camera along arcs (see "Single 3D world camera waypoint moves"). **The signature ace for architecture/culture & tourism/astronomy/science/product subjects**; the reference benchmark (Forbidden City / garden 3D roaming) is this very pattern.

**★Don't stop the materials at flat `MeshLambertMaterial` colors (instantly fake plastic bricks)** — what material each object needs and how to tune it, derive by subject via the realism four questions + capability mapping table in the "High-fidelity 3D" section (stone-and-wood architecture is one case; glowing celestial bodies, organic life, metallic machinery all differ). The PBR/shadow/tone-mapping below is the **universal groundwork** (nearly all realistic scenes turn it on) — safe to copy; but the material parameters are one example for "stone architecture", not a default for every 3D scene.

```js
const scene=new THREE.Scene(); scene.fog=new THREE.Fog(0x0b1a2a,40,160);   // fog color matched to --bg
const renderer=new THREE.WebGLRenderer({canvas,antialias:true,alpha:true});
renderer.setPixelRatio(Math.min(devicePixelRatio,2)); renderer.setSize(1280,720);
renderer.shadowMap.enabled=true; renderer.shadowMap.type=THREE.PCFSoftShadowMap;   // ★real shadows
renderer.toneMapping=THREE.ACESFilmicToneMapping; renderer.toneMappingExposure=1.1;// ★cinematic tone mapping
renderer.outputEncoding=THREE.sRGBEncoding;                                        // r128 uses outputEncoding (not colorSpace)
const camera=new THREE.PerspectiveCamera(50,1280/720,.1,500);
scene.add(new THREE.AmbientLight(0x88aacc,.5));
const sun=new THREE.DirectionalLight(0xffe6b0,1.6); sun.position.set(40,60,30);     // key light strong, casts shadows
sun.castShadow=true; sun.shadow.mapSize.set(2048,2048);
sun.shadow.camera.left=-120; sun.shadow.camera.right=120;                           // shadow camera covers the scene
sun.shadow.camera.top=120; sun.shadow.camera.bottom=-120; sun.shadow.bias=-0.0004;  // bias prevents acne
scene.add(sun);
// ★PBR material factory: stone/wood/tile get different roughness, metal parts get metalness (colors still from deck tokens)
const M=(c,rough=0.85,metal=0.0)=>new THREE.MeshStandardMaterial({color:c,roughness:rough,metalness:metal});
const ANCHORS={};                                                          // ★named-anchor registry: camera rigs aim at subjects through it
function place(name,group,x,y,z){ group.position.set(x,y,z); scene.add(group); ANCHORS[name]=group; return group; }
function building(x,z,w,h,d,roof){                                          // parameterized "one building"
  const g=new THREE.Group();
  const base=new THREE.Mesh(new THREE.BoxGeometry(w,h,d),M(0x7a3b2e,0.9)); base.position.y=h/2;
  const top=new THREE.Mesh(new THREE.ConeGeometry(w*0.85,h*0.5,4),M(roof,0.6)); // four-corner pyramidal roof (tiles slightly glossier)
  top.position.y=h+h*0.25; top.rotation.y=Math.PI/4;
  g.add(base,top); g.traverse(o=>{o.castShadow=true;o.receiveShadow=true;});  // every mesh casts + receives shadows
  return g;
}
for(let i=0;i<12;i++) place('hall'+i, building(0,0,10,6+Math.random()*6,8,0x123a2a),
  (i%4-1.5)*16,0,(Math.floor(i/4)-1)*20);                                  // 12+ entities stack into a courtyard, each registered as an anchor
place('mainHall', building(0,0,16,10,12,0x123a2a), 0,0,-20);               // a key subject that will be discussed — give it a semantic name
const ground=new THREE.Mesh(new THREE.PlaneGeometry(400,400),M(0x0e2233,0.95));     // water/ground
ground.rotateX(-Math.PI/2); ground.receiveShadow=true; scene.add(ground);          // ground receiving shadows → grounds the spatial depth
(function loop(){ requestAnimationFrame(loop); renderer.render(scene,camera); })();
```

Parameters: ≥10 entities before it feels like a "scene" (Forbidden-City grade: 50–180 meshes); reuse geometry via `Group` + loop generation, don't hand-write a pile; the light trio (ambient + key directional + warm point) — missing any one leaves the scene gray; camera fov 45–55; the fog color must equal `--bg` or the far view shows a "hard edge". **★Every subject that some page's copy will discuss (that building/that star/that part) gets registered into `ANCHORS` with a semantic key at build time** — this is the prerequisite for "Single 3D world camera waypoint moves" rigs to aim at subjects instead of "spinning aimlessly"; pure background meshes never individually discussed (ground, distant clusters) need not be registered.
Pitfalls: lock Three.js r128 (see SKILL §8); `MeshStandard/Lambert/Phong` all need lights or everything is black; `MeshStandard` metal parts go dark without an envMap (give them an environment reflection, see "High-fidelity 3D"); build all geometry once at skeleton time, per page only move the camera — never rebuild the scene; cap `pixelRatio` at 2 and shadow.mapSize ≤ 2048 to avoid stalls; the canvas lives in `#bg-layer` at the lowest z-index.
**For more realism (realistic material textures · environment reflections · post-processing bloom) → see the next section "High-fidelity 3D".**

### High-fidelity 3D (the realism mindset: making objects "look real") ★turning "plastic bricks" into "photo-grade"
When the subject needs realism (architecture/products/machinery/vehicles/anatomy/biology/nature/landforms…, **you judge by subject whether to push it and how far**), basic geometry + flat `MeshLambert` colors are far from enough — that's the plastic toy-brick look. **But there is no universal copyable recipe for "realism": metal lives on reflections, jade on transmission, leaves on subsurface scattering, clouds and mist on volume, velvet on anisotropy — for each subject to look real, the leverage point is completely different.** So this section gives you no material code library (copying some material only makes you slap metal reflections onto a leaf — faker), but instead teaches **how to think + how to translate the thought into Three.js capabilities, then write the implementation yourself**.

**The realism four questions (for any subject, ask yourself these four first; the answers decide what code you write):**
1. **How does this surface respond to light?** Reflection (metal/water/lacquer), diffuse (stone/earth/matte plastic), transmission and refraction (glass/water/ice), or light entering and scattering back out (jade/skin/leaves/wax/milk)?
2. **What surface details does it have?** Texture (wood grain/brick joints/brushed metal), relief (normals), uneven local gloss (wear/oil stains/water marks)?
3. **Is the light environment right?** Is there an environment to reflect, are real shadows landing, is the ambient warmth/coolness on-subject?
4. **Does the overall frame read like a photo?** Tone mapping, bloom, depth of field, atmosphere — these are the "lens feel".

**Realism dimension → Three.js capability (translate "the effect I want" into API; the concrete values/implementation you tune yourself by subject):**

| What you want to express | Which capability to use (write the implementation yourself) |
|---|---|
| Metal/lacquer reflections, gleaming | `MeshStandardMaterial` with high `metalness`; **there must be an environment to reflect** (see the "environment" scaffold below), otherwise it turns dead black-gray |
| Matte stone/earth/wood/concrete | `MeshStandardMaterial` with high `roughness` low `metalness` + procedural `map`/`normalMap` for detail |
| Glass/water/ice (transmission & refraction) | `MeshPhysicalMaterial`'s `transmission`+`ior`+`thickness`; thin shells may also use `transparent+opacity` |
| Jade/skin/leaves/wax (subsurface feel) | No built-in SSS: translucent material + backlight (a light behind the object) + edge Fresnel to approximate the "glowing rim" |
| Velvet/hair/brushed metal (anisotropy) | `MeshPhysicalMaterial`'s `sheen`/`clearcoat`, or a directional `roughnessMap` |
| Surface texture/relief/wear | Procedural `CanvasTexture` (noise/drawing) as `map`/`normalMap`/`roughnessMap` — **no external images** (see the "texture" scaffold below) |
| Clouds/fog/smoke/light shafts (volume) | `FogExp2` + layered translucent sprites/planes + additive `AdditiveBlending`; light shafts via translucent cone meshes |
| Rim-light edges (backlit creatures/thin leaves) | Fresnel: brighter as the view-normal angle grows; custom shader or `MeshPhysicalMaterial` approximation |
| Grounded shadows, spatial depth | `shadowMap` (see the "shadow" scaffold below) — **the single biggest realism factor**; nearly every solid scene should enable it |
| Lens-grade imaging (bloom/tone) | Post-processing `EffectComposer` (see the "post-processing" scaffold below) + ACESFilmic tone mapping |

> The table only says "which capability to use", **not finished materials** — the values, what the texture looks like, how many layers stack: you derive them from "what exactly is this thing, under what light". That is the key to realism: observe how real objects respond to light, then use the capabilities above to reproduce it — don't apply templates.

**Three subject-agnostic universal scaffolds (the groundwork every realistic scene builds; these can be used directly):**

*Environment (gives reflective materials something to reflect; otherwise metal/glass turn dead black):*
```js
const pmrem=new THREE.PMREMGenerator(renderer);
const envScene=new THREE.Scene(); envScene.background=new THREE.Color(0x??????);   // environment base color matched to the subject's warmth
const envTex=pmrem.fromScene(envScene,0,0.1,100).texture;                          // procedural, no external HDRI needed
scene.environment=envTex;                                                          // all PBR materials in the scene consume it automatically
// For "more content to reflect" (so metal doesn't go gray): add a big BackSide sphere / a few bright panels to envScene as reflection sources
```
*Shadows (the biggest fake-to-real lever):* `renderer.shadowMap.enabled=true; type=PCFSoftShadowMap`; the key light `castShadow`, the shadow camera covering the scene, `bias≈-0.0004` against acne; objects `castShadow`, the ground `receiveShadow`.
*Post-processing (lens-grade bloom/tone):* `renderer.toneMapping=ACESFilmicToneMapping` (a free quality boost, nearly always on); bloom via `EffectComposer`+`RenderPass`+`UnrealBloomPass`. **★r128 post-processing modules 404 on cdnjs; they must come from unpkg/jsdelivr at the r128 version** (still locked to r128):
```html
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script src="https://unpkg.com/three@0.128.0/examples/js/postprocessing/EffectComposer.js"></script>
<script src="https://unpkg.com/three@0.128.0/examples/js/postprocessing/RenderPass.js"></script>
<script src="https://unpkg.com/three@0.128.0/examples/js/postprocessing/ShaderPass.js"></script>
<script src="https://unpkg.com/three@0.128.0/examples/js/shaders/CopyShader.js"></script>
<script src="https://unpkg.com/three@0.128.0/examples/js/shaders/LuminosityHighPassShader.js"></script>
<script src="https://unpkg.com/three@0.128.0/examples/js/postprocessing/UnrealBloomPass.js"></script>
```
`UnrealBloomPass` depends on `LuminosityHighPassShader`+`CopyShader`+`ShaderPass`; missing any one gives `undefined` — don't scramble the `<script>` order. The render loop uses `composer.render()` instead of `renderer.render()`.

**Procedural texture mindset (surface detail without external images):** draw on an offscreen `canvas` → `CanvasTexture` → use as `map`/`normalMap`/`roughnessMap`. What to draw is set by the subject (wood grain runs in long streaks, stone in dense speckles, water in ripples). **Universal keys: enough contrast** (base color + thousands of dense two-way light/dark noise dots + structural lines, three layers stacked, before texture shows; sparse dots get washed to a flat color by tone mapping); **structural lines must be irregular/offset — never a regular square lattice** (a regular grid trips SKILL §7's "no grid textures" stiff look).

**Headless-rendering discipline (mandatory; post-processing crashes most easily):**
- **Smoke-test first**: for any post-processing/complex material, first write a minimal deck and run `render_deck.py --page 1`; confirm `console_errors` is empty and the effect really shows before rolling it out. Don't discover the CDN didn't load / API names don't match r128 after writing the whole thing.
- **Fallback plan**: wrap post-processing in `try{}`, falling back to `renderer.render()` — never let post-processing failure take down the whole background.
- **Performance budget**: `pixelRatio≤2`, `shadow.mapSize≤2048`, bloom resolution capped; it must finish rendering within the headless 2.6s screenshot window.
- **Same criterion as the overall fancy principle**: realism gains that serve the subject go in; piling it up into stalls/timeouts/errors for show is a loss. Realism is a bonus, not a hard gate.

### Background-foreground separation scrim (mandatory on content pages of 3D / heavy-dynamic-background decks)
When SVG diagrams, dense annotations, or data tables sit on a full-screen 3D / particle background, the foreground **must carry its own backing**, otherwise the background's highlights/geometry/drifting particles bleed up through the stroke gaps, compete with the foreground line work, and smear the text. **Don't globally dim the background** (it kills the 3D on covers/section pages); back only the foreground container locally, and de-densify the background by page type.

```css
/* Soft-edged radial scrim: holds the diagram without cutting a hard box; the 3D still shows through around (first choice) */
.diagram-wrap{position:relative;background:radial-gradient(70% 70% at 50% 50%,
  rgba(12,16,20,.85) 0%, rgba(12,16,20,.55) 60%, rgba(12,16,20,0) 100%)}  /* rgba in the same hue as --bg */
/* At high information density use a card + backdrop-filter: blur smears the background's high-frequency detail and the line work pops out immediately */
.data-card{background:linear-gradient(180deg,rgba(12,16,20,.78),rgba(12,16,20,.9));
  backdrop-filter:blur(6px);border:1px solid rgba(255,255,255,.08)}
```
```js
// De-densify the background by page type: entering content/data pages lowers particles and highlights; returning to the cover restores them
function bgIntensity(type){ const dim = (type==='content'||type==='data');
  dust.material.opacity = dim ? .25 : .55;        // dust motes
  if(glow) glow.intensity = dim ? .5 : 1.8; }     // key light/glow
```
Parameters: scrim center alpha .8–.9, .5 at 60%, 0 at the edge; card base .75–.92 + blur 5–8px; content-page particle opacity cut to ~.25–.3, glow halved.
Pitfalls: **a bare `<svg><g stroke>` hung directly on `.slide` is never allowed** (the background will bleed through); scrim/card rgba must be the same hue as `--bg` (mismatched hues turn gray); each backdrop-filter block is one offscreen composite — ≤ a few blocks on dense pages; **don't add these backings to covers/section pages** (they should expose the full 3D); stroke-opacity ≥ .5 and width ≥ 1.2 — however faint, never below background contrast.

### 2D canvas ambience particles (glowing dust / three-layer parallax starfield)
A few dozen ultra-slow glowing dots drift globally, instantly bringing a dark deck "alive". **The lightest background of all — not the default answer**: when the subject can carry 3D/immersion, prefer the heavy signature; use this when no concrete space is truly needed. **Formal-tier OK** (fewer particles, slower speed, low alpha).

```js
const colors=['rgba(94,234,212,','rgba(167,139,250,','rgba(244,114,182,'];  // replace with the deck's theme colors
const ps=Array.from({length:42},()=>({x:Math.random()*1280,y:Math.random()*720,
  vx:(Math.random()-.5)*.18, vy:(Math.random()-.5)*.18,
  r:Math.random()*1.6+.4, a:Math.random()*.5+.25, c:colors[(Math.random()*3)|0]}));
(function draw(){
  ctx.clearRect(0,0,1280,720);
  for(const p of ps){
    p.x+=p.vx; p.y+=p.vy;
    if(p.x<-5)p.x=1285; if(p.x>1285)p.x=-5;            // wrap around at the edges
    if(p.y<-5)p.y=725;  if(p.y>725)p.y=-5;
    ctx.fillStyle=p.c+p.a+')'; ctx.shadowColor=p.c+'1)'; ctx.shadowBlur=8;
    ctx.beginPath(); ctx.arc(p.x,p.y,p.r,0,Math.PI*2); ctx.fill();
  }
  ctx.shadowBlur=0;                                    // reset after each frame
  requestAnimationFrame(draw);
})();
```

Parameters: 30–60 particles; speed <0.2px/frame (any faster reads as snow, not ambience); alpha .25–.75; shadowBlur 6–10; the starfield variant splits three layers (180/90/40 stars, speed ratio 1:3:7, near layer with glow) for depth parallax.
Pitfalls: shadowBlur is canvas's most expensive state — ≤60 particles and reset every frame; the canvas is fixed at 1280×720 on the bottom z-index and does not adapt to the window.

### Pure-CSS texture layers (CRT scanlines / halftone dots + mask fade)
Repeating gradients make CRT scanlines, or comic/pop halftone dots, with a mask fading the texture strongly toward the content center. Zero JS, zero assets. **Formal-tier OK** (single low-opacity layer).

> **Forbidden: grid-type backgrounds** — graph paper / blueprint grids (square tiling via `background-size:Npx Npx`) / perspective grid floors / full-bleed regular dot matrices, **never** (a badly overused AI trope, see usage note 7). For depth use gradient glows / shader noise / particle flow fields. Halftone dots are allowed only in comic·pop styles as a **style texture**, and must mask-fade hard toward the content center — never tiled full-bleed as a generic base.

```css
/* CRT scanlines (cyber/retro screen feel): thin horizontal lines, screen blending, stacked radial vignette */
.deck::before{content:'';position:absolute;inset:0;pointer-events:none;z-index:1;
  background-image:repeating-linear-gradient(0deg,rgba(0,229,255,.05) 0 1px,transparent 1px 4px);
  mix-blend-mode:screen;
  -webkit-mask-image:radial-gradient(120% 120% at 50% 50%,#000 55%,transparent 100%);
          mask-image:radial-gradient(120% 120% at 50% 50%,#000 55%,transparent 100%)}
/* Halftone dots (comic/pop only; hard fade, never full-bleed):
   background-image:radial-gradient(var(--accent) 1.4px,transparent 1.6px);background-size:14px 14px;
   opacity:.12; mask-image:linear-gradient(135deg,#000 25%,transparent 60%)  ← the hard fade is mandatory */
```

Parameters: scanlines 1px line / 3–4px gap, alpha .04–.06, screen blending; halftone dot radius 1.2–1.8px, spacing 12–26px, opacity ≤.15 with a strong mask fade.
Pitfalls: **any square tiling via `background-size:Npx Npx` counts as a grid texture — forbidden**; texture layers get pointer-events:none and a z-index below the slides; halftone appears only in comic/pop styles, never as a generic background.

### Floating glass shards + highlight sweep
Blurred color blobs underneath, 5 backdrop-filter glass shards each drifting slowly, stacked with a 7s looping diagonal highlight. **The standard cover for glassmorphism / elegant magazine styles**; restrained, it may enter the formal tier.

```css
.deck::before{content:'';position:absolute;width:600px;height:600px;border-radius:50%;
  background:var(--accent);top:-200px;left:-180px;filter:blur(90px);opacity:.32}
.shard{position:absolute;border-radius:16px;overflow:hidden;
  background:linear-gradient(135deg,rgba(111,177,255,.18),rgba(179,136,255,.07));
  backdrop-filter:blur(14px);border:1px solid rgba(255,255,255,.18);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.25),0 30px 60px rgba(0,0,0,.35)}
.shard::after{content:'';position:absolute;inset:0;
  background:linear-gradient(115deg,transparent 28%,rgba(255,255,255,.22) 50%,transparent 72%);
  animation:sweep 7s linear infinite}
@keyframes sweep{from{transform:translateX(-120%)}to{transform:translateX(120%)}}
@keyframes drift1{0%,100%{transform:translate(0,0) rotate(14deg)}
  50%{transform:translate(18px,-26px) rotate(17deg)}}
/* Each shard gets a different driftN duration 7–11s, with one of them on reverse to desynchronize */
```

Parameters: 4–6 shards, 90–300px; drift amplitude 10–26px, rotational sway ±3–5°; blob blur 80–100px, opacity .25–.35.
Pitfalls: each backdrop-filter shard is one offscreen composite — ≤6 shards; durations must be staggered + one on reverse, otherwise the whole field breathes in sync and looks very fake; drift amplitude >30px reads as a drifting-away bug.

### Flowing lines + curve particle streams
Dashed lines flowing infinitely via dashoffset (pure CSS), or a river of light dots along a Three.js CatmullRom curve, expressing routes/processes/data flows/energy flows. **The SVG version is formal-tier OK**.

```css
.flow{fill:none;stroke-width:2.4;stroke-linecap:round;
  stroke-dasharray:8 7;animation:flow 2.6s linear infinite}
@keyframes flow{to{stroke-dashoffset:-90}}   /* 90 = (8+7)×6; must be an integer multiple of the period for a seamless loop */
```
```js
// Three.js version: particles advance along the curve at their own phases
const curve=new THREE.CatmullRomCurve3(waypoints,false,'catmullrom',0.4);
for(let i=0;i<N;i++){
  prog[i]+=dt*0.13; if(prog[i]>1) prog[i]-=1;          // -=1 wraps around, keeping spacing free of jitter
  const p=curve.getPoint(prog[i]);
  pos.set([p.x, p.y+Math.sin(t*2+i*.3)*.12, p.z], i*3); // sinusoidal float
}
geo.attributes.position.needsUpdate=true;               // required every frame
// Fixed material combo: AdditiveBlending + depthWrite:false + transparent
```

Parameters: dash 6–10/gap 5–8, period 2–3.5s and must be linear; 24–300 particles, flow speed 0.1–0.25 progress/second, curve tension 0.3–0.5.
Pitfalls: an offset that isn't an integer multiple of the period hitches at the end of every loop; flow direction follows the path's drawing direction; additive particles without depthWrite:false show black square occlusions; when node/milestone markers must land on this line, take points from the same path via `getPointAtLength()` in the same source (most common on timeline pages) — HTML percent-positioned eyeballed overlay is forbidden (see SKILL §7 same-source anchoring).

### Negative-delay pre-warmed themed particle eruption
A config table batch-generates multi-layer DOM/SVG particles (smoke/fire/petals/bubbles), each on an infinite looping keyframes, with animationDelay set to negative random values so the very first frame is already full. **Free tier only**; a persistent atmospheric hero (volcano/fireworks/steam/incense).

```js
const layers=[ {count:14,cls:'smoke',size:[140,260],speed:[14,22]},
               {count:22,cls:'spark',size:[8,16],  speed:[2.5,4.2]} ];
layers.forEach(cfg=>{
  for(let i=0;i<cfg.count;i++){
    const p=document.createElement('div'); p.className=cfg.cls;
    const sz=rand(cfg.size), dur=rand(cfg.speed);
    p.style.width=p.style.height=sz+'px';
    p.style.left=spawnX()+'px';
    p.style.animationDuration=dur+'s';
    p.style.animationDelay=(-Math.random()*dur)+'s';   // negative delay = full field on frame one; the soul of the recipe
    holder.appendChild(p);
  }
});
/* One looping keyframes per class; first and last frames at opacity:0 keep the loop seam invisible */
```

Parameters: 10–25 per layer, ≤70 DOM particles total; fast/slow layer speed ratio ≥3× for depth; blur ≤12px, ≤15 large particles; randomized trajectory parameters via CSS variables.
Pitfalls: without the negative delay the opening is an awkward "empty stage slowly filling with smoke"; animations bind only under `.slide.active` so they stop off-page; the factory function checks childElementCount for idempotency against duplicate insertion.

### 3D hero celestial body (node sphere / Fresnel wireframe globe / fbm star)
A rotating 3D sphere as the hero visual: a hand-written 2D-canvas neural node sphere (zero dependencies), a Three.js lat-long wireframe globe + BackSide Fresnel glow shell, or a GLSL fbm boiling star. **The cover ace for tech/AI/geography/astronomy decks**; it can also serve **purely as a lively background layer** on any tech/abstract/data/network deck (no need to go full immersive roaming).

```js
// Node sphere (zero dependencies): 200 nodes distributed on a spherical shell; compute k=3 nearest-neighbor edges once at init (O(N²) must never enter the draw loop)
// Each frame: dual-axis rotation + perspective projection p=620/(620+z), edge opacity clamp(.4-avgZ/800, .04, .4)
// Fresnel glow shell (Three.js version):
const halo=new THREE.Mesh(new THREE.SphereGeometry(R*1.05,64,64), new THREE.ShaderMaterial({
  vertexShader:`varying vec3 vN;void main(){vN=normalize(normalMatrix*normal);
    gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.);}`,
  fragmentShader:`varying vec3 vN;void main(){float r=pow(1.0-abs(vN.z),3.0);
    gl_FragColor=vec4(vec3(.58,.84,.7),r*.45);}`,       // swap the color for the deck's theme color
  transparent:true, side:THREE.BackSide, depthWrite:false,
  blending:THREE.AdditiveBlending}));
scene.add(halo);
```

Parameters: 150–250 nodes, nearest-neighbor k 2–3, spin 0.003–0.005 rad/frame; glow shell radius ×1.03–1.1, Fresnel exponent 2.5–3.5; fbm star 5-octave (drop to 3–4 on mobile); UnrealBloom (strength ~1.0, threshold .15) lifts the whole thing a grade.
Pitfalls: the glow shell's trio BackSide+transparent+Additive — missing any one smears the sphere; give the canvas 2× internal resolution for crisp lines; pixelRatio capped at 2; when reused across pages share the build function and switch palettes via parameters.

## Interaction easter eggs (interaction)

### data-depth layered mouse parallax (DOM / 3D camera lerp)
Cards shift with the mouse at their own depths, or the 3D camera lerps toward the mouse. Cheap but distinctly premium. **At the low end of the range it may enter the formal tier**.

```js
deck.addEventListener('mousemove', e=>{
  const r=deck.getBoundingClientRect();
  const mx=(e.clientX-r.left-r.width/2)/r.width, my=(e.clientY-r.top-r.height/2)/r.height;
  document.querySelectorAll('.slide.active .parallax').forEach(el=>{
    const d=parseFloat(el.dataset.depth||1);          // <div class="parallax" data-depth=".5">
    el.style.transform=`translate(${mx*12*d}px,${my*12*d}px)`;
  });
});
deck.addEventListener('mouseleave', ()=>
  document.querySelectorAll('.parallax').forEach(el=>el.style.transform=''));
// 3D version: camera.position.x += (mx*0.5 - camera.position.x)*0.05  exponential approach, auto-recenters on leave
// Or the orientation-preserving trick: camera.position.add(off); camera.lookAt(target); camera.position.sub(off);
```

Parameters: base amplitude 8–16px (formal tier ≤8px); depth 0.5–1.6 in 2–3 tiers; 3D approach factor .04–.08; <10 parallax elements per page.
Pitfalls: the inline transform is overwritten directly — parallaxed elements can no longer use transform for layout/centering; mouseleave must reset everything or offsets linger across page turns.

### Two-way hover linkage (dim the rest + glow highlight / hard-shadow detail panel)
A list and a graphic link both ways via shared data-id: hovering either side dims the rest and glows the matching item; a variant lifts the hovered card magnetically with a live-switching detail panel on the right. **Formal-tier OK** (map/chart/architecture-diagram pages).

```css
.map.dim .item:not(.hl){opacity:.18}
.item.hl{stroke-width:4;filter:drop-shadow(0 0 6px currentColor)}
.item{transition:opacity .35s,stroke-width .35s}
/* neo-brutalist variant: .cell:hover,.cell.act{transform:translate(-3px,-3px);
   box-shadow:6px 6px 0 var(--ink)}  zero-blur hard shadow = magnetic lift */
```
```js
terms.forEach(t=>{
  t.addEventListener('mouseenter',()=>{ map.classList.add('dim');
    items.forEach(c=>c.classList.toggle('hl', c.dataset.id===t.dataset.id)); });
  t.addEventListener('mouseleave',()=>{ map.classList.remove('dim');
    items.forEach(c=>c.classList.remove('hl')); });
});
```

Parameters: dim opacity .15–.25; highlight stroke ×1.5–2, drop-shadow 4–8px using currentColor; transition .3–.4s; hard-shadow offset = 2× the lift, blur constant 0.
Pitfalls: hang dim on the container + exclude with `:not(.hl)` — cleaner than JS restyling each item; mouseleave must clear dim and hl completely; the deck-level click-to-page listener must exclude these interactive zones via `e.target.closest(...)`.

### Raycaster click-to-focus + info card slide-in
Click a target in the 3D scene → the camera flies to an orbit position + a frosted-glass info card slides in; click empty space to reset. **The core interaction loop of 3D decks** (product showcases/galaxies/map exploration).

```js
cv.addEventListener('mousedown',e=>{down={x:e.clientX,y:e.clientY};drag=false});
cv.addEventListener('mousemove',e=>{ if(down &&
  (e.clientX-down.x)**2+(e.clientY-down.y)**2>30) drag=true; });
cv.addEventListener('mouseup',e=>{ if(!drag) pick(e); down=null; });   // releasing after a drag doesn't count as a click
function pick(e){
  const r=cv.getBoundingClientRect();
  mouse.set(((e.clientX-r.left)/r.width)*2-1, -((e.clientY-r.top)/r.height)*2+1);
  ray.setFromCamera(mouse,camera);
  let obj=ray.intersectObjects(targets,true)[0]?.object;
  while(obj && !obj.userData.info) obj=obj.parent;     // hit a child mesh: climb up to find the business data
  focused=obj||null; card.classList.toggle('show',!!obj);
}
// Every frame: while focused, camera.position.lerp(orbit position,0.06) + lookAt(focused.position)
// CSS: .card{opacity:0;transform:translateX(20px);transition:.4s;pointer-events:none}
```

Parameters: drag-detection threshold 25–50 (squared displacement); camera approach .05–.08 or a 1.2–1.8s cubic ease-out flight; orbit distance = target radius × 3–5 + constant; bind ESC to close as well.
Pitfalls: without drag/click discrimination, releasing a rotation immediately mis-selects; give small targets an oversized invisible collision body with visible:false; clicking empty space must clear focused or the camera locks up; disable autoRotate while focused and restore it after closing the card.

### Cursor glow blobs / themed custom cursor
Two offset blurred light blobs trail the mouse with lag (screen-blend glow), or cursor:none swapped for a themed critter/particle-swarm spring-following cursor. **Free tier only** (dark tech / themed narratives).

```css
.bloom{position:absolute;width:520px;height:520px;border-radius:50%;pointer-events:none;
  background:radial-gradient(circle,rgba(0,229,255,.18) 0%,transparent 60%);
  filter:blur(30px);mix-blend-mode:screen;will-change:transform;
  transition:transform .35s cubic-bezier(.2,.8,.3,1)}   /* the transition itself is the trail */
```
```js
document.addEventListener('mousemove',e=>{
  const r=deck.getBoundingClientRect(), sc=r.width/1280;   // must convert back into deck coordinates
  const x=(e.clientX-r.left)/sc, y=(e.clientY-r.top)/sc;
  bloom1.style.transform=`translate(${x-260}px,${y-260}px)`;
  bloom2.style.transform=`translate(${x-210}px,${y-290}px)`;  // the second blob offset creates volume
});
// Themed cursor variant: spring following v=v*0.82+(target-pos)*0.06, heading atan2(vy,vx), trail 8–12 frames
```

Parameters: blobs 400–600px, blur 24–36px, center alpha .12–.20; trail transition .3–.4s; spring damping .8–.86, factor .05–.08.
Pitfalls: with the deck transform:scale'd, coordinates must be divided by the live scale factor or the blobs don't track the hand; screen blending + blur — missing either turns them into dead color blocks; after cursor:none every clickable element needs hover feedback as compensation.

### Control triple feedback (value flash + graphic rebuild + particle burst)
When a slider/button/click changes a parameter, three layers of instant feedback: the value recomputes live, the number pulses with glow, and the graphic rebuilds while spraying a wave of light dots. Turns parameter changes into a "direct hit" feel. **Demo/teaching/data-interaction pages**; the burst part is free tier only.

```js
slider.addEventListener('input', e=>{
  val.textContent = fmt(compute(+e.target.value));   // 1. formula-value linkage
  val.classList.add('flash');                        // 2. .flash{transform:scale(1.05);
  setTimeout(()=>val.classList.remove('flash'),320); //    color/text-shadow in the accent color}
  rebuild(+e.target.value);                          // 3. rebuild the graphic (dispose the old objects inside first)
  burst(24);                                         //    + a synchronized particle burst
});
// Particle pool: life -=0.018 per frame, opacity=life; on death remove + material.dispose()
// DOM-version burst: initial velocity ±10px/frame, gravity .25, life 80–120 frames, alpha decay over the last 30 frames
```

Parameters: burst 16–32 particles, dying out in about 0.7–1.1s; flash 250–400ms matching the CSS transition; the glow sprite texture must be cached and reused.
Pitfalls: Three.js particles must dispose on death against GPU leaks; the deck's global click-to-page listener must exclude the control zone; continuous dragging is backstopped by the particle-pool cap.

### Mouse-driven text chromatic aberration (glitch easter egg)
Set a pair of opposing red/blue text-shadows on the whole deck based on cursor offset from center, forming a sub-pixel RGB split that follows the mouse. **Free tier only** (CRT/cyber/glitch styles); one line of inline style covers the whole show.

```js
addEventListener('mousemove', e=>{
  const r=deck.getBoundingClientRect();
  const dx=(e.clientX-r.left-r.width/2)/r.width, dy=(e.clientY-r.top-r.height/2)/r.height;
  deck.style.textShadow=
    `${dx*.6}px ${dy*.6}px 0 rgba(255,80,80,.35), `+
    `${-dx*.6}px ${-dy*.6}px 0 rgba(80,180,255,.35)`;   // the two must point in opposite directions
});
```

Parameters: split factor 0.3–1px (any larger and body text smears); red/blue pair alpha .3–.4.
Pitfalls: it works through text-shadow inheritance — titles with their own text-shadow override it, which happens to create a natural layering of "body text lightly split, titles keeping their glow"; don't implement it with filter:drop-shadow (a full-layer repaint, an order of magnitude more expensive).
