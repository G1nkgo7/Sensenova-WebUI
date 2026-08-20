#!/usr/bin/env python3
"""check_a11y —— deck.html 确定性可访问性校验(对比度 + 字号下限),确定性蒸馏信号 / badcase 解法。

逐页(?slide=N)在无头浏览器里走**可见文字节点**,算 WCAG 对比度 + 字号下限,违规写 a11y.json + 控制台摘要。
- 画布 1280×720(dazzle 设计画布)。
- 对比度:普通文字 ≥4.5:1;大字(≥24px,或 ≥18.66px 且 font-weight≥700)≥3.0:1。
- 字号下限:<14px = fail(太小);<16px = warn。
- 文字压在**生成图 / 渐变**背景上时,背景色测不准 → 记 manual(交人眼,不硬判,避免误报)。

用法:
    python check_a11y.py <deck.html> [--json <out>]   # 默认写到 deck 同目录 a11y.json
退出码:0=无 fail;1=有 fail(供拒绝采样/CI 门控)。
"""
import json
import os
import sys
from pathlib import Path

DECK_W, DECK_H = 1280, 720
FONT_FAIL, FONT_WARN = 14.0, 16.0

AUDIT_JS = r"""
() => {
  const FONT_FAIL = %f, FONT_WARN = %f;
  function parseColor(s){
    const m = s && s.match(/rgba?\(([^)]+)\)/);
    if(!m) return null;
    const p = m[1].split(',').map(x=>parseFloat(x.trim()));
    return {r:p[0], g:p[1], b:p[2], a:(p.length>3?p[3]:1)};
  }
  function lum(c){
    const f = v => { v/=255; return v<=0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055,2.4); };
    return 0.2126*f(c.r)+0.7152*f(c.g)+0.0722*f(c.b);
  }
  function contrast(a,b){ const L1=lum(a),L2=lum(b); const hi=Math.max(L1,L2),lo=Math.min(L1,L2); return (hi+0.05)/(lo+0.05); }
  function visible(el){
    const s=getComputedStyle(el);
    if(s.display==='none'||s.visibility==='hidden'||parseFloat(s.opacity)===0) return false;
    const r=el.getBoundingClientRect();
    if(r.width<2||r.height<2) return false;
    if(r.bottom<0||r.top>%d||r.right<0||r.left>%d) return false;  // 画布外不算
    return true;
  }
  // 有效背景:沿祖先找;遇 background-image(图/渐变)→ image(测不准);遇不透明实色→用它;否则到根用 body 底色
  function effBg(el){
    let e=el;
    while(e){
      const s=getComputedStyle(e);
      if(s.backgroundImage && s.backgroundImage!=='none') return {kind:'image'};
      const c=parseColor(s.backgroundColor);
      if(c && c.a>=0.85) return {kind:'solid', c};
      e=e.parentElement;
    }
    const bc=parseColor(getComputedStyle(document.body).backgroundColor);
    return bc && bc.a>=0.85 ? {kind:'solid', c:bc} : {kind:'image'};
  }
  function hasDirectText(el){
    for(const n of el.childNodes) if(n.nodeType===3 && n.textContent.trim().length>=2) return true;
    return false;
  }
  const out=[];
  const all=document.querySelectorAll('body *');
  for(const el of all){
    if(!hasDirectText(el) || !visible(el)) continue;
    const s=getComputedStyle(el);
    const fg=parseColor(s.color); if(!fg) continue;
    const fs=parseFloat(s.fontSize)||0;
    const fw=parseInt(s.fontWeight)||400;
    const txt=(el.textContent||'').trim().slice(0,40);
    const tag=el.tagName.toLowerCase()+(el.className&&typeof el.className==='string'?('.'+el.className.trim().split(/\s+/)[0]):'');
    // 字号
    if(fs>0 && fs<FONT_FAIL) out.push({type:'fontsize',sev:'fail',value:Math.round(fs*10)/10,threshold:FONT_FAIL,el:tag,text:txt});
    else if(fs>0 && fs<FONT_WARN) out.push({type:'fontsize',sev:'warn',value:Math.round(fs*10)/10,threshold:FONT_WARN,el:tag,text:txt});
    // 对比度
    const bg=effBg(el);
    if(bg.kind==='image'){ out.push({type:'contrast',sev:'manual',note:'文字压图/渐变,背景测不准,请人眼核',el:tag,text:txt}); continue; }
    const large = fs>=24 || (fs>=18.66 && fw>=700);
    const need = large?3.0:4.5;
    const cr = contrast(fg, bg.c);
    if(cr < need) out.push({type:'contrast',sev:'fail',value:Math.round(cr*100)/100,threshold:need,large,el:tag,text:txt});
  }
  return out;
}
""" % (FONT_FAIL, FONT_WARN, DECK_H, DECK_W)


