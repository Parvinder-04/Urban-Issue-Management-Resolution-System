"""UIMRS Task 15 — unified site, analytics filters and 3D gallery tests.

Covers the deliverables that are new in Task 15 without weakening any existing
assertion:

* the unified landing page is served at ``/`` and the historical dashboard moved
  to ``/dashboard``;
* every internal link on the landing page resolves to a real route;
* the Future Development section contains exactly the three approved items;
* analytics and gallery pages are served with local assets only;
* the optional analytics date filters actually narrow historical and live data;
* the 3D gallery declares nine categories, local Three.js and a WebGL fallback.

All API tests use an isolated in-memory SQLite database. No project database is
opened or modified.
"""
import re
import sys
import unittest
from datetime import datetime
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
from app.core.config import FRONTEND_DIR, FRONTEND_PAGES
from app.database import Base, get_db
from app.main import app
from app.routers.auth import create_access_token, hash_password


LANDING_PAGE = FRONTEND_DIR / FRONTEND_PAGES["india"]
HISTORICAL_PAGE = FRONTEND_DIR / "index_india_staging.html"
ANALYTICS_PAGE = FRONTEND_DIR / "analytics.html"
GALLERY_PAGE = FRONTEND_DIR / "gallery.html"

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


def read_page(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestUnifiedRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_landing_page_is_root(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, LANDING_PAGE.read_bytes())
        self.assertIn("text/html", response.headers.get("content-type", ""))

    def test_historical_dashboard_moved_to_dashboard(self):
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, HISTORICAL_PAGE.read_bytes())

    def test_analytics_and_gallery_routes(self):
        analytics = self.client.get("/analytics")
        gallery = self.client.get("/gallery")
        self.assertEqual(analytics.status_code, 200)
        self.assertEqual(gallery.status_code, 200)
        self.assertEqual(analytics.content, ANALYTICS_PAGE.read_bytes())
        self.assertEqual(gallery.content, GALLERY_PAGE.read_bytes())

    def test_portal_routes_still_served(self):
        for route, expected in (
            ("/citizen", "citizen_portal.html"),
            ("/officer", "officer_portal.html"),
            ("/admin", "admin_portal.html"),
        ):
            response = self.client.get(route)
            self.assertEqual(response.status_code, 200, route)
            self.assertEqual(
                response.content,
                (FRONTEND_DIR / expected).read_bytes(),
                route,
            )

    def test_static_assets_are_served(self):
        css = self.client.get("/assets/uimrs.css")
        nav = self.client.get("/assets/uimrs-nav.js")
        three = self.client.get("/vendor/three/three.module.min.js")
        licence = self.client.get("/vendor/three/LICENSE")
        self.assertEqual(css.status_code, 200)
        self.assertIn("text/css", css.headers.get("content-type", ""))
        self.assertEqual(nav.status_code, 200)
        self.assertEqual(three.status_code, 200)
        self.assertEqual(licence.status_code, 200)
        self.assertIn("MIT License", licence.text)

    def test_favicon_does_not_404(self):
        response = self.client.get("/favicon.ico")
        self.assertEqual(response.status_code, 204)


class TestLandingPageContent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = read_page(LANDING_PAGE)

    def test_required_navigation_entries(self):
        for key in (
            "home", "dashboard", "citizen", "officer",
            "admin", "analytics", "gallery",
        ):
            self.assertIn('data-nav="%s"' % key, self.html)
        # The future section lives on the landing page itself.
        self.assertIn('href="#future"', self.html)

    def test_future_development_has_exactly_three_approved_cards(self):
        self.assertEqual(self.html.count('class="uimrs-future-card"'), 3)
        for title in (
            "Real-Time Municipal Integration",
            "Multilingual Voice-Based Reporting",
            "Mobile Application and Notifications",
        ):
            self.assertIn(title, self.html)
        self.assertEqual(self.html.count("Future / Planned"), 3)

    def test_no_dead_or_external_links(self):
        self.assertNotIn('href="#"', self.html)
        self.assertNotIn("unpkg.com", self.html)
        self.assertNotIn("cdn.jsdelivr", self.html)
        self.assertNotIn("cdnjs", self.html)

        allowed = {
            "/", "/dashboard", "/citizen", "/officer", "/admin",
            "/analytics", "/gallery", "/health", "/ready", "/docs",
            "/#future", "#future", "#data", "#how-it-works", "#main",
            "/assets/uimrs.css",
        }
        hrefs = re.findall(r'href="([^"]+)"', self.html)
        self.assertTrue(hrefs)
        for href in hrefs:
            if href.startswith("data:"):
                continue  # inline SVG favicon
            self.assertIn(href, allowed, "unexpected landing-page link: %s" % href)

    def test_scope_and_accuracy_disclaimers_present(self):
        # Normalise whitespace and case so a phrase split across HTML lines
        # still matches, without weakening the requirement itself.
        text = re.sub(r"\s+", " ", self.html).lower()
        for phrase in (
            "Gurugram is the primary operational city",
            "No live MCG or GMDA API connection",
            "not a pan-India deployment",
            "not AI image recognition",
            "not a digital twin",
            "Priority is an explainable heuristic",
            "[EVALUATION TEST]",
        ):
            self.assertIn(phrase.lower(), text)


