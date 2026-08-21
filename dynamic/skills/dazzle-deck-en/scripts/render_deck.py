#!/usr/bin/env python3
"""dazzle-deck render script: renders a single-HTML deck into per-page PNGs (for the agent's visual self-check).

Usage (at the repo root, in the fancy-sft environment):
    python skills/dazzle-deck/scripts/render_deck.py <deck.html> <out_dir> --page N   # render page N (1-based)
    python skills/dazzle-deck/scripts/render_deck.py <deck.html> <out_dir> --all      # render all pages + contact sheet

Behavior contract (the agent loop / accept depend on it; do not change casually):
- stdout prints one generated PNG path per line, and **the last line is guaranteed to be a PNG path** (with --all the last line is contact_sheet.png).
- Warnings (console errors / static pages / fallback paging) are printed before the PNG paths, prefixed [console]/[static]/[nav].
- Render metadata is written to <out_dir>/render.json (console_errors / static_pages / blank_pages / nav / page count).
- On failure (page won't open / not a single page captured): stderr + non-zero exit code.

Page navigation:
- --page N prefers the `file://deck.html?slide=N` direct jump (required by the dazzle-deck architecture contract),
  validating the landing with the "active page signal" after the jump; on failure it falls back to paging from
  page 1 via ArrowRight and warns.
- --all pages with real key presses (ArrowRight/Space/PageDown + active-page signal + perceptual screenshot hash
  backstop + loop detection), which simultaneously verifies the deck's keyboard navigation truly works.

Motion detection (--motion-check, on by default): samples three frames per page —
- early (~0.5s after arriving on the page, entrance animation in progress)
- final (~2.6s, entrance animation has settled per the ≤2s contract; used as the official screenshot)
- live  (~0.6s after final, probing for persistent animation)
entrance_diff = diff(early, final), live_diff = diff(final, live). Both ≈0 → the page is recorded in
static_pages (a signal only, not an error: a static frame in a formal scenario can be a conscious design,
to be judged by the agent/judge in context).

Ported from visual_qc/render_shots.py (active-page signal JS / paging convergence / blank detection / browser
library injection); this script must stay self-contained (the skill can be copied wholesale), so if you fix a
bug please sync both sides.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import warnings
from io import BytesIO
from pathlib import Path

warnings.filterwarnings("ignore", category=DeprecationWarning)  # PIL getdata noise

# ── Canvas dimensions / timing constants ─────────────────────────────────────
DECK_W, DECK_H = 1280, 720
DEVICE_SCALE = 1               # under CPU-only swiftshader, 2x doubles rasterization time on heavy 3D pages and risks timeouts
MAX_PAGES = 40                 # per-page upper bound (guards against abnormal decks looping forever)
SETTLE_MS = 1500               # first-screen settling (Three.js / particles / fonts)
NAV_SETTLE_MS = 900            # settling after a page turn (wait before the phash-mode "did it turn" check)
NAV_CHECK_MS = 200             # signal-mode page-turn confirmation wait (class switches synchronously; no need to
                               # wait for the transition to finish — confirming early lets the early frame catch fast entrances)
EARLY_MS = 500                 # motion-check: early frame sampling point (entrance animation in progress)
FINAL_EXTRA_MS = 2100          # wait this long after early before the final frame (~2.6s total; entrance settled;
                               # 3D scenes also need ~2.5s for camera/layout to settle — do not reduce)
LIVE_EXTRA_MS = 600            # wait this long after final before the live frame (probes persistent animation)
GOTO_TIMEOUT_MS = 30000
NETWORKIDLE_TIMEOUT_MS = 8000
SCREENSHOT_TIMEOUT_MS = 60000

# Perceptual difference threshold: mean absolute difference of 16x16 grayscale blocks (page-turn detection,
# calibrated value carried over from render_shots)
PHASH_SAME_THRESH = 6.0        # a difference < this value counts as "did not turn"
BLANK_STD_THRESH = 3.0         # grayscale standard deviation < this value counts as a blank/near-solid page
MAX_CONSOLE_ERRORS = 20        # maximum recorded console errors (after dedup)

# Motion detection (64x64 grid): with lossless screenshots, static content is pixel-identical; any local
# difference beyond noise is motion. A 16x16 mean is too blunt for small particles/scanlines (diluted by
# averaging), so we count "cells that changed" instead.
MOTION_GRID = 64               # downsampling grid side length
MOTION_CELL_EPS = 4            # a cell grayscale difference >= this counts as a "changed cell" (tolerates anti-aliasing wobble)
MOTION_MIN_CELLS = 5           # changed cell count < this (/4096) counts as no motion


def ensure_browser_libs() -> None:
    """Add $CONDA_PREFIX/lib and ~/pwdeps/lib to LD_LIBRARY_PATH so chromium finds its system libraries.

    conda-forge provides most libraries such as nss/nspr/atk/X11; libgbm/libcups exist in neither the system
    nor conda, so they live in ~/pwdeps/lib (binaries copied from a working machine).
    """
    candidates = []
    prefix = os.environ.get("CONDA_PREFIX")
    if prefix:
        candidates.append(str(Path(prefix) / "lib"))
    pwdeps = Path.home() / "pwdeps" / "lib"
    if pwdeps.is_dir():
        candidates.append(str(pwdeps))
    cur = os.environ.get("LD_LIBRARY_PATH", "")
    parts = [p for p in cur.split(":") if p]
    for libdir in candidates:
        if libdir not in parts:
            parts.insert(0, libdir)
    if parts:
        os.environ["LD_LIBRARY_PATH"] = ":".join(parts)
    # chromium's statically linked fontconfig reads /etc/fonts/fonts.conf by default; this pod has no /etc/fonts,
    # and without FONTCONFIG_FILE the whole font system fails (even webfont text won't draw; pages show only graphics).
    if not Path("/etc/fonts/fonts.conf").exists() and "FONTCONFIG_FILE" not in os.environ:
        if prefix and (Path(prefix) / "etc/fonts/fonts.conf").exists():
            os.environ["FONTCONFIG_FILE"] = str(Path(prefix) / "etc/fonts/fonts.conf")


# Reads the "current active page" signal: returns "active index:total pages" (e.g. "2:6"); "-1:N" when undetectable.
# Priority: explicit active/current class → the most visible slide element (highest opacity and filling the viewport).
_ACTIVE_SIGNAL_JS = r"""
() => {
  // Only recognize the deck contract's slide markers: class="slide" (exact token, not matching .slide-xxx) or data-slide.
  // Do not use broad matches like [class*="page"]/section — they would miscount content class names such as
  // .page-pad as slides and trigger false [nav] warnings.
  const sel = '.slide,[data-slide]';
  let els = [...document.querySelectorAll(sel)];
  els = els.filter(e => {
    const r = e.getBoundingClientRect();
    return r.width >= window.innerWidth * 0.5 && r.height >= window.innerHeight * 0.5;
  });
  const total = els.length;
  if (total === 0) return "-1:0";
  for (let i = 0; i < total; i++) {
    const c = (els[i].className && els[i].className.baseVal !== undefined)
      ? els[i].className.baseVal : (els[i].className || '');
    if (/(^|[\s_-])(active|current|is-active|selected|on|visible|show)([\s_-]|$)/i.test(c))
      return i + ":" + total;
  }
  let best = -1, bo = -1;
  els.forEach((e, i) => {
    const s = getComputedStyle(e);
    if (s.display === 'none' || s.visibility === 'hidden') return;
    const o = parseFloat(s.opacity || '1');
    if (o > bo) { bo = o; best = i; }
  });
  return best + ":" + total;
}
"""


def _img_stats(png_bytes: bytes) -> tuple[float, tuple]:
    """Returns (grayscale standard deviation, 16x16 thumbnail grayscale blocks) — for blank detection + perceptual diff."""
    from PIL import Image

    im = Image.open(BytesIO(png_bytes)).convert("L").resize((16, 16))
    px = list(im.getdata())
    n = len(px)
    mean = sum(px) / n
    var = sum((p - mean) ** 2 for p in px) / n
    return var ** 0.5, tuple(px)


def _block_diff(a: tuple, b: tuple) -> float:
    """Mean absolute difference between two 16x16 grayscale blocks (0-255)."""
    if not a or not b or len(a) != len(b):
        return 999.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def _motion_blocks(png_bytes: bytes) -> tuple:
    """64x64 grayscale grid (dedicated to motion detection, much finer than the 16x16 phash)."""
    from PIL import Image

    im = Image.open(BytesIO(png_bytes)).convert("L").resize((MOTION_GRID, MOTION_GRID))
    return tuple(im.getdata())


def _moving_cells(a: tuple, b: tuple) -> int:
    """Number of cells whose grayscale difference between two frames is >= MOTION_CELL_EPS (0..MOTION_GRID^2)."""
    if not a or not b or len(a) != len(b):
        return -1
    return sum(1 for x, y in zip(a, b) if abs(x - y) >= MOTION_CELL_EPS)


def _parse_sig(sig: str) -> tuple[int, int]:
    try:
        i, t = sig.split(":")
        return int(i), int(t)
    except Exception:
        return -1, 0


class DeckRenderer:
    """One CLI invocation: launch an independent chromium → render → close."""

    def __init__(self, html_path: Path, out_dir: Path, motion_check: bool = True) -> None:
        self.html_path = html_path
        self.out_dir = out_dir
        self.motion_check = motion_check
        self.console_errors: list[str] = []
        self.rendered_this_run: set[int] = set()
        self.meta: dict = {
            "deck": str(html_path),
            "w": DECK_W, "h": DECK_H,
            "mode": "", "nav": "",
            "n_pages": 0,
            "pages": [],            # [{page, png, blank, entrance_diff, live_diff, static}]
            "console_errors": [],
            "static_pages": [],
            "blank_pages": [],
            "notes": "",
        }
        self._pw = None
        self._browser = None
        self._page = None

    # ── Browser lifecycle ────────────────────────────────────────────
    def start(self) -> None:
        ensure_browser_libs()
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                  "--use-gl=swiftshader", "--enable-unsafe-swiftshader"],
        )
        ctx = self._browser.new_context(
            viewport={"width": DECK_W, "height": DECK_H},
            device_scale_factor=DEVICE_SCALE,
            ignore_https_errors=True,
            bypass_csp=True,
        )
        self._page = ctx.new_page()
        self._page.set_default_timeout(GOTO_TIMEOUT_MS)
        self._page.on("console", self._on_console)
        self._page.on("pageerror", self._on_pageerror)

    def close(self) -> None:
        for closer in (lambda: self._browser.close(), lambda: self._pw.stop()):
            try:
                closer()
            except Exception:
                pass

    def _on_console(self, msg) -> None:
        try:
            if msg.type == "error":
                self._record_error(f"console: {msg.text[:300]}")
        except Exception:
            pass

    def _on_pageerror(self, exc) -> None:
        self._record_error(f"pageerror: {str(exc)[:300]}")

    def _record_error(self, text: str) -> None:
        if text not in self.console_errors and len(self.console_errors) < MAX_CONSOLE_ERRORS:
            self.console_errors.append(text)

    # ── Loading and navigation ───────────────────────────────────────
    def _load(self, query: str = "") -> None:
        """Load the deck without a long settle (sampling timing is controlled by _sample_page; otherwise the early frame misses entrance animations)."""
        url = self.html_path.resolve().as_uri() + query
        self._page.goto(url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
        try:
            self._page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_TIMEOUT_MS)
        except Exception:
            pass
        self._page.set_default_timeout(SCREENSHOT_TIMEOUT_MS)
        # Many decks' fitDeck()/camera only re-layout on resize; fire one right after load to nudge them into place
        try:
            self._page.evaluate("() => window.dispatchEvent(new Event('resize'))")
        except Exception:
            pass

    def _grab(self) -> tuple[bytes, str, float, tuple]:
        png = self._page.screenshot(clip={"x": 0, "y": 0, "width": DECK_W, "height": DECK_H})
        sig = self._page.evaluate(_ACTIVE_SIGNAL_JS)
        std, blocks = _img_stats(png)
        return png, sig, std, blocks

    def _signal(self) -> tuple[int, int]:
        try:
            return _parse_sig(self._page.evaluate(_ACTIVE_SIGNAL_JS))
        except Exception:
            return -1, 0

    # ── Three-frame sampling (motion check) ──────────────────────────
    def _sample_page(self, already_waited_ms: int = 0) -> dict:
        """Sample the "just-arrived" current page. Returns {png, std, blocks, entrance_diff, live_diff}.

        already_waited_ms: time already waited before the call (goto's settle / the page-turn NAV_SETTLE).
        Without motion-check, only top up to the final moment and take one frame.
        """
        if not self.motion_check:
            remain = max(0, EARLY_MS + FINAL_EXTRA_MS - already_waited_ms)
            self._page.wait_for_timeout(remain)
            png, _sig, std, blocks = self._grab()
            return {"png": png, "std": std, "blocks": blocks,
                    "entrance_cells": None, "live_cells": None}

        # early frame (as close to 0.5s after arrival as possible; if we've already waited longer, sample immediately)
        self._page.wait_for_timeout(max(0, EARLY_MS - already_waited_ms))
        early_png = self._page.screenshot(clip={"x": 0, "y": 0, "width": DECK_W, "height": DECK_H})
        # final frame (the official screenshot)
        self._page.wait_for_timeout(FINAL_EXTRA_MS)
        png, _sig, std, blocks = self._grab()
        # live frame (probes persistent animation)
        self._page.wait_for_timeout(LIVE_EXTRA_MS)
        live_png = self._page.screenshot(clip={"x": 0, "y": 0, "width": DECK_W, "height": DECK_H})
        final_m = _motion_blocks(png)
        return {
            "png": png, "std": std, "blocks": blocks,
            "entrance_cells": _moving_cells(_motion_blocks(early_png), final_m),
            "live_cells": _moving_cells(final_m, _motion_blocks(live_png)),
        }

    # ── Single-page rendering (--page N) ─────────────────────────────
    def render_page(self, n: int) -> list[Path]:
        self.meta["mode"] = f"page:{n}"
        nav = "jump"
        self._load(f"?slide={n}")
        self._page.wait_for_timeout(250)  # give the navigation controller a moment to initialize/read the URL parameter
        idx, total = self._signal()
        waited = 250
        if not (idx == n - 1 and total >= n):
            # Direct jump didn't take → fallback: return to page 1 and press keys over
            nav = "keys-fallback"
            print(f"[nav] deck does not implement the ?slide=N direct-jump contract (signal idx={idx} total={total}, expected {n - 1}); "
                  f"fell back to key-press paging. Please support the URL parameter slide in the navigation controller per the dazzle-deck contract.",
                  file=sys.stderr)
            self._load()
            self._page.wait_for_timeout(SETTLE_MS)  # let the first screen settle before starting to page
            for _ in range(n - 1):
                self._page.keyboard.press("ArrowRight")
                self._page.wait_for_timeout(NAV_SETTLE_MS)
            idx2, _total2 = self._signal()
            if idx2 >= 0 and idx2 != n - 1:
                print(f"[nav] after fallback paging the signal is idx={idx2}, which does not match the target page {n - 1}; the screenshot may not be page {n}.",
                      file=sys.stderr)
            waited = NAV_SETTLE_MS if n > 1 else SETTLE_MS
        self.meta["nav"] = nav

        s = self._sample_page(already_waited_ms=waited)
        out = self.out_dir / f"page_{n:02d}.png"
        out.write_bytes(s["png"])
        self._add_page_meta(n, out, s)
        self.meta["n_pages"] = max(self.meta["n_pages"], n)
        return [out]

    # ── All-pages rendering (--all) ──────────────────────────────────
    def render_all(self) -> list[Path]:
        self.meta["mode"] = "all"
        self._load()
        # First-page sampling (the early frame starts timing from load, catching the first page's entrance animation)
        samples: list[dict] = [self._sample_page(already_waited_ms=0)]
        idx0, total0 = self._signal()
        use_signal = idx0 >= 0 and total0 > 1
        self.meta["nav"] = "keys+signal" if use_signal else "keys+phash"
        # Convergence relies on "loop detection (turning back to a seen page) + can't-turn detection"; total is not
        # a hard bound; MAX_PAGES is only the backstop
        seen_idx: set[int] = {idx0} if use_signal else set()
        seen_blocks: list[tuple] = [samples[0]["blocks"]]
        prev_idx = idx0
        prev_blocks = samples[0]["blocks"]

        while len(samples) < MAX_PAGES:
            advanced = False
            cur_sig = ""
            waited = 0
            for key in ("ArrowRight", "Space", "PageDown"):
                self._page.keyboard.press(key)
                if use_signal:
                    # class switches synchronously in keydown; confirming early → the early frame catches fast entrances
                    self._page.wait_for_timeout(NAV_CHECK_MS)
                    waited = NAV_CHECK_MS
                    cur_sig = self._page.evaluate(_ACTIVE_SIGNAL_JS)
                    ci = _parse_sig(cur_sig)[0]
                    if ci >= 0 and ci != prev_idx:
                        advanced = True
                        break
                else:
                    self._page.wait_for_timeout(NAV_SETTLE_MS)
                    waited = NAV_SETTLE_MS
                    cur_sig = self._page.evaluate(_ACTIVE_SIGNAL_JS)
                    quick = self._page.screenshot(
                        clip={"x": 0, "y": 0, "width": DECK_W, "height": DECK_H})
                    _, qb = _img_stats(quick)
                    if _block_diff(prev_blocks, qb) >= PHASH_SAME_THRESH:
                        advanced = True
                        break
            if not advanced:
                break  # none of the three keys turns the page → last page
            cur_idx = _parse_sig(cur_sig)[0]
            s = self._sample_page(already_waited_ms=waited)
            if use_signal:
                if cur_idx in seen_idx:
                    break  # turned back to a seen page → looping deck, stop
                seen_idx.add(cur_idx)
                prev_idx = cur_idx
            else:
                if any(_block_diff(b, s["blocks"]) < PHASH_SAME_THRESH for b in seen_blocks):
                    break
                seen_blocks.append(s["blocks"])
            samples.append(s)
            prev_blocks = s["blocks"]

        # Write to disk
        paths: list[Path] = []
        for i, s in enumerate(samples, start=1):
            out = self.out_dir / f"page_{i:02d}.png"
            out.write_bytes(s["png"])
            self._add_page_meta(i, out, s)
            paths.append(out)
        self.meta["n_pages"] = len(paths)
        if total0 > 1 and total0 != len(paths):
            self._note(f"sig_total={total0} != captured={len(paths)}")
        if len(paths) >= MAX_PAGES:
            self._note("hit_max_pages(nav_nonconverge)")
        # contact sheet (a montage for a one-glance whole-deck scan)
        sheet = self._contact_sheet(paths)
        if sheet:
            paths.append(sheet)
        return paths

    def _add_page_meta(self, n: int, png_path: Path, s: dict) -> None:
        self.rendered_this_run.add(n)
        blank = s["std"] < BLANK_STD_THRESH
        static = (
            s["entrance_cells"] is not None
            and 0 <= s["entrance_cells"] < MOTION_MIN_CELLS
            and 0 <= s["live_cells"] < MOTION_MIN_CELLS
        )
        self.meta["pages"].append({
            "page": n, "png": str(png_path), "blank": blank,
            "entrance_cells": s["entrance_cells"], "live_cells": s["live_cells"],
            "static": static,
        })
        if blank:
            self.meta["blank_pages"].append(n)
        if static:
            self.meta["static_pages"].append(n)

    def _contact_sheet(self, paths: list[Path]) -> Path | None:
        try:
            from PIL import Image, ImageDraw

            thumb_w, thumb_h = 320, 180
            cols = min(4, max(1, len(paths)))
            rows = math.ceil(len(paths) / cols)
            sheet = Image.new("RGB", (cols * thumb_w, rows * thumb_h), (24, 24, 24))
            draw = ImageDraw.Draw(sheet)
            for i, p in enumerate(paths):
                im = Image.open(p).convert("RGB").resize((thumb_w, thumb_h))
                x, y = (i % cols) * thumb_w, (i // cols) * thumb_h
                sheet.paste(im, (x, y))
                draw.rectangle([x, y, x + 34, y + 18], fill=(0, 0, 0))
                draw.text((x + 6, y + 3), f"{i + 1:02d}", fill=(255, 255, 255))
            out = self.out_dir / "contact_sheet.png"
            sheet.save(out)
            return out
        except Exception as e:  # noqa: BLE001
            self._note(f"contact_sheet failed: {type(e).__name__}")
            return None

    def _note(self, text: str) -> None:
        self.meta["notes"] = (self.meta["notes"] + "; " if self.meta["notes"] else "") + text

    # ── Finalization ─────────────────────────────────────────────────
    def finalize(self) -> None:
        self.meta["console_errors"] = self.console_errors
        # --page mode merges into an existing render.json (keeping other pages' records); --all overwrites entirely
        manifest = self.out_dir / "render.json"
        if self.meta["mode"].startswith("page:") and manifest.exists():
            try:
                old = json.loads(manifest.read_text(encoding="utf-8"))
                kept = [p for p in old.get("pages", [])
                        if p["page"] not in {q["page"] for q in self.meta["pages"]}]
                self.meta["pages"] = sorted(kept + self.meta["pages"], key=lambda p: p["page"])
                self.meta["n_pages"] = max(self.meta["n_pages"], old.get("n_pages", 0))
                old_errs = [e for e in old.get("console_errors", []) if e not in self.console_errors]
                self.meta["console_errors"] = (old_errs + self.console_errors)[:MAX_CONSOLE_ERRORS]
                self.meta["blank_pages"] = sorted({p["page"] for p in self.meta["pages"] if p["blank"]})
                self.meta["static_pages"] = sorted(
                    {p["page"] for p in self.meta["pages"] if p.get("static")})
            except Exception:
                pass
        manifest.write_text(json.dumps(self.meta, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    global MOTION_MIN_CELLS
    ap = argparse.ArgumentParser(description="Render a single-HTML deck into per-page PNGs")
    ap.add_argument("deck", help="path to deck.html")
    ap.add_argument("out_dir", help="screenshot output directory (render.json is also written here)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--page", type=int, help="render page N (1-based), preferring the ?slide=N direct jump")
    g.add_argument("--all", action="store_true", help="render all pages via key-press paging + contact sheet")
    ap.add_argument("--no-motion-check", action="store_true", help="disable three-frame motion sampling (faster)")
    ap.add_argument("--motion-min-cells", type=int, default=MOTION_MIN_CELLS,
                    help=f"changed cell count < this value counts as no motion (default {MOTION_MIN_CELLS}/4096)")
    args = ap.parse_args()
    MOTION_MIN_CELLS = args.motion_min_cells

    html_path = Path(args.deck)
    if not html_path.exists():
        print(f"deck does not exist: {html_path}", file=sys.stderr)
        return 2
    if args.page is not None and args.page < 1:
        print(f"--page must be >= 1, got {args.page}", file=sys.stderr)
        return 2
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    r = DeckRenderer(html_path, out_dir, motion_check=not args.no_motion_check)
    try:
        r.start()
        paths = r.render_page(args.page) if args.page is not None else r.render_all()
    except Exception as e:  # noqa: BLE001
        print(f"render failed: {type(e).__name__}: {str(e)[:300]}", file=sys.stderr)
        r.finalize()
        return 1
    finally:
        r.close()
    r.finalize()

    if not paths:
        print("not a single page captured (deck won't open or has no content)", file=sys.stderr)
        return 1

    # Warnings printed first, PNG paths after (the last line is guaranteed to be a path)
    for err in r.console_errors:
        print(f"[console] {err}")
    for p in r.meta["pages"]:
        if p["page"] not in r.rendered_this_run:
            continue  # in --page mode render.json merged historical pages; warn only about pages rendered this run
        if p.get("static"):
            print(f"[static] page {p['page']}: entrance_cells={p['entrance_cells']} "
                  f"live_cells={p['live_cells']} — no visible motion on this page (ignore if the static frame is intentional design)")
        if p.get("blank"):
            print(f"[blank] page {p['page']}: near-solid/blank page")
    for p in paths:
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
