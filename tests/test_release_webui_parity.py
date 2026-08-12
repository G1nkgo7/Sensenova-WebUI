import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReleaseWebUIParityTests(unittest.TestCase):
    def test_process_feed_keeps_local_deployment_autofollow_and_times(self):
        app = (ROOT / "studio/static/app.js").read_text(encoding="utf-8")
        for marker in (
            "function orchestrationEventTime(event)",
            "function orchestrationOutputTimeMarkup(value, label = \"记录\")",
            "function orchestrationTimingMarkup()",
            "const wasNearBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 120",
            "const shouldFollow = wasNearBottom || (firstFlowRender && running)",
            "top: shouldFollow ? scroller.scrollHeight : previousTop",
        ):
            self.assertIn(marker, app)

    def test_process_feed_keeps_terminal_states_and_revision_scoping(self):
        app = (ROOT / "studio/static/app.js").read_text(encoding="utf-8")
        trace = (ROOT / "studio/app/trace.py").read_text(encoding="utf-8")
        main = (ROOT / "studio/app/main.py").read_text(encoding="utf-8")
        css = (ROOT / "studio/static/app.css").read_text(encoding="utf-8")

        self.assertIn(
            '["completed", "failed", "rejected", "interrupted", "not_started"]',
            app,
        )
        self.assertIn("function renderStatus(p, phase)", app)
        self.assertIn("def _inherit_live_event_positions", trace)
        self.assertIn("def _attach_absolute_event_times", trace)
        self.assertIn('event["ts"] = _trace_iso(started_at + elapsed_s)', trace)
        self.assertGreaterEqual(main.count("trace.scoped_specialist_artifacts("), 3)
        self.assertIn(".orchestration-timing-card", css)
        self.assertIn(".orch-output-time", css)

    def test_public_release_still_exposes_only_the_renamed_workflow(self):
        engine = (ROOT / "studio/app/engine.py").read_text(encoding="utf-8")
        app = (ROOT / "studio/static/app.js").read_text(encoding="utf-8")
        self.assertIn('_DEFAULT_PUBLIC_SKILL_KEYS = ("sn-ppt-web",)', engine)
        self.assertIn('"sn-ppt-web": "sn-ppt-web"', app)
        self.assertNotIn('"long-horizon-presenter": "long-horizon-presenter"', app)


if __name__ == "__main__":
    unittest.main()
