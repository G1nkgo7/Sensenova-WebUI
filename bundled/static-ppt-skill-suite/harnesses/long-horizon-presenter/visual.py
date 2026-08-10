#!/usr/bin/env python3
"""visual.py —— 直接可视化 runs/ 里的生成结果(渲染好的幻灯片)。

把一个 batch(或单个 run)的所有 deck 渲染图拼成一个**自包含 HTML 画廊**,浏览器里一眼看完:
每个 deck 一张卡片(query + 状态 + 页数 + 模型),下面横排该 deck 的全部 slide 截图(点开看原图大图)。
状态/原因取自 log/<batch>.manifest.jsonl,query/模型取自 _trace/orchestrator/config.json。

用法:
    uv run python visual.py --batch smoke1                     # 生成 runs/smoke1/gallery.html
    uv run python visual.py --batch smoke1 --serve 8009        # 生成并起 http 服务(端口转发后浏览)
    uv run python visual.py --batch smoke1 --status completed   # 只看通过的(completed/rejected/error)
    uv run python visual.py --run runs/smoke1/<sid>            # 只看单个 run
    uv run python visual.py --batch smoke1 --embed             # 图片内联 base64,产出单文件可 scp 带走
"""
import argparse
import base64
import glob
import html
import json
import os
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(ROOT, "runs")
LOGS = os.path.join(ROOT, "log")

STATUS_COLOR = {"completed": "#3fb950", "rejected": "#d29922", "error": "#f85149"}


