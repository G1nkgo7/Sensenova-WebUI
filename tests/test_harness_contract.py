import importlib.util
from pathlib import Path


CORE_PATH = (
    Path(__file__).resolve().parents[1]
    / "bundled/static-ppt-skill-suite/harnesses/sn-ppt-web/core/__init__.py"
)
SPEC = importlib.util.spec_from_file_location("sn_ppt_web_contract", CORE_PATH)
CORE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CORE)


def test_research_output_accepts_bold_markdown_label_and_preserves_case():
    contract = CORE._final_contract(
        "**status:** ready\n"
        "**output:** research/Research.md\n"
        "**unresolved:** none\n"
    )

    assert contract["status"] == "ready"
    assert contract["output"] == "research/Research.md"
