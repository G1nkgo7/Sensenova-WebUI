#!/usr/bin/env python3
"""Cross-platform launcher for SenseNova Present Studio.

The launcher deliberately uses only the Python standard library so it can
bootstrap the two managed ``uv`` environments before Studio dependencies are
installed.  Configuration precedence is:

    command line > process environment > .env file > built-in defaults
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STUDIO_ROOT = PROJECT_ROOT / "studio"
ENGINE_ROOT = PROJECT_ROOT / "inference"
BUNDLED_PRESENTER_SUITE = PROJECT_ROOT / "bundled" / "static-ppt-skill-suite"


def _load_env_file(path: Path) -> None:
    """Load a small dotenv/shell-export file without overriding the shell."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SystemExit(f"Cannot read environment file {path}: {exc}") from exc
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise SystemExit(f"Invalid environment entry at {path}:{number}")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "A").isalnum() or key[0].isdigit():
            raise SystemExit(f"Invalid environment name at {path}:{number}: {key!r}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _python_in(project: Path) -> Path:
    if os.name == "nt":
        return project / ".venv" / "Scripts" / "python.exe"
    return project / ".venv" / "bin" / "python"


def _venv_python(venv: Path) -> Path:
    """Return the interpreter inside a virtualenv directory itself."""
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _find_browser(root: Path) -> Path | None:
    names = {"chrome-headless-shell", "chrome-headless-shell.exe"}
    if not root.exists():
        return None
    for candidate in root.rglob("*"):
        if candidate.is_file() and candidate.name in names:
            return candidate
    return None


def _run(command: list[str], *, label: str) -> None:
    print(f"[SenseNova Present] {label}", flush=True)
    try:
        subprocess.run(command, cwd=PROJECT_ROOT, check=True)
    except FileNotFoundError as exc:
        raise SystemExit(f"Command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"{label} failed (exit={exc.returncode})") from exc


def _flag_default(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"", "0", "false", "no", "off"}


def _presenter_runtime_ready(normalize_python: Path, fonts_dir: Path) -> bool:
    return (
        normalize_python.is_file()
        and any(fonts_dir.glob("NotoSansSC*.ttf"))
        and any(fonts_dir.glob("NotoSerifSC*.ttf"))
    )


def _prepare_presenter_runtime(engine_python: Path, playwright_root: Path) -> tuple[Path, Path]:
    """Install the complete attachment/font runtime on Linux and macOS.

    Native Windows users are supported through Docker or WSL because the
    bundled skill installer is POSIX shell based. The WebUI itself still runs
    natively; an actionable warning is emitted instead of a cryptic failure.
    """
    runtime_root = Path(os.environ.get("SENSENOVA_RUNTIME_DIR", PROJECT_ROOT / "runtime"))
    runtime_root = runtime_root.expanduser().resolve()
    normalize_venv = runtime_root / "normalize"
    normalize_python = _venv_python(normalize_venv)
    fonts_dir = runtime_root / "fonts"
    install_script = (
        BUNDLED_PRESENTER_SUITE
        / "skills/long-horizon-presenter/scripts/install.sh"
    )
    if _presenter_runtime_ready(normalize_python, fonts_dir):
        return normalize_python, fonts_dir
    if os.name == "nt":
        print(
            "[SenseNova Present] Full attachment/font setup on native Windows "
            "requires Docker or WSL; see README.md.",
            flush=True,
        )
        return normalize_python, fonts_dir
    bash = shutil.which("bash")
    if not bash or not install_script.is_file():
        raise SystemExit(f"Presenter installer is missing: {install_script}")
    env = os.environ.copy()
    env.update({
        "PYBIN": str(engine_python),
        "NORMALIZE_VENV": str(normalize_venv),
        "FONTS_DIR": str(fonts_dir),
        "PLAYWRIGHT_BROWSERS_PATH": str(playwright_root),
        "PATH": str(engine_python.parent) + os.pathsep + env.get("PATH", ""),
    })
    print("[SenseNova Present] Preparing fonts and attachment parsers (first run only)", flush=True)
    try:
        subprocess.run(
            [bash, str(install_script), "normalize", "pymupdf", "fonts", "fonttools", "image-cutout", "material-tools"],
            cwd=PROJECT_ROOT,
            env=env,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Presenter runtime setup failed (exit={exc.returncode})") from exc
    if not _presenter_runtime_ready(normalize_python, fonts_dir):
        raise SystemExit("Presenter setup finished but required fonts/parsers are still missing")
    return normalize_python, fonts_dir


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "configured"
    return f"{value[:3]}…{value[-3:]}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Start SenseNova Present WebUI on Linux, macOS, or Windows."
    )
    parser.add_argument(
        "--language", choices=("zh", "en"),
        default=os.environ.get("STUDIO_LANGUAGE", "zh").strip().lower() or "zh",
        help="initial WebUI language (default: zh)",
    )
    parser.add_argument(
        "--host", default=os.environ.get("SENSE_NOVA_LOCAL_HOST", "127.0.0.1"),
        help="listen address (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("SENSE_NOVA_LOCAL_PORT", "8001")),
        help="listen port (default: 8001)",
    )
    parser.add_argument(
        "--edition", choices=("v1", "full"),
        default=os.environ.get("STUDIO_EDITION", "v1").strip().lower() or "v1",
        help="product profile (default: v1)",
    )
    parser.add_argument(
        "--env-file", type=Path,
        default=Path(os.environ.get("SENSENOVA_ENV_FILE", PROJECT_ROOT / ".env")),
        help="dotenv file loaded below existing shell variables (default: ./.env)",
    )
    parser.add_argument("--reload", action="store_true", help="enable Uvicorn development reload")
    parser.add_argument("--no-install", action="store_true", help="reuse existing virtual environments")
    parser.add_argument(
        "--no-browser-install", action="store_true",
        help="do not install Playwright Chromium when it is missing",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="validate and print the effective non-secret configuration without starting",
    )
    return parser


def main() -> int:
    # Read the conventional file first so its values can become argparse
    # defaults. Existing exported variables remain authoritative.
    preliminary = argparse.ArgumentParser(add_help=False)
    preliminary.add_argument(
        "--env-file", type=Path,
        default=Path(os.environ.get("SENSENOVA_ENV_FILE", PROJECT_ROOT / ".env")),
    )
    known, _ = preliminary.parse_known_args()
    env_file = known.env_file.expanduser().resolve()
    _load_env_file(env_file)
    args = _parser().parse_args()

    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")

    pipeline_root = Path(
        os.environ.get("PPTAGENT_CLEAN_PIPELINE_ROOT", PROJECT_ROOT / "vendor/static_ppt-clean-current")
    ).expanduser().resolve()
    bundled_skill = BUNDLED_PRESENTER_SUITE / "skills" / "long-horizon-presenter" / "SKILL.md"
    bundled_harness = BUNDLED_PRESENTER_SUITE / "harnesses" / "long-horizon-presenter" / "distill_ppt.py"
    has_bundled_presenter = bundled_skill.is_file() and bundled_harness.is_file()
    if not has_bundled_presenter and not (pipeline_root / "infer.py").is_file():
        raise SystemExit(
            "No runnable static generation bundle was found. Expected either "
            f"{bundled_skill} + {bundled_harness}, or {pipeline_root / 'infer.py'}."
        )

    uv_bin = os.environ.get("SENSE_NOVA_UV_BIN") or shutil.which("uv")
    if not args.no_install:
        if not uv_bin:
            raise SystemExit(
                "uv is required. Run start.sh/start.ps1 for automatic setup, "
                "or install it from https://docs.astral.sh/uv/."
            )
        _run([uv_bin, "sync", "--project", str(STUDIO_ROOT), "--frozen"], label="Preparing WebUI runtime")
        _run([uv_bin, "sync", "--project", str(ENGINE_ROOT), "--frozen"], label="Preparing generation runtime")

    studio_python = _python_in(STUDIO_ROOT)
    engine_python = _python_in(ENGINE_ROOT)
    if not studio_python.is_file():
        raise SystemExit(f"Studio runtime is missing: {studio_python}")
    if not engine_python.is_file():
        raise SystemExit(f"Generation runtime is missing: {engine_python}")

    playwright_root = Path(
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH", STUDIO_ROOT / "data/ms-playwright")
    ).expanduser().resolve()
    normalize_python = _venv_python(Path(os.environ.get(
        "SENSENOVA_RUNTIME_DIR", PROJECT_ROOT / "runtime"
    )).expanduser().resolve() / "normalize")
    fonts_dir = (
        Path(os.environ.get("SENSENOVA_RUNTIME_DIR", PROJECT_ROOT / "runtime"))
        .expanduser().resolve() / "fonts"
    )
    if not args.no_install and has_bundled_presenter:
        normalize_python, fonts_dir = _prepare_presenter_runtime(engine_python, playwright_root)
    browser = _find_browser(playwright_root)
    if browser is None and not args.no_browser_install:
        env = os.environ.copy()
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(playwright_root)
        print("[SenseNova Present] Installing Chromium renderer (first run only)", flush=True)
        subprocess.run(
            [str(engine_python), "-m", "playwright", "install", "chromium"],
            cwd=PROJECT_ROOT, env=env, check=True,
        )
        browser = _find_browser(playwright_root)

    is_v1 = args.edition == "v1"
    defaults = {
        "PPTAGENT_CLEAN_PIPELINE_ROOT": str(pipeline_root),
        "PPTAGENT_INFERENCE_ROOT": str(ENGINE_ROOT),
        "PPTAGENT_ENGINE_PYTHON": str(engine_python),
        "PLAYWRIGHT_BROWSERS_PATH": str(playwright_root),
        "NORMALIZE_PY": str(normalize_python),
        "PPT_FONT_SOURCE_DIRS": str(fonts_dir),
        "STUDIO_SESSION_COOKIE": "sense_nova_present_session",
        "STUDIO_EDITION": args.edition,
        "STUDIO_AUTH_ENABLED": "0" if is_v1 else "1",
        "STUDIO_DYNAMIC_ENABLED": "0" if is_v1 else "1",
        "STUDIO_SINGLE_USER_USERNAME": "user",
        "STUDIO_MAX_PER_MODEL": "0",
        "STUDIO_AGENT_MAX_TOKENS": "40960",
        "STUDENT_TEMPERATURE": "0.3",
        # Release deployments start with no internal test model exposed.
        # Operators may register one model through SENSENOVA_MODEL_* and users
        # can add/remove further OpenAI-compatible models in the WebUI.
        "PPTAGENT_PUBLIC_MODEL_KEYS": "",
    }
    if not is_v1:
        defaults.update({
            "DYNAMIC_RENDER_PYTHON": str(engine_python),
            "AGENTIC_SKILLS_DIR": str(PROJECT_ROOT / "dynamic/skills"),
        })
    if has_bundled_presenter:
        defaults.update({
            "PPTAGENT_LONG_HORIZON_PRESENTER_SUITE_ROOT": str(BUNDLED_PRESENTER_SUITE),
            "PPTAGENT_PUBLIC_SKILL_KEYS": "long-horizon-presenter",
            "PPTAGENT_DEFAULT_SKILL": "long-horizon-presenter",
            "PPTAGENT_LONG_HORIZON_PRESENTER_DISPLAY_NAME": "mural-presenter",
        })
    for key, value in defaults.items():
        os.environ.setdefault(key, value)
    configured_data_dir = os.environ.get("STUDIO_DATA_DIR", "").strip()
    if configured_data_dir:
        data_path = Path(configured_data_dir).expanduser()
        if not data_path.is_absolute():
            data_path = PROJECT_ROOT / data_path
        os.environ["STUDIO_DATA_DIR"] = str(data_path.resolve())
    os.environ["STUDIO_EDITION"] = args.edition
    os.environ["STUDIO_LANGUAGE"] = args.language
    os.environ["SENSE_NOVA_LOCAL_HOST"] = args.host
    os.environ["SENSE_NOVA_LOCAL_PORT"] = str(args.port)
    if browser:
        os.environ.setdefault("PPT_SKILL_BROWSER_EXE", str(browser))

    environment_model_url = os.environ.get("SENSENOVA_MODEL_BASE_URL", "").strip()
    environment_model_name = os.environ.get("SENSENOVA_MODEL_NAME", "").strip()
    if bool(environment_model_url) != bool(environment_model_name):
        raise SystemExit(
            "SENSENOVA_MODEL_BASE_URL and SENSENOVA_MODEL_NAME must be configured together"
        )

    public = {
        "url": f"http://{args.host}:{args.port}",
        "language": args.language,
        "edition": args.edition,
        "auth_enabled": _flag_default("STUDIO_AUTH_ENABLED", not is_v1),
        "dynamic_enabled": _flag_default("STUDIO_DYNAMIC_ENABLED", not is_v1),
        "pipeline_root": str(pipeline_root),
        "presenter_suite": str(
            BUNDLED_PRESENTER_SUITE if has_bundled_presenter else "external configuration"
        ),
        "default_skill": os.environ.get("PPTAGENT_DEFAULT_SKILL", "environment default"),
        "public_model_keys": [
            item.strip()
            for item in os.environ.get("PPTAGENT_PUBLIC_MODEL_KEYS", "").split(",")
            if item.strip()
        ],
        "environment_model": (
            os.environ.get("SENSENOVA_MODEL_DISPLAY_NAME", "").strip()
            or os.environ.get("SENSENOVA_MODEL_NAME", "").strip()
            or "not configured"
        ),
        "data_dir": os.environ.get("STUDIO_DATA_DIR", str(STUDIO_ROOT / "data")),
        "environment_file": str(env_file),
        "browser": str(browser or "not installed"),
        "normalize_python": str(normalize_python),
        "font_source_dirs": os.environ.get("PPT_FONT_SOURCE_DIRS", str(fonts_dir)),
        "external_services": {
            "model_key": _mask(os.environ.get("SENSENOVA_MODEL_API_KEY", "")),
            "image_url": os.environ.get("SENSENOVA_IMAGE_BASE_URL", ""),
            "image_key": _mask(os.environ.get("SENSENOVA_IMAGE_API_KEY", "")),
            "search_url": os.environ.get("SENSENOVA_SEARCH_BASE_URL", ""),
            "search_key": _mask(os.environ.get("SENSENOVA_SEARCH_API_KEY", "")),
        },
    }
    if args.check:
        print(json.dumps(public, ensure_ascii=False, indent=2))
        return 0

    command = [
        str(studio_python), "-m", "uvicorn", "app.main:app",
        "--host", args.host, "--port", str(args.port),
    ]
    if args.reload:
        command.append("--reload")
    print(f"[SenseNova Present] Ready at {public['url']} (language={args.language}, edition={args.edition})")
    try:
        return subprocess.call(command, cwd=STUDIO_ROOT, env=os.environ.copy())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
