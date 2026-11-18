"""
UIMRS Task 3 — India staging dashboard repair validation.

Static validation of the served staging page:
* the known broken JavaScript fragment is gone,
* the inline script parses (Node.js ``--check`` when available),
* the page calls the real backend endpoint contracts,
* historical data is labelled truthfully and NULL metrics are not fabricated,
* map markers come from API coordinates with a Bengaluru focus.

No database and no browser are required for these checks.
"""
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PAGE = PROJECT_ROOT / "frontend" / "index_india_staging.html"


def read_page() -> str:
    return PAGE.read_text(encoding="utf-8")


def inline_scripts(html: str) -> str:
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    return "\n".join(blocks)


class TestStagingPageStructure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = read_page()
        cls.script = inline_scripts(cls.html)

    def test_page_exists_and_has_inline_script(self):
        self.assertTrue(PAGE.is_file())
        self.assertGreater(len(self.script), 1000)

    def test_broken_fragment_is_removed(self):
        # The old orphan fragment referenced an undefined `page` at top level and
        # duplicated switchPage's title map with a stale "AI Hotspots" label.
        self.assertNotIn("hotspots:'AI Hotspots'", self.script)
        self.assertNotIn("classList.toggle('active',x.dataset.page===page));document", self.script)
        self.assertEqual(self.script.count("function switchPage"), 1)

    def test_navigation_is_wired(self):
        self.assertIn("function switchPage(page)", self.script)
        self.assertIn("addEventListener('click',()=>switchPage(x.dataset.page))", self.script)
        for page in ("overview", "issues", "hotspots", "departments", "analytics", "support"):
            self.assertIn(f'id="{page}"', self.html)

    def test_inline_javascript_parses(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not installed; syntax validated manually elsewhere")
        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "staging_inline.js"
            script_path.write_text(self.script, encoding="utf-8")
            result = subprocess.run(
                [node, "--check", str(script_path)],
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_uses_existing_backend_endpoint_contracts(self):
        for endpoint in (
            "/api/v1/analytics/dashboard",
            "/api/v1/issues/dashboard?limit=500",
            "/api/v1/analytics/departments",
            "/api/v1/chatbot/message",
        ):
            self.assertIn(endpoint, self.script)

    def test_dashboard_consumes_real_response_fields(self):
        for field in (
            "total_issues",
            "open_issues",
            "resolved_this_week",
            "avg_resolution_hours",
            "category_breakdown",
            "hotspots",
            "source_record_id",
            "source_status",
            "source_agency",
            "is_historical",
        ):
            self.assertIn(field, self.script)
        for field in ("records", "reported", "in_progress", "resolved", "rejected", "unassigned_records"):
            self.assertIn(field, self.script)

    def test_chatbot_uses_post_and_existing_endpoint(self):
        self.assertIn("method:'POST'", self.script)
        self.assertIn("'/api/v1/chatbot/message'", self.script)


class TestTruthfulHistoricalPresentation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = read_page()
        cls.script = inline_scripts(cls.html)

    def test_historical_bengaluru_is_labelled(self):
        # The repeated long historical notice was removed. The dataset is still
        # identified by the existing Historical Bengaluru labels.
        self.assertIn("Historical Bengaluru", self.html)

    def test_read_only_claim_matches_current_write_policy(self):
        # Task 2 removed the global write block, so the page must not claim that
        # writes are disabled globally.
        self.assertNotIn("Writes are disabled", self.html)
        self.assertIn("Historical Bengaluru", self.html)

    def test_null_resolution_metrics_are_not_fabricated(self):
        self.assertIn("resolved_this_week==null", self.script)
        self.assertIn("avg_resolution_hours==null", self.script)
        self.assertIn("'Not available'", self.script)
        self.assertNotIn("nullh", self.script)

    def test_missing_priority_is_not_rendered_as_zero(self):
        self.assertIn("'Not recorded'", self.script)
        self.assertNotIn("priority_score||0", self.script)
        self.assertNotIn("priority_score ?? 0", self.script)

    def test_historical_and_live_records_are_distinguished(self):
        # Labels are still rendered, now through the safe `pill()` DOM helper.
        self.assertIn("'Historical'", self.script)
        self.assertIn("'Live'", self.script)
        self.assertIn("'Historical Bengaluru'", self.script)
        self.assertIn("'Live submission'", self.script)
        self.assertIn("'pill Hist'", self.script)
        self.assertIn("'pill Live'", self.script)

    def test_map_markers_come_from_api_coordinates(self):
        self.assertIn("issueData.filter", self.script)
        self.assertIn("x.lat", self.script)
        self.assertIn("x.lng", self.script)
        self.assertNotIn("Math.random", self.script)

    def test_map_focus_is_bengaluru_not_new_york(self):
        self.assertIn("[12.9716,77.5946]", self.script)
        self.assertNotIn("40.75", self.html)
        self.assertNotIn("-73.98", self.html)

    def test_map_container_keeps_its_300px_height(self):
        # Leaflet adds .leaflet-container{height:100%} to the map element; that
        # used to override .map{height:300px} and collapse both maps to ~61px.
        self.assertIn("div.map{height:300px", self.html)
        self.assertNotIn(".leaflet-container{height:100%", self.html)

    def test_maps_refit_to_historical_bounds_when_shown(self):
        self.assertIn("overviewMap.fitBounds(issueBounds.pad(0.1))", self.script)
        self.assertIn("hotspotMap.fitBounds(issueBounds.pad(0.1))", self.script)

    def test_no_foreign_city_statistics(self):
        for marker in ("New York", "NYC", "Philadelphia"):
            self.assertNotIn(marker, self.html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
