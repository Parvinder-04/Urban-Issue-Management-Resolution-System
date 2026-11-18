"""
UIMRS Task 4 — Issue read security, public dashboard privacy and XSS safety.

Verifies:
A. Citizens can read only their own live complaints (never historical/private).
B. Officers can read only their own department's records.
C. Admins can read all authorised records.
D. The public historical dashboard stays accessible without login but exposes
   no reporter identifiers, private live complaint data or live coordinates.
E. The India staging page renders every untrusted value through safe DOM APIs.

Every API test runs against an isolated in-memory SQLite database. The real
project databases are never opened.
"""
import re
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import Base, get_db
from app.main import app
from app.routers.auth import create_access_token, hash_password

PAGE = PROJECT_ROOT / "frontend" / "index_india_staging.html"
XSS_PAYLOAD = "<img src=x onerror=alert(1)>"

TEST_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


class TestIssueReadSecurity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)
        # One bcrypt hash reused by fixtures: tokens are used directly, so the
        # password itself is irrelevant to these tests.
        cls.password_hash = hash_password("ReadSecurity#2026")

    def setUp(self):
        self.db = TestingSessionLocal()
        self.old_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[get_db] = override_get_db

        self.db.query(models.IssueStatusUpdate).delete()
        self.db.query(models.IssueImage).delete()
        self.db.query(models.Issue).delete()
        self.db.query(models.User).delete()
        self.db.query(models.Department).delete()
        self.db.commit()

        self._setup_fixtures()

    def tearDown(self):
        self.db.close()
        app.dependency_overrides = self.old_overrides

    # ------------------------------------------------------------------
    # Fixtures
    # ------------------------------------------------------------------
    def _user(self, name, email, role, department_id=None):
        return models.User(
            name=name,
            email=email,
            password_hash=self.password_hash,
            role=role,
            department_id=department_id,
        )

    def _issue(self, ticket, reporter, department, *, historical=False,
               status=models.IssueStatus.REPORTED, category=models.IssueCategory.ROAD_DAMAGE,
               lat=12.9716, lng=77.5946, title="Fixture issue", source_record_id=None):
        return models.Issue(
            ticket_number=ticket,
            title=title,
            description="Fixture description used only for isolated tests.",
            category=category,
            status=status,
            latitude=lat,
            longitude=lng,
            reporter_id=reporter.id,
            department_id=department.id if department else None,
            is_historical=historical,
            source_record_id=source_record_id,
        )

    def _setup_fixtures(self):
        self.dept_roads = models.Department(
            name="TEST_READ_ROADS",
            handles_category=models.IssueCategory.ROAD_DAMAGE,
            city="Bengaluru, Karnataka, India",
            source_agency="TEST_AGENCY_ROADS",
        )
        self.dept_garbage = models.Department(
            name="TEST_READ_GARBAGE",
            handles_category=models.IssueCategory.GARBAGE_WASTE,
            city="Bengaluru, Karnataka, India",
            source_agency="TEST_AGENCY_GARBAGE",
        )
        self.db.add_all([self.dept_roads, self.dept_garbage])
        self.db.commit()

        self.citizen_a = self._user(
            "TEST_READ_CITIZEN_A", "read.citizen.a@uimrs.org", models.UserRole.CITIZEN
        )
        self.citizen_b = self._user(
            "TEST_READ_CITIZEN_B", "read.citizen.b@uimrs.org", models.UserRole.CITIZEN
        )
        self.officer_roads = self._user(
            "TEST_READ_OFFICER_ROADS", "read.officer.roads@uimrs.org",
            models.UserRole.OFFICER, self.dept_roads.id,
        )
        self.officer_garbage = self._user(
            "TEST_READ_OFFICER_GARBAGE", "read.officer.garbage@uimrs.org",
            models.UserRole.OFFICER, self.dept_garbage.id,
        )
        self.officer_none = self._user(
            "TEST_READ_OFFICER_NONE", "read.officer.none@uimrs.org",
            models.UserRole.OFFICER, None,
        )
        self.admin = self._user(
            "TEST_READ_ADMIN", "read.admin@uimrs.org", models.UserRole.ADMIN
        )
        self.db.add_all([
            self.citizen_a, self.citizen_b, self.officer_roads,
            self.officer_garbage, self.officer_none, self.admin,
        ])
        self.db.commit()

        # --- Live citizen submissions -------------------------------------
        self.live_a_roads = self._issue(
            "READ-LIVE-A", self.citizen_a, self.dept_roads,
            title="PRIVATE-LIVE-A-TITLE", lat=28.4601, lng=77.0648,
        )
        self.live_b_roads = self._issue(
            "READ-LIVE-B", self.citizen_b, self.dept_roads,
            status=models.IssueStatus.IN_PROGRESS,
            title="PRIVATE-LIVE-B-TITLE", lat=28.4610, lng=77.0650,
        )
        self.live_b_garbage = self._issue(
            "READ-LIVE-G", self.citizen_b, self.dept_garbage,
            category=models.IssueCategory.GARBAGE_WASTE,
            title="PRIVATE-LIVE-GARBAGE-TITLE", lat=28.4700, lng=77.0700,
        )
        self.live_unassigned = self._issue(
            "READ-LIVE-U", self.citizen_b, None,
            category=models.IssueCategory.STREETLIGHT,
            title="PRIVATE-LIVE-COORDINATE-MARKER", lat=28.9999, lng=77.9999,
        )

        # --- Historical Bengaluru records (read-only, public source data) --
        self.hist_open_roads = self._issue(
            "READ-HIST-OPEN", self.admin, self.dept_roads,
            historical=True, status=models.IssueStatus.REPORTED,
            title="Historical open road record", lat=12.9716, lng=77.5946,
            source_record_id="HIST-OPEN-1",
        )
        self.hist_resolved_roads = self._issue(
            "READ-HIST-RES", self.admin, self.dept_roads,
            historical=True, status=models.IssueStatus.RESOLVED,
            title="Historical resolved road record", lat=12.9800, lng=77.6000,
            source_record_id="HIST-RES-1",
        )
        self.hist_garbage = self._issue(
            "READ-HIST-G", self.admin, self.dept_garbage,
            historical=True, category=models.IssueCategory.GARBAGE_WASTE,
            title="Historical garbage record", lat=12.9900, lng=77.6100,
            source_record_id="HIST-G-1",
        )
        self.hist_reported_by_a = self._issue(
            "READ-HIST-A", self.citizen_a, self.dept_roads,
            historical=True, title="Historical record with citizen reporter row",
            lat=12.9500, lng=77.5800, source_record_id="HIST-A-1",
        )
        self.hist_unassigned = self._issue(
            "READ-HIST-U", self.admin, None,
            historical=True, title="Historical unassigned record",
            lat=12.9600, lng=77.5900, source_record_id="HIST-U-1",
        )

        self.db.add_all([
            self.live_a_roads, self.live_b_roads, self.live_b_garbage,
            self.live_unassigned, self.hist_open_roads, self.hist_resolved_roads,
            self.hist_garbage, self.hist_reported_by_a, self.hist_unassigned,
        ])
        self.db.commit()

        self.db.add_all([
            models.IssueStatusUpdate(
                issue_id=self.live_a_roads.id,
                updated_by_id=self.officer_roads.id,
                old_status=models.IssueStatus.REPORTED,
                new_status=models.IssueStatus.IN_PROGRESS,
                comment="Fixture audit entry",
                created_at=datetime.now(timezone.utc),
            ),
            models.IssueStatusUpdate(
                issue_id=self.live_b_garbage.id,
                updated_by_id=self.officer_garbage.id,
                old_status=models.IssueStatus.REPORTED,
                new_status=models.IssueStatus.ACKNOWLEDGED,
                comment="Fixture audit entry",
                created_at=datetime.now(timezone.utc),
            ),
            models.IssueStatusUpdate(
                issue_id=self.hist_open_roads.id,
                updated_by_id=self.admin.id,
                old_status=models.IssueStatus.REPORTED,
                new_status=models.IssueStatus.IN_PROGRESS,
                comment="Fixture historical audit entry",
                created_at=datetime.now(timezone.utc),
            ),
        ])
        self.db.commit()

        self.token_a = create_access_token(self.citizen_a)
        self.token_b = create_access_token(self.citizen_b)
        self.token_roads = create_access_token(self.officer_roads)
        self.token_garbage = create_access_token(self.officer_garbage)
        self.token_none = create_access_token(self.officer_none)
        self.token_admin = create_access_token(self.admin)

    def _ids(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        return {row["id"] for row in response.json()}

    # ------------------------------------------------------------------
    # A. Citizen access
    # ------------------------------------------------------------------
    def test_citizen_can_read_own_live_issue(self):
        response = self.client.get(
            f"/api/v1/issues/{self.live_a_roads.id}", headers=bearer(self.token_a)
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["id"], self.live_a_roads.id)
        self.assertEqual(body["reporter_id"], self.citizen_a.id)

    def test_citizen_cannot_read_another_citizen_issue(self):
        response = self.client.get(
            f"/api/v1/issues/{self.live_b_roads.id}", headers=bearer(self.token_a)
        )
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("PRIVATE-LIVE-B-TITLE", response.text)

    def test_citizen_cannot_read_historical_record_even_if_reporter_matches(self):
        response = self.client.get(
            f"/api/v1/issues/{self.hist_reported_by_a.id}", headers=bearer(self.token_a)
        )
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("HIST-A-1", response.text)

    def test_citizen_list_is_scoped_to_own_live_issues(self):
        ids = self._ids(
            self.client.get("/api/v1/issues?limit=500", headers=bearer(self.token_a))
        )
        self.assertEqual(ids, {self.live_a_roads.id})

    def test_citizen_cannot_widen_scope_with_query_parameters(self):
        ids = self._ids(
            self.client.get(
                f"/api/v1/issues?limit=500&reporter_id={self.citizen_b.id}"
                f"&department_id={self.dept_garbage.id}&is_historical=true",
                headers=bearer(self.token_a),
            )
        )
        self.assertEqual(ids, {self.live_a_roads.id})

    def test_citizen_history_is_limited_to_own_live_issue(self):
        own = self.client.get(
            f"/api/v1/issues/{self.live_a_roads.id}/history",
            headers=bearer(self.token_a),
        )
        self.assertEqual(own.status_code, 200, own.text)
        self.assertEqual(len(own.json()), 1)

        other = self.client.get(
            f"/api/v1/issues/{self.live_b_roads.id}/history",
            headers=bearer(self.token_a),
        )
        self.assertEqual(other.status_code, 404)

        historical = self.client.get(
            f"/api/v1/issues/{self.hist_reported_by_a.id}/history",
            headers=bearer(self.token_a),
        )
        self.assertEqual(historical.status_code, 404)

    def test_anonymous_reads_are_rejected(self):
        for path in (
            "/api/v1/issues",
            f"/api/v1/issues/{self.live_a_roads.id}",
            f"/api/v1/issues/{self.live_a_roads.id}/history",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 401)

    # ------------------------------------------------------------------
    # B. Officer access
    # ------------------------------------------------------------------
    def test_officer_can_read_same_department_issue(self):
        response = self.client.get(
            f"/api/v1/issues/{self.live_a_roads.id}", headers=bearer(self.token_roads)
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["id"], self.live_a_roads.id)

    def test_officer_cannot_read_other_department_issue(self):
        response = self.client.get(
            f"/api/v1/issues/{self.live_b_garbage.id}", headers=bearer(self.token_roads)
        )
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("PRIVATE-LIVE-GARBAGE-TITLE", response.text)

    def test_officer_cannot_read_other_department_historical_record(self):
        response = self.client.get(
            f"/api/v1/issues/{self.hist_garbage.id}", headers=bearer(self.token_roads)
        )
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("HIST-G-1", response.text)

    def test_officer_list_is_scoped_to_department(self):
        ids = self._ids(
            self.client.get("/api/v1/issues?limit=500", headers=bearer(self.token_roads))
        )
        self.assertEqual(
            ids,
            {
                self.live_a_roads.id,
                self.live_b_roads.id,
                self.hist_open_roads.id,
                self.hist_resolved_roads.id,
                self.hist_reported_by_a.id,
            },
        )
        self.assertNotIn(self.live_b_garbage.id, ids)
        self.assertNotIn(self.hist_garbage.id, ids)
        self.assertNotIn(self.live_unassigned.id, ids)

    def test_officer_cannot_widen_scope_with_department_query_parameter(self):
        ids = self._ids(
            self.client.get(
                f"/api/v1/issues?limit=500&department_id={self.dept_garbage.id}",
                headers=bearer(self.token_roads),
            )
        )
        self.assertNotIn(self.live_b_garbage.id, ids)
        self.assertNotIn(self.hist_garbage.id, ids)

    def test_officer_history_follows_department_scope(self):
        own = self.client.get(
            f"/api/v1/issues/{self.live_a_roads.id}/history",
            headers=bearer(self.token_roads),
        )
        self.assertEqual(own.status_code, 200, own.text)
        self.assertEqual(len(own.json()), 1)

        other = self.client.get(
            f"/api/v1/issues/{self.live_b_garbage.id}/history",
            headers=bearer(self.token_roads),
        )
        self.assertEqual(other.status_code, 404)

        other_historical = self.client.get(
            f"/api/v1/issues/{self.hist_garbage.id}/history",
            headers=bearer(self.token_roads),
        )
        self.assertEqual(other_historical.status_code, 404)

    def test_officer_without_department_cannot_list_or_read(self):
        listing = self.client.get(
            "/api/v1/issues", headers=bearer(self.token_none)
        )
        self.assertEqual(listing.status_code, 403)

        record = self.client.get(
            f"/api/v1/issues/{self.live_a_roads.id}", headers=bearer(self.token_none)
        )
        self.assertEqual(record.status_code, 404)

    # ------------------------------------------------------------------
    # C. Admin access
    # ------------------------------------------------------------------
    def test_admin_can_read_live_and_historical_records(self):
        for issue in (
            self.live_a_roads,
            self.live_b_garbage,
            self.hist_open_roads,
            self.hist_garbage,
        ):
            with self.subTest(issue=issue.ticket_number):
                response = self.client.get(
                    f"/api/v1/issues/{issue.id}", headers=bearer(self.token_admin)
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["id"], issue.id)

    def test_admin_list_returns_all_records(self):
        ids = self._ids(
            self.client.get("/api/v1/issues?limit=500", headers=bearer(self.token_admin))
        )
        expected = {
            self.live_a_roads.id, self.live_b_roads.id, self.live_b_garbage.id,
            self.live_unassigned.id, self.hist_open_roads.id,
            self.hist_resolved_roads.id, self.hist_garbage.id,
            self.hist_reported_by_a.id, self.hist_unassigned.id,
        }
        self.assertEqual(ids, expected)

    def test_admin_can_read_any_history(self):
        for issue in (self.live_a_roads, self.live_b_garbage, self.hist_open_roads):
            with self.subTest(issue=issue.ticket_number):
                response = self.client.get(
                    f"/api/v1/issues/{issue.id}/history",
                    headers=bearer(self.token_admin),
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertGreaterEqual(len(response.json()), 1)

    # ------------------------------------------------------------------
    # D. Public historical dashboard
    # ------------------------------------------------------------------
    def test_public_historical_dashboard_requires_no_login(self):
        response = self.client.get("/api/v1/issues/dashboard?limit=1000")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()

        historical_ids = {
            self.hist_open_roads.id, self.hist_resolved_roads.id,
            self.hist_garbage.id, self.hist_reported_by_a.id,
            self.hist_unassigned.id,
        }
        returned_ids = {row["id"] for row in body}
        self.assertEqual(returned_ids, historical_ids)
        self.assertTrue(all(row["is_historical"] for row in body))
        for live in (
            self.live_a_roads, self.live_b_roads, self.live_b_garbage,
            self.live_unassigned,
        ):
            self.assertNotIn(live.id, returned_ids)

    def test_public_dashboard_exposes_no_reporter_or_private_fields(self):
        response = self.client.get("/api/v1/issues/dashboard?limit=1000")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body)

        forbidden = {
            "reporter_id", "reporter", "reporter_name", "reporter_email",
            "reporter_phone", "description", "address_text",
            "assigned_officer_id", "password", "password_hash",
        }
        for row in body:
            self.assertFalse(forbidden & set(row.keys()), row.keys())

        # No live complaint text or private identifiers may leak.
        self.assertNotIn("PRIVATE-LIVE", response.text)
        for live in (self.live_a_roads, self.live_b_roads, self.live_b_garbage):
            self.assertNotIn(live.ticket_number, response.text)

    def test_public_dashboard_preserves_historical_coordinates(self):
        response = self.client.get("/api/v1/issues/dashboard?limit=1000")
        self.assertEqual(response.status_code, 200, response.text)
        row = next(
            item for item in response.json() if item["id"] == self.hist_open_roads.id
        )
        self.assertAlmostEqual(row["lat"], self.hist_open_roads.latitude)
        self.assertAlmostEqual(row["lng"], self.hist_open_roads.longitude)
        self.assertEqual(row["source_record_id"], "HIST-OPEN-1")

    def test_public_analytics_exclude_live_issues(self):
        response = self.client.get("/api/v1/analytics/dashboard")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()

        self.assertEqual(body["total_issues"], 5)
        self.assertEqual(body["open_issues"], 4)
        categories = {row["category"]: row["count"] for row in body["category_breakdown"]}
        self.assertEqual(categories.get("Road_Damage"), 4)
        self.assertEqual(categories.get("Garbage_Waste"), 1)
        self.assertNotIn("Streetlight", categories)
        self.assertEqual(sum(categories.values()), 5)

    def test_public_departments_exclude_live_issues(self):
        response = self.client.get("/api/v1/analytics/departments")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()

        records = sum(row["records"] for row in body["departments"])
        self.assertEqual(records, 4)
        self.assertEqual(body["unassigned_records"], 1)
        self.assertEqual(body["unassigned_status_counts"], {"reported": 1})

        agency_names = {row["source_agency"] for row in body["departments"]}
        self.assertIn("TEST_AGENCY_ROADS", agency_names)
        self.assertIn("TEST_AGENCY_GARBAGE", agency_names)

    def test_public_heatmap_excludes_live_coordinates(self):
        response = self.client.get("/api/v1/analytics/heatmap")
        self.assertEqual(response.status_code, 200, response.text)
        points = {(round(p["lat"], 6), round(p["lng"], 6)) for p in response.json()}

        self.assertIn((12.9716, 77.5946), points)  # historical open record
        self.assertIn((12.99, 77.61), points)      # historical garbage record
        self.assertNotIn((28.4601, 77.0648), points)
        self.assertNotIn((28.9999, 77.9999), points)
        self.assertNotIn((28.47, 77.07), points)

    def test_public_analytics_do_not_require_login(self):
        for path in (
            "/api/v1/analytics/dashboard",
            "/api/v1/analytics/departments",
            "/api/v1/analytics/heatmap",
            "/api/v1/issues/dashboard?limit=10",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)


class TestStagingPageXSSSafety(unittest.TestCase):
    """Static guarantees that untrusted text never reaches an HTML sink."""

    @classmethod
    def setUpClass(cls):
        cls.html = PAGE.read_text(encoding="utf-8")
        cls.script = "\n".join(
            re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", cls.html, re.S)
        )

    def test_no_innerhtml_html_sink_remains(self):
        self.assertNotIn("innerHTML", self.script)
        self.assertNotIn("outerHTML", self.script)
        self.assertNotIn("insertAdjacentHTML", self.script)
        self.assertNotIn("document.write", self.script)

    def test_untrusted_values_use_text_nodes(self):
        self.assertIn("textContent", self.script)
        self.assertIn("createTextNode", self.script)
        self.assertIn("td.textContent=textValue", self.script)
        self.assertIn("b.textContent=textValue", self.script)
        self.assertIn("node.textContent=textValue", self.script)

    def test_map_popups_are_dom_elements_not_html_strings(self):
        self.assertIn("function issuePopup(x)", self.script)
        self.assertIn("function hotspotPopup(h)", self.script)
        self.assertIn("bindPopup(issuePopup(x))", self.script)
        self.assertIn("bindPopup(hotspotPopup(h))", self.script)
        self.assertNotIn("bindPopup(`", self.script)

    def test_chatbot_messages_use_textcontent(self):
        self.assertIn("function appendChatMessage(kind,message)", self.script)
        self.assertIn("div.textContent=textValue(message", self.script)
        self.assertNotIn("log.innerHTML", self.script)

    def test_tables_and_hotspot_list_are_built_with_dom(self):
        self.assertIn("tbody.replaceChildren(fragment)", self.script)
        self.assertIn("hotspotList.replaceChildren", self.script)
        self.assertIn("barsContainer.replaceChildren", self.script)
        self.assertIn("container.replaceChildren(fragment)", self.script)

    def test_xss_payload_is_never_treated_as_a_template(self):
        # The payload is only a test input; the page must contain no code path
        # that evaluates complaint/chatbot text as markup.
        self.assertNotIn(XSS_PAYLOAD, self.script)
        self.assertNotIn("eval(", self.script)
        self.assertNotIn("new Function(", self.script)


if __name__ == "__main__":
    unittest.main(verbosity=2)
