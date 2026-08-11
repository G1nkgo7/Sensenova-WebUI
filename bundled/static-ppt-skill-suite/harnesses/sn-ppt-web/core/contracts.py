"""Canonical Harness artifact contracts shared by runtime and acceptance gates.

Keep artifact names here instead of duplicating string literals across the
tool closeout allowlist and final acceptance. A required final artifact must
also be writable during deterministic Review closeout.
"""

REVIEW_ISSUES_PATH = "_trace/review-issues.md"
CONTENT_FIDELITY_PATH = "_trace/content-fidelity.md"

REVIEW_CLOSEOUT_ARTIFACTS = frozenset({
    REVIEW_ISSUES_PATH,
    CONTENT_FIDELITY_PATH,
})

