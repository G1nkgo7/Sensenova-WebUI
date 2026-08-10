#!/usr/bin/env python3
"""推理入口 —— 批量并行 rollout，生成 演示文稿推理结果。

每一条 seed(jsonl 的一行)= 一个 sample，跑在**独立的子进程 + 独立 run 目录**里，
所以进程级全局、playwright 浏览器、cwd 永不串台:

    runs/<batch>/<sample_id>/      # 该 sample 的 ppt-skill 工作区 + 轨迹(exist_ok=False 创建,绝不覆盖)
    logs/<batch>.manifest.jsonl    # 每个 sample 一条结果,用于断点续跑

输入 jsonl 每行一个对象,字段对齐 queryGeneration/seeds/*.jsonl:
    {"query": "...", "class": "...", "material": "free", "lang": "zh",
     "slide_count": 12, "topic": "..."}
其中 `query` 是 brief,其余字段(lang / slide_count / topic …)整体传给 agent_loop。

用法:
    uv run python distill.py --seeds ../queryGeneration/seeds/金融与投资.jsonl --batch fin --workers 8
    uv run python distill.py --seeds seeds.jsonl --batch fin --resume     # 断点续跑
    uv run python distill.py --seeds seeds.jsonl --batch fin --dry-run     # 不调模型,只看进度条/流程
    uv run python distill.py --query "做一份 5 页的人工智能简介" --batch adhoc

agent_loop / tools 稍后再写。本文件只依赖一个约定好的入口:
    agent_loop.run_sample(sample_id: str, seed: dict, run_dir: str, config: dict) -> dict
返回至少含 {"status": "completed" | "rejected", ...};其它任何异常都被捕获记为 error。
"""
import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import shutil
import sys
import time
import traceback

from attachments_runtime import stage_seed_attachments

ROOT = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(ROOT, "runs")
LOGS = os.path.join(ROOT, "logs")

# 终态:断点续跑时跳过这些状态;其余(error / 缺失 / 半截)都会重跑。
TERMINAL = {"completed"}