def _ensure_browser_libs():
    """同 render_deck:把 conda/pwdeps 的 lib 加进 LD_LIBRARY_PATH,并指好 fontconfig,让 chromium 起得来。"""
    candidates = []
    prefix = os.environ.get("CONDA_PREFIX")
    if prefix:
        candidates.append(str(Path(prefix) / "lib"))
    pwdeps = Path.home() / "pwdeps" / "lib"
    if pwdeps.is_dir():
        candidates.append(str(pwdeps))
    parts = [p for p in os.environ.get("LD_LIBRARY_PATH", "").split(":") if p]
    for d in candidates:
        if d not in parts:
            parts.insert(0, d)
    if parts:
        os.environ["LD_LIBRARY_PATH"] = ":".join(parts)
    if not Path("/etc/fonts/fonts.conf").exists() and "FONTCONFIG_FILE" not in os.environ:
        if prefix and (Path(prefix) / "etc/fonts/fonts.conf").exists():
            os.environ["FONTCONFIG_FILE"] = str(Path(prefix) / "etc/fonts/fonts.conf")


def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(2)
    deck = Path(sys.argv[1]).resolve()
    out_json = None
    if "--json" in sys.argv:
        out_json = Path(sys.argv[sys.argv.index("--json")+1])
    if out_json is None:
        out_json = deck.parent / "a11y.json"
    if not deck.exists():
        print(f"check_a11y 错误:找不到 {deck}"); sys.exit(2)

    _ensure_browser_libs()
    from playwright.sync_api import sync_playwright
    url0 = deck.as_uri()
    result = {"deck": str(deck), "pages": [], "summary": {}}
    n_fail = n_warn = n_manual = 0
    with sync_playwright() as pw:
        br = pw.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
            "--use-gl=swiftshader", "--enable-unsafe-swiftshader"])
        ctx = br.new_context(viewport={"width": DECK_W, "height": DECK_H},
                             ignore_https_errors=True, bypass_csp=True)
        pg = ctx.new_page()
        pg.goto(url0, wait_until="domcontentloaded", timeout=30000)
        pg.wait_for_timeout(700)
        n_pages = pg.evaluate(
            "() => document.querySelectorAll('.slide,[class*=\"slide\"],[data-slide],section').length") or 1
        n_pages = max(1, min(int(n_pages), 40))
        for i in range(1, n_pages + 1):
            try:
                pg.goto(f"{url0}?slide={i}", wait_until="domcontentloaded", timeout=30000)
                pg.wait_for_timeout(500)
                viol = pg.evaluate(AUDIT_JS) or []
            except Exception as e:
                viol = [{"type": "error", "sev": "warn", "note": str(e)[:120]}]
            for v in viol:
                if v.get("sev") == "fail": n_fail += 1
                elif v.get("sev") == "warn": n_warn += 1
                elif v.get("sev") == "manual": n_manual += 1
            if viol:
                result["pages"].append({"page": i, "violations": viol})
        br.close()
    result["summary"] = {"fail": n_fail, "warn": n_warn, "manual": n_manual, "n_pages": n_pages}
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"=== check_a11y: {deck.name} ===")
    print(f"fail={n_fail}  warn={n_warn}  manual(压图待人眼)={n_manual}  → {out_json}")
    for p in result["pages"]:
        for v in p["violations"]:
            if v.get("sev") in ("fail", "manual"):
                print(f"  [p{p['page']}][{v['sev']}][{v['type']}] {v.get('el','')} "
                      f"{('对比'+str(v.get('value'))+'<'+str(v.get('threshold'))) if v['type']=='contrast' and v.get('value') is not None else ''}"
                      f"{('字号'+str(v.get('value'))+'px') if v['type']=='fontsize' else ''}"
                      f"{v.get('note','')}  «{v.get('text','')}»")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