def load_manifest(batch):
    """log/<batch>.manifest.jsonl → {sid: 末条记录}(后写覆盖先写)。"""
    mpath = os.path.join(LOGS, f"{batch}.manifest.jsonl")
    out = {}
    if os.path.exists(mpath):
        with open(mpath, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("sample_id"):
                    out[r["sample_id"]] = r
    return out


def read_orch_config(run_dir):
    """取该 run 编排器的 config.json(含 query=task、model)。"""
    p = os.path.join(run_dir, "_trace", "orchestrator", "config.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def collect_deck(run_dir, manifest):
    """收集一个 run 的可视化信息:query / 状态 / 模型 / 全部渲染图路径(按页号排序)。"""
    sid = os.path.basename(run_dir.rstrip("/"))
    cfg = read_orch_config(run_dir)
    rec = manifest.get(sid, {})
    pngs = sorted(glob.glob(os.path.join(run_dir, "renders", "slide_*.png")),
                  key=lambda p: os.path.basename(p))
    return {
        "sid": sid,
        "query": (cfg.get("task") or rec.get("query") or "(无 query)").strip(),
        "model": cfg.get("model", "?"),
        "status": rec.get("status", "?"),
        "reason": rec.get("reason", ""),
        "n_slides": rec.get("n_slides", len(pngs)),
        "n_workers": rec.get("n_workers", ""),
        "pngs": pngs,
    }


def _img_src(png, out_dir, embed):
    if embed:
        with open(png, "rb") as f:
            return "data:image/png;base64," + base64.b64encode(f.read()).decode()
    return html.escape(os.path.relpath(png, out_dir))


def build_html(decks, out_html, title, embed):
    out_dir = os.path.dirname(os.path.abspath(out_html))
    ok = sum(1 for d in decks if d["status"] == "completed")
    rej = sum(1 for d in decks if d["status"] == "rejected")
    err = sum(1 for d in decks if d["status"] not in ("completed", "rejected"))
    cards = []
    for d in decks:
        color = STATUS_COLOR.get(d["status"], "#8b949e")
        pill = (f'<span class="pill" style="background:{color}22;color:{color};border-color:{color}55">'
                f'{html.escape(d["status"])}</span>')
        reason = (f'<span class="reason">{html.escape(d["reason"])}</span>'
                  if d["reason"] and d["status"] != "completed" else "")
        slides = "".join(
            f'<figure><img loading="lazy" src="{_img_src(p, out_dir, embed)}" '
            f'onclick="zoom(this.src)"><figcaption>{html.escape(os.path.basename(p))}</figcaption></figure>'
            for p in d["pngs"]) or '<div class="empty">(无渲染图)</div>'
        cards.append(f"""
    <section class="deck">
      <div class="head">
        {pill}
        <span class="sid">{html.escape(d["sid"])}</span>
        <span class="meta">{d["n_slides"]} 页 · {html.escape(str(d["model"]))}</span>
        {reason}
      </div>
      <div class="query">{html.escape(d["query"])}</div>
      <div class="strip">{slides}</div>
    </section>""")

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} · 画廊</title>
<style>
  :root{{--bg:#0d1117;--card:#161b22;--bd:#30363d;--fg:#e6edf3;--mut:#8b949e}}
  *{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--fg);
    font:14px/1.5 -apple-system,"Segoe UI","Noto Sans SC",sans-serif}}
  header{{position:sticky;top:0;z-index:5;background:#0d1117ee;backdrop-filter:blur(6px);
    border-bottom:1px solid var(--bd);padding:14px 20px;display:flex;gap:16px;align-items:baseline}}
  header h1{{font-size:16px;margin:0}} header .counts{{color:var(--mut);font-size:13px}}
  header .counts b{{color:var(--fg)}} header .gen{{margin-left:auto;color:var(--mut);font-size:12px}}
  .deck{{border:1px solid var(--bd);background:var(--card);border-radius:12px;margin:16px 20px;padding:14px 16px}}
  .head{{display:flex;gap:10px;align-items:center;flex-wrap:wrap}}
  .pill{{font-size:12px;padding:2px 9px;border-radius:999px;border:1px solid;font-weight:600}}
  .sid{{font-family:ui-monospace,monospace;color:var(--mut);font-size:12px}}
  .meta{{color:var(--mut);font-size:12px}}
  .reason{{color:#d29922;font-size:12px}}
  .query{{margin:8px 0 12px;font-size:15px;white-space:pre-wrap}}
  .strip{{display:flex;gap:12px;overflow-x:auto;padding-bottom:6px}}
  figure{{margin:0;flex:0 0 auto;width:360px}}
  figure img{{width:360px;aspect-ratio:16/9;object-fit:cover;border:1px solid var(--bd);
    border-radius:8px;cursor:zoom-in;background:#000;display:block}}
  figcaption{{color:var(--mut);font-size:11px;margin-top:4px;font-family:ui-monospace,monospace}}
  .empty{{color:var(--mut);padding:20px}}
  #lb{{position:fixed;inset:0;background:#000d;display:none;align-items:center;justify-content:center;
    z-index:50;cursor:zoom-out}} #lb img{{max-width:96vw;max-height:96vh;border-radius:8px}}
</style></head><body>
<header>
  <h1>{html.escape(title)}</h1>
  <span class="counts"><b style="color:{STATUS_COLOR['completed']}">✓{ok}</b> 通过 ·
    <b style="color:{STATUS_COLOR['rejected']}">⊘{rej}</b> 丢弃 ·
    <b style="color:{STATUS_COLOR['error']}">✗{err}</b> 其它 · 共 <b>{len(decks)}</b> deck</span>
  <span class="gen">生成于 {now}</span>
</header>
{''.join(cards)}
<div id="lb" onclick="this.style.display='none'"><img id="lbimg" src=""></div>
<script>
  function zoom(src){{document.getElementById('lbimg').src=src;document.getElementById('lb').style.display='flex'}}
  addEventListener('keydown',e=>{{if(e.key==='Escape')document.getElementById('lb').style.display='none'}});
</script>
</body></html>"""


def find_run_dirs(batch):
    bd = os.path.join(RUNS, batch)
    if not os.path.isdir(bd):
        raise SystemExit(f"找不到 batch 目录: {bd}")
    return sorted(os.path.join(bd, d) for d in os.listdir(bd)
                  if os.path.isdir(os.path.join(bd, d)) and not d.startswith("."))


def serve(directory, port):
    import functools
    import http.server
    import socketserver
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("0.0.0.0", port), handler) as httpd:
        print(f"▶ 服务已起: http://0.0.0.0:{port}/gallery.html  (Ctrl-C 退出)\n"
              f"  本地查看: ssh 端口转发 -L {port}:localhost:{port} 后浏览器开上面地址", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止。")


def main():
    ap = argparse.ArgumentParser(description="可视化 runs/ 里的生成结果(渲染图画廊)。")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--batch", help="可视化整个批次 runs/<batch>/")
    g.add_argument("--run", help="可视化单个 run 目录 runs/<batch>/<sid>")
    ap.add_argument("--status", help="只看某状态(completed/rejected/error)")
    ap.add_argument("--out", help="输出 html 路径(默认 runs/<batch>/gallery.html)")
    ap.add_argument("--embed", action="store_true", help="图片内联 base64,产出单文件(可 scp 带走,但文件大)")
    ap.add_argument("--serve", type=int, metavar="PORT", help="生成后起 http 服务在该端口")
    args = ap.parse_args()

    if args.run:
        run_dir = os.path.abspath(args.run)
        batch = os.path.basename(os.path.dirname(run_dir.rstrip("/")))
        manifest = load_manifest(batch)
        run_dirs = [run_dir]
        title = f"{batch}/{os.path.basename(run_dir)}"
        default_out = os.path.join(os.path.dirname(run_dir), "gallery.html")
    else:
        batch = args.batch
        manifest = load_manifest(batch)
        run_dirs = find_run_dirs(batch)
        title = f"batch: {batch}"
        default_out = os.path.join(RUNS, batch, "gallery.html")

    decks = [collect_deck(rd, manifest) for rd in run_dirs]
    if args.status:
        if args.status in ("completed", "rejected"):
            decks = [d for d in decks if d["status"] == args.status]
        else:                                   # error = 非 completed/rejected
            decks = [d for d in decks if d["status"] not in ("completed", "rejected")]
    # 通过的排前面,其余按 sid
    decks.sort(key=lambda d: (d["status"] != "completed", d["sid"]))

    out_html = os.path.abspath(args.out or default_out)
    os.makedirs(os.path.dirname(out_html), exist_ok=True)
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(build_html(decks, out_html, title, args.embed))
    n_slides = sum(len(d["pngs"]) for d in decks)
    print(f"✓ 画廊已生成: {out_html}\n  {len(decks)} 个 deck · {n_slides} 张渲染图"
          f"{' · 图片已内联' if args.embed else ''}", flush=True)

    if args.serve:
        serve(os.path.dirname(out_html), args.serve)


if __name__ == "__main__":
    main()