class TestAnalyticsPageContent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = read_page(ANALYTICS_PAGE)

    def test_historical_and_live_sections_are_separated(self):
        text = re.sub(r"\s+", " ", self.html)
        self.assertIn("Public &middot; Historical data", text)
        self.assertIn("Restricted &middot; Live data", text)
        self.assertIn("never summed into a single figure", text)
        self.assertIn("They are never summed into a single figure.", text)

    def test_uses_real_endpoints_and_role_gating(self):
        for endpoint in (
            "/analytics/dashboard",
            "/analytics/departments",
            "/admin/overview",
            "/officer/overview",
            "/officer/issues",
            "/auth/login",
            "/auth/me",
        ):
            self.assertIn(endpoint, self.html)
        self.assertIn("sessionStorage", self.html)
        self.assertIn("officer and administrator accounts", self.html)

    def test_filters_and_honest_priority_note(self):
        self.assertIn('id="hist-from"', self.html)
        self.assertIn('id="hist-to"', self.html)
        self.assertIn("date_from", self.html)
        self.assertIn("date_to", self.html)
        self.assertIn("Priority distribution is not shown for historical data", self.html)

    def test_analytics_page_has_no_external_assets(self):
        self.assertNotIn("unpkg.com", self.html)
        self.assertNotIn("cdn.jsdelivr", self.html)
        self.assertNotIn("cdnjs", self.html)


class TestGalleryPageContent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = read_page(GALLERY_PAGE)

    def test_all_nine_categories_declared(self):
        for name in (
            "Road Damage / Potholes",
            "Garbage / Waste",
            "Streetlights",
            "Drainage / Sewage",
            "Water Supply",
            "Traffic Management / Signals",
            "Trees / Environment",
            "Illegal Construction",
            "Public Property Damage",
        ):
            self.assertIn(name, self.html)

    def test_local_threejs_and_no_external_hotlinks(self):
        self.assertIn('from "/vendor/three/three.module.min.js"', self.html)
        self.assertNotIn("unpkg.com", self.html)
        self.assertNotIn("cdn.jsdelivr", self.html)
        self.assertNotIn('src="http', self.html)
        self.assertNotIn('href="http', self.html)

    def test_interaction_and_fallback_requirements(self):
        for token in (
            "pointerdown",
            "pointermove",
            "wheel",
            "gal-reset",
            "WebGL is not available",
            "Building ",
            "Try again",
            "requestAnimationFrame",
            "ResizeObserver",
        ):
            self.assertIn(token, self.html)
        self.assertIn("replaceState", self.html)

    def test_illustrative_disclaimer(self):
        self.assertIn("not reconstructions of citizen complaints", self.html)
        self.assertIn("not AI image recognition", self.html)
        self.assertIn("not a live digital twin", self.html)