def cgroup_cpus():
    """返回 cgroup 真实可用核数(容器配额),读不到则退回 os.cpu_count()。
    很多容器里 nproc 报的是宿主机核数,会误导并发设置——这里读真实配额。"""
    try:                                     # cgroup v2
        with open("/sys/fs/cgroup/cpu.max") as f:
            q, p = f.read().split()
        if q != "max":
            return max(1, int(int(q) / int(p)))
    except Exception:
        pass
    try:                                     # cgroup v1
        q = int(open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read())
        p = int(open("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read())
        if q > 0:
            return max(1, int(q / p))
    except Exception:
        pass
    return os.cpu_count() or 1


def load_dotenv():
    """加载 ROOT/.env 里的 KEY=VALUE 到环境变量(不覆盖已设的)。无依赖、幂等;
    main 和 worker 都调一次,保证父进程和(spawn 出来的)子进程都拿得到密钥。"""
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)


# ---------------------------------------------------------------- seeds / manifest

def load_seeds(path):
    """读 jsonl,每行一个 seed dict。容错:跳过空行,坏行报错并指出行号。"""
    seeds = []
    with open(path, encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise SystemExit(f"seed 文件第 {ln} 行不是合法 JSON: {e}")
            if not isinstance(obj, dict) or not obj.get("query"):
                raise SystemExit(f"seed 文件第 {ln} 行缺少 query 字段: {line[:120]}")
            seeds.append(obj)
    return seeds


def make_sample_id(batch, seed, seen):
    """稳定且唯一的 sample_id,**与 seed 在文件里的位置无关**(对整条 seed 做规范化哈希),
    这样过滤/重排 seed 文件后 --resume 仍能映射到同一目录,不会全量重跑。完全相同的 seed
    用出现次数消歧。"""
    canon = json.dumps(seed, sort_keys=True, ensure_ascii=False)
    h = hashlib.sha1(canon.encode("utf-8")).hexdigest()[:12]
    base = f"{batch}_{h}"
    seen[base] = seen.get(base, 0) + 1
    return base if seen[base] == 1 else f"{base}_{seen[base]}"


def load_manifest(mpath):
    """读 manifest,返回 {sample_id: last_record}(后写覆盖先写,取最终态)。每条记录注入
    `_attempts`(该 sid 出现过几次)。容错:跳过坏行 / 缺 sample_id 的行(末行可能是中断写入的半行)。"""
    done, attempts = {}, {}
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
                sid = r.get("sample_id")
                if not sid:
                    continue
                attempts[sid] = attempts.get(sid, 0) + 1
                done[sid] = r
    for sid, r in done.items():
        r["_attempts"] = attempts[sid]
    return done


def append_manifest(mpath, rec):
    with open(mpath, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()


# ---------------------------------------------------------------- worker(子进程)

def worker(task):
    """在**子进程**里跑。懒加载 agent_loop,保持进程池轻量。"""
    load_dotenv()                       # spawn 安全:子进程也加载密钥
    sid, run_dir = task["sample_id"], task["run_dir"]
    config, seed = task["config"], task["seed"]

    if config.get("dry_run"):
        try:
            return _dry_worker(sid, run_dir, seed)
        except Exception as e:
            return {"sample_id": sid, "run_dir": run_dir, "status": "error",
                    "error": f"{type(e).__name__}: {e}"}

    # 建 key 检查放在创建 run_dir 之前,缺 key 不要占坑(否则重跑会被当成 skipped_exists)。
    if (
        os.environ.get("MODEL_BACKEND", "").strip().lower() != "openai"
        and "ANTHROPIC_API_KEY" not in os.environ
    ):
        return {"sample_id": sid, "status": "error", "run_dir": run_dir,
                "error": "子进程环境里没有 ANTHROPIC_API_KEY"}
    try:
        from agent_loop import run_sample
    except Exception as e:
        return {"sample_id": sid, "status": "error", "run_dir": run_dir,
                "error": f"无法导入 agent_loop.run_sample: {type(e).__name__}: {e}"}

    try:
        os.makedirs(run_dir, exist_ok=False)   # 并行安全:拒绝覆盖已存在目录
    except FileExistsError:
        # 残留目录(上次被中断没清干净 / 孤儿进程重建)。主进程已决定本 sample 要跑,
        # 这里的目录一定是脏残留——清掉重建,让续跑稳健,而不是直接报 error。
        shutil.rmtree(run_dir, ignore_errors=True)
        try:
            os.makedirs(run_dir, exist_ok=False)
        except FileExistsError:
            return {"sample_id": sid, "status": "error", "run_dir": run_dir,
                    "error": "run_dir 反复无法创建(疑似有进程正在写它),跳过"}
    try:
        stage_seed_attachments(seed, run_dir)
        res = run_sample(sid, seed, run_dir, config)
        status = res.get("status", "completed") if isinstance(res, dict) else "completed"
        out = {"sample_id": sid, "run_dir": run_dir, "status": status}
        if isinstance(res, dict):
            out.update({k: v for k, v in res.items() if k not in out})
        return out
    except Exception as e:
        return {"sample_id": sid, "run_dir": run_dir, "status": "error",
                "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:]}


def _dry_worker(sid, run_dir, seed):
    """--dry-run:不调模型,造一个 run_dir 和假轨迹,随机/确定地分配 completed/rejected,
    让我们能验证进度条、断点续跑、隔离目录这些骨架。"""
    os.makedirs(run_dir, exist_ok=True)
    stage_seed_attachments(seed, run_dir)
    n = int(seed.get("slide_count") or 6)
    # 用 sample_id 哈希做确定性"掷骰",免得依赖随机数:大多数 completed,少数 rejected/error。
    roll = int(hashlib.sha1(sid.encode()).hexdigest(), 16) % 10
    time.sleep(0.2 + (roll % 5) * 0.1)
    with open(os.path.join(run_dir, "dry.json"), "w", encoding="utf-8") as f:
        json.dump({"query": seed.get("query"), "slides": n}, f, ensure_ascii=False)
    if roll == 0:
        raise RuntimeError("dry-run 模拟错误")
    status = "rejected" if roll == 1 else "completed"
    return {"sample_id": sid, "run_dir": run_dir, "status": status, "slides": n, "dry": True}


# ---------------------------------------------------------------- config

def build_config(args):
    return {
        "batch": args.batch,
        "dry_run": args.dry_run,
        "model": os.environ.get("MODEL", "claude-opus-4-7"),
        "anthropic_base_url": os.environ.get("ANTHROPIC_BASE_URL", "https://tokenhub.sensetime.com"),
        "openai_base_url": os.environ.get("OPENAI_BASE_URL", "https://tokenhub.sensetime.com/v1"),
        "image_model": os.environ.get("IMAGE_MODEL", "gpt-image-2"),
        "serper_base_url": os.environ.get("SERPER_BASE_URL", "https://google.serper.dev"),
        "skill_dir": os.path.join(ROOT, "skills", "ppt-skill"),
        "max_turns": int(os.environ.get("MAX_TURNS", "120")),
        "child_max_turns": int(os.environ.get("SUBAGENT_MAX_TURNS", "0")),
        "max_tokens": int(os.environ.get("MAX_TOKENS", "32000")),
    }


# ---------------------------------------------------------------- 进度条

class Progress:
    """优先用 rich 渲染好看的实时进度条;没装 rich 就退化成单行打印,功能不变。"""

    def __init__(self, total):
        self.total = total
        self.ok = self.rej = self.err = 0
        self.start = time.time()
        self._rich = None
        try:
            from rich.console import Console
            from rich.progress import (Progress as RP, SpinnerColumn, BarColumn,
                                       TextColumn, MofNCompleteColumn,
                                       TimeElapsedColumn, TimeRemainingColumn)
            self._console = Console()
            self._rich = RP(
                SpinnerColumn(),
                TextColumn("[bold cyan]推理中[/]"),
                BarColumn(bar_width=None),
                MofNCompleteColumn(),
                TextColumn("[green]✓{task.fields[ok]}[/] [yellow]⊘{task.fields[rej]}[/] [red]✗{task.fields[err]}[/]"),
                TextColumn("·"),
                TimeElapsedColumn(),
                TextColumn("剩余"),
                TimeRemainingColumn(),
                console=self._console,
            )
            self._tid = self._rich.add_task("run", total=total, ok=0, rej=0, err=0)
            self._rich.start()
        except Exception:
            self._rich = None  # 退化模式

    def update(self, rec):
        st = rec.get("status")
        if st == "completed":
            self.ok += 1
        elif st == "rejected":
            self.rej += 1
        else:
            self.err += 1
        done = self.ok + self.rej + self.err
        if self._rich:
            self._rich.update(self._tid, advance=1, ok=self.ok, rej=self.rej, err=self.err)
            tag = {"completed": "[green]✓[/]", "rejected": "[yellow]⊘[/]"}.get(st, "[red]✗[/]")
            line = f"  {tag} {rec['sample_id']}  [dim]{st}[/]"
            if rec.get("error"):
                line += f"  [red]{str(rec['error'])[:80]}[/]"
            self._console.log(line)
        else:
            extra = f"  {rec.get('error','')[:80]}" if rec.get("error") else ""
            print(f"  [{done}/{self.total}] {rec['sample_id']}: {st}{extra}", flush=True)

    def close(self):
        if self._rich:
            self._rich.stop()
        dur = time.time() - self.start
        print(f"\n完成。✓{self.ok} 通过  ⊘{self.rej} 丢弃  ✗{self.err} 失败  "
              f"用时 {dur:.0f}s", flush=True)


# ---------------------------------------------------------------- main

def main():
    load_dotenv()                       # 父进程加载密钥;fork 出的子进程会继承
    ap = argparse.ArgumentParser(description="PPT Agent 推理生成 —— 批量并行 rollout。")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--seeds", help="seed jsonl 文件(每行一个 {query, lang, slide_count, ...})")
    g.add_argument("--query", help="单条 inline brief,临时跑一条")
    ap.add_argument("--batch", required=True, help="批次名,决定 runs/<batch>/ 和 manifest 文件名")
    ap.add_argument("--workers", type=int, default=4, help="并行子进程数")
    ap.add_argument("--limit", type=int, default=0, help="只取前 N 条(0=全部)")
    ap.add_argument("--resume", action="store_true",
                    help="断点续跑:跳过已 completed 的;未完成/出错的清掉残留目录后重跑")
    ap.add_argument("--overwrite", action="store_true",
                    help="从头重来:删掉该批次已有的 runs/ 产物和 manifest 再跑(危险,会丢已完成轨迹)")
    ap.add_argument("--max-attempts", type=int, default=3,
                    help="单个 sample 最多尝试几次(含历史),跑满仍未 completed 就放弃(默认 3)")
    ap.add_argument("--dry-run", action="store_true", help="不调模型,模拟跑,验证骨架与进度条")
    args = ap.parse_args()

    # 护栏:worker 远超真实核数 → chromium 渲染会争抢/OOM 崩(TargetClosed)→ 大量拒绝。
    if not args.dry_run:
        cores = cgroup_cpus()
        if args.workers > 2 * cores:
            print(f"⚠️  警告:--workers={args.workers} 远超本容器真实核数 {cores}(cgroup 配额,非宿主机 nproc)。\n"
                  f"    每个 worker 会起 chromium 渲染,过度并发会 OOM/崩(TargetClosed)→ 大量拒绝。\n"
                  f"    建议 --workers ≈ {cores}~{2 * cores}(配 SLIDE_CONCURRENCY=2)。", flush=True)

    if args.resume and args.overwrite:
        raise SystemExit("--resume 和 --overwrite 含义相反,不能同时用(续跑 or 从头重来,二选一)。")
    if args.max_attempts < 1:
        raise SystemExit("--max-attempts 必须 >= 1。")

    if args.query:
        seeds = [{"query": args.query, "lang": "zh"}]
    else:
        seeds = load_seeds(args.seeds)
    if args.limit:
        seeds = seeds[: args.limit]

    batch_dir = os.path.join(RUNS, args.batch)
    os.makedirs(LOGS, exist_ok=True)
    mpath = os.path.join(LOGS, f"{args.batch}.manifest.jsonl")

    # 安全闸:批次已有产物(manifest 或非空的 runs 目录)又没给 --resume,默认**不动**(防误删已完成轨迹)。
    # 要续跑用 --resume;确实想从头重来用 --overwrite。
    has_runs = os.path.isdir(batch_dir) and os.listdir(batch_dir)
    if (os.path.exists(mpath) or has_runs) and not args.resume and not args.overwrite:
        raise SystemExit(f"批次 {args.batch} 已存在产物(manifest: {mpath} 或 runs/{args.batch}/)。\n"
                         f"  续跑:加 --resume   从头重来(会删旧产物):加 --overwrite   或换个 --batch")
    if args.overwrite:
        shutil.rmtree(batch_dir, ignore_errors=True)
        if os.path.exists(mpath):
            os.remove(mpath)
    os.makedirs(batch_dir, exist_ok=True)

    done = load_manifest(mpath) if args.resume else {}
    config = build_config(args)

    # 一次性列出 batch_dir 下现存目录名(单次 listdir),后面用内存集合判断残留 —— 避免对
    # 全部 14 万种子逐个 os.path.isdir(在慢 FUSE 上 13.8 万次串行 stat 会让 --resume 启动卡几十分钟)。
    existing_dirs = set(os.listdir(batch_dir)) if os.path.isdir(batch_dir) else set()

    tasks, skipped, exhausted, seen, computed_sids = [], 0, 0, {}, set()
    for seed in seeds:
        sid = make_sample_id(args.batch, seed, seen)
        computed_sids.add(sid)
        run_dir = os.path.join(batch_dir, sid)
        prev = done.get(sid, {})
        if prev.get("status") in TERMINAL:
            skipped += 1
            continue
        # 重试上限:rejected/error 反复跑也不一定能成,跑满 --max-attempts 次就放弃(避免无限重试)。
        if prev.get("_attempts", 0) >= args.max_attempts:
            exhausted += 1
            continue
        # 重跑前清掉上次的残留目录(error/半截),否则子进程 exist_ok=False 会拒绝。
        # 符合"脏数据直接丢"——残留的是失败轨迹,没有保留价值;completed 的已在上面跳过,不会被删。
        if sid in existing_dirs:
            shutil.rmtree(run_dir, ignore_errors=True)
        tasks.append({"sample_id": sid, "seed": seed, "run_dir": run_dir, "config": config})

    # 护栏:--resume 时,看 manifest 里有多少条**对不上当前任何 seed**(孤儿)。比对的是
    # manifest 自己的记录(不是"当前 seed 在不在 manifest"),所以正常中断续跑——多数 seed
    # 还没跑完、不在 manifest——不会误报;只有 seed 文件真被改过(id 内容寻址,变了就全变)、
    # manifest 大批记录变孤儿时才警告。
    orphans = set(done) - computed_sids
    if args.resume and done and len(orphans) >= 0.8 * len(done):
        print(f"⚠️  警告:--resume 但已有 manifest 的 {len(orphans)}/{len(done)} 条记录对不上当前任何 seed。\n"
              f"    seed 文件很可能在上次运行后被改过 → 这次会几乎全量重跑、旧产物变孤儿。\n"
              f"    若非本意:换个 --batch,或确认 seed 文件没动。", flush=True)

    mode = "  [dry-run]" if args.dry_run else ""
    print(f"batch={args.batch}  seeds={len(seeds)}  待跑={len(tasks)}  "
          f"已完成跳过={skipped}  达重试上限={exhausted}  workers={args.workers}{mode}", flush=True)
    if not tasks:
        print("没有要跑的。")
        return

    prog = Progress(len(tasks))
    # max_tasks_per_child=1:每个 sample 用一个全新子进程跑完即回收,杜绝跨 sample 残留
    # (僵尸线程 / playwright 句柄 / 模块级状态串台)。Python 3.11+ 支持。
    pool_kw = {"max_workers": args.workers}
    try:
        import sys as _sys
        if _sys.version_info >= (3, 11):
            pool_kw["max_tasks_per_child"] = 1
    except Exception:
        pass
    with cf.ProcessPoolExecutor(**pool_kw) as ex:
        futs = {ex.submit(worker, t): t for t in tasks}
        for fut in cf.as_completed(futs):
            t = futs[fut]
            try:
                rec = fut.result()
            except Exception as e:
                # 子进程硬崩溃(段错误 / BrokenProcessPool)也不让整批中断;
                # 记为 error,resume 会再捡起来。
                rec = {"sample_id": t["sample_id"], "run_dir": t["run_dir"],
                       "status": "error", "error": f"子进程崩溃: {type(e).__name__}: {e}"}
            rec["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            append_manifest(mpath, rec)
            prog.update(rec)
    prog.close()
    print(f"manifest: {mpath}")
    if prog.err and not args.dry_run:
        print("有失败样本,可加 --resume 重跑未完成的。", file=sys.stderr)


if __name__ == "__main__":
    main()