class AnalyticsDateFilterTests(unittest.TestCase):
    """Exercise the new optional date filters against an isolated database."""

    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)
        cls.password_hash = hash_password("Task15Analytics#2026")

    def setUp(self):
        self.db = TestingSessionLocal()
        self.old_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[get_db] = override_get_db

        for model in (
            models.AdminAuditLog,
            models.IssueStatusUpdate,
            models.IssueImage,
            models.Issue,
            models.User,
            models.Department,
        ):
            self.db.query(model).delete()
        self.db.commit()

        self.admin = models.User(
            name="Task 15 Admin",
            email="task15-admin@example.com",
            password_hash=self.password_hash,
            role=models.UserRole.ADMIN,
        )
        self.db.add(self.admin)
        self.db.commit()
        self.db.refresh(self.admin)
        self.token = create_access_token(self.admin)

        self.historical_2020 = self._issue(
            "T15-HIST-2020", datetime(2020, 6, 1, 9, 0, 0),
            historical=True, city="Bengaluru, Karnataka, India",
        )
        self.historical_2022 = self._issue(
            "T15-HIST-2022", datetime(2022, 3, 1, 9, 0, 0),
            historical=True, city="Bengaluru, Karnataka, India",
        )
        self.live_2024 = self._issue(
            "T15-LIVE-2024", datetime(2024, 8, 1, 9, 0, 0),
            historical=False, city="Gurugram, Haryana, India",
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()
        app.dependency_overrides = self.old_overrides

    def _issue(self, ticket, created_at, *, historical, city):
        issue = models.Issue(
            ticket_number=ticket,
            title="Task 15 analytics fixture",
            description="Fixture row for date-filter tests.",
            category=models.IssueCategory.ROAD_DAMAGE,
            category_confidence=0.0,
            category_source="india_keyword_rule",
            priority_score=10.0,
            priority_level=models.PriorityLevel.LOW,
            status=models.IssueStatus.REPORTED,
            latitude=12.9716 if historical else 28.4595,
            longitude=77.5946 if historical else 77.0266,
            reporter_id=self.admin.id,
            created_at=created_at,
            updated_at=created_at,
            city=city,
            is_historical=historical,
        )
        self.db.add(issue)
        self.db.flush()
        return issue

    def test_historical_date_window_is_applied(self):
        response = self.client.get(
            "/api/v1/analytics/dashboard"
            "?date_from=2020-01-01&date_to=2020-12-31"
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total_issues"], 1)

        response = self.client.get(
            "/api/v1/analytics/dashboard"
            "?date_from=2019-01-01&date_to=2021-12-31"
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total_issues"], 1)

        response = self.client.get(
            "/api/v1/analytics/dashboard"
            "?date_from=2021-01-01&date_to=2021-12-31"
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total_issues"], 0)

    def test_historical_filters_never_include_live_rows(self):
        response = self.client.get(
            "/api/v1/analytics/dashboard"
            "?date_from=2024-01-01&date_to=2024-12-31"
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total_issues"], 0)

    def test_invalid_or_reversed_dates_are_rejected(self):
        self.assertEqual(
            self.client.get("/api/v1/analytics/dashboard?date_from=not-a-date").status_code,
            422,
        )
        self.assertEqual(
            self.client.get(
                "/api/v1/analytics/dashboard"
                "?date_from=2024-01-01&date_to=2020-01-01"
            ).status_code,
            422,
        )

    def test_admin_overview_date_and_city_filters(self):
        headers = {"Authorization": "Bearer " + self.token}
        full = self.client.get("/api/v1/admin/overview", headers=headers)
        self.assertEqual(full.status_code, 200, full.text)
        self.assertEqual(full.json()["live"]["total"], 1)
        self.assertEqual(full.json()["historical"]["total"], 2)

        window = self.client.get(
            "/api/v1/admin/overview?date_from=2024-01-01&date_to=2024-12-31",
            headers=headers,
        )
        self.assertEqual(window.status_code, 200, window.text)
        self.assertEqual(window.json()["live"]["total"], 1)
        self.assertEqual(window.json()["historical"]["total"], 0)
        self.assertEqual(window.json()["filters"]["date_from"], "2024-01-01")

        city = self.client.get(
            "/api/v1/admin/overview?city=Gurugram%2C%20Haryana%2C%20India",
            headers=headers,
        )
        self.assertEqual(city.status_code, 200, city.text)
        self.assertEqual(city.json()["live"]["total"], 1)
        self.assertEqual(city.json()["historical"]["total"], 0)

    def test_live_overview_is_still_role_protected(self):
        self.assertEqual(self.client.get("/api/v1/admin/overview").status_code, 401)
        citizen = models.User(
            name="Task 15 Citizen",
            email="task15-citizen@example.com",
            password_hash=self.password_hash,
            role=models.UserRole.CITIZEN,
        )
        self.db.add(citizen)
        self.db.commit()
        token = create_access_token(citizen)
        response = self.client.get(
            "/api/v1/admin/overview",
            headers={"Authorization": "Bearer " + token},
        )
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
