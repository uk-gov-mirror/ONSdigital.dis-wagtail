from datetime import UTC, datetime

from django.test import TestCase

from cms.articles.models import StatisticalArticlePage
from cms.articles.tests.factories import StatisticalArticlePageFactory
from cms.bundles.tests.factories import BundleFactory, BundlePageFactory
from cms.bundles.utils import (
    BundleAPIBundleMetadata,
    build_bundle_data_for_api,
    build_bundle_data_from_api_response,
    get_bundleable_page_types,
    get_pages_in_active_bundles,
)
from cms.methodology.models import MethodologyPage
from cms.release_calendar.models import ReleaseCalendarPage
from cms.standard_pages.models import IndexPage, InformationPage
from cms.topics.models import TopicPage


class BundlesUtilsTestCase(TestCase):
    def test_get_bundleable_page_types(self):
        page_types = get_bundleable_page_types()
        page_types.sort(key=lambda x: x.__name__)
        self.assertListEqual(
            page_types,
            [
                IndexPage,
                InformationPage,
                MethodologyPage,
                ReleaseCalendarPage,
                StatisticalArticlePage,
                TopicPage,
            ],
        )

    def test_get_pages_in_active_bundles(self):
        self.assertListEqual(get_pages_in_active_bundles(), [])

        bundle = BundleFactory()
        published_bundle = BundleFactory(published=True)

        page_in_active_bundle = StatisticalArticlePageFactory()
        page_in_published_bundle = StatisticalArticlePageFactory(parent=page_in_active_bundle.get_parent())
        _page_not_in_bundle = StatisticalArticlePageFactory(parent=page_in_active_bundle.get_parent())

        BundlePageFactory(parent=bundle, page=page_in_active_bundle)
        BundlePageFactory(parent=published_bundle, page=page_in_published_bundle)

        self.assertListEqual(get_pages_in_active_bundles(), [page_in_active_bundle.pk])


class BundleAPIBundleMetadataTests(TestCase):
    """Tests for BundleAPIBundleMetadata dataclass.

    BundleAPIBundleMetadata is the core dataclass that normalizes bundle metadata
    for comparison between CMS and Bundle API. It normalizes:
    - preview_teams: None -> [], sorted by id for consistent comparison
    - scheduled_at: datetime/string -> UTC ISO-8601 format (no microseconds)
    - bundle_type: Auto-set to SCHEDULED if scheduled_at present, else MANUAL
    - managed_by: Defaults to WAGTAIL

    This normalization ensures that equivalent bundle data from different sources
    (CMS Bundle, Bundle API JSON) can be reliably compared for sync decisions.
    """

    def test_as_dict_returns_all_fields(self):
        """Test that as_dict returns a dictionary with all fields."""
        data = BundleAPIBundleMetadata(
            title="Test Bundle",
            bundle_type="MANUAL",
            state="DRAFT",
            managed_by="WAGTAIL",
            preview_teams=[],
            scheduled_at=None,
        )

        result = data.as_dict()

        expected = {
            "title": "Test Bundle",
            "bundle_type": "MANUAL",
            "state": "DRAFT",
            "managed_by": "WAGTAIL",
            "preview_teams": [],
            "scheduled_at": None,
        }
        self.assertEqual(result, expected)

    def test_defaults_managed_by_to_wagtail(self):
        """Test that managed_by defaults to WAGTAIL when not provided."""
        data = BundleAPIBundleMetadata(title="Test")

        self.assertEqual(data.managed_by, "WAGTAIL")

    def test_defaults_bundle_type_to_manual_when_no_schedule(self):
        """Test that bundle_type defaults to MANUAL when scheduled_at is None."""
        data = BundleAPIBundleMetadata(title="Test", scheduled_at=None)

        self.assertEqual(data.bundle_type, "MANUAL")

    def test_defaults_bundle_type_to_scheduled_when_date_provided(self):
        """Test that bundle_type defaults to SCHEDULED when scheduled_at has a value."""
        data = BundleAPIBundleMetadata(title="Test", scheduled_at="2025-12-01T10:00:00Z")

        self.assertEqual(data.bundle_type, "SCHEDULED")

    def test_respects_explicit_bundle_type(self):
        """Test that explicitly set bundle_type is not overridden."""
        data = BundleAPIBundleMetadata(
            title="Test",
            bundle_type="MANUAL",
            scheduled_at="2025-12-01T10:00:00Z",
        )

        self.assertEqual(data.bundle_type, "MANUAL")

    def test_normalizes_preview_teams_none_to_empty_list(self):
        """Test that None preview_teams is normalized to empty list."""
        data = BundleAPIBundleMetadata(title="Test", preview_teams=None)

        self.assertEqual(data.preview_teams, [])

    def test_normalizes_preview_teams_sorting_by_id(self):
        """Test that preview_teams are sorted by id for consistent comparison.

        This ensures that [{"id": "b"}, {"id": "a"}] and [{"id": "a"}, {"id": "b"}]
        are treated as equivalent after normalization.
        """
        data = BundleAPIBundleMetadata(
            title="Test",
            preview_teams=[
                {"id": "team-z"},
                {"id": "team-a"},
                {"id": "team-m"},
            ],
        )

        self.assertEqual(
            data.preview_teams,
            [
                {"id": "team-a"},
                {"id": "team-m"},
                {"id": "team-z"},
            ],
        )

    def test_normalizes_scheduled_at_to_utc_isoformat(self):
        """Test that datetime objects are normalized to UTC ISO-8601 format without microseconds."""
        cases = [
            datetime(2025, 12, 1, 10, 30, 45, tzinfo=UTC),  # UTC datetime
            datetime(2025, 12, 1, 10, 30, 45, 123456, tzinfo=UTC),  # UTC with microseconds
            "2025-12-01T10:30:45Z",  # UTC string Zulu
            "2025-12-01T10:30:45.123456Z",  # UTC string Zulu with microseconds
            "2025-12-01T11:30:45+01:00",  # +1 hour offset
            "2025-12-01T10:30:45",  # Naive datetime string (assumed UTC)
        ]

        for case in cases:
            with self.subTest(case=case):
                data = BundleAPIBundleMetadata(title="Test", scheduled_at=case)
                self.assertEqual(data.scheduled_at, "2025-12-01T10:30:45+00:00")

    def test_returns_unparseable_date_string_unchanged(self):
        """Test that invalid date strings are returned unchanged.

        This defensive behavior prevents crashes on malformed API data.
        """
        data = BundleAPIBundleMetadata(title="Test", scheduled_at="invalid-date")

        self.assertEqual(data.scheduled_at, "invalid-date")

    def test_equality_after_normalization(self):
        """Test that two instances with equivalent data are equal after normalization.

        This validates the core purpose of BundleAPIBundleMetadata: enabling reliable
        comparison of bundle metadata from different sources (CMS vs Bundle API).
        """
        data1 = BundleAPIBundleMetadata(
            title="Test",
            state="DRAFT",
            preview_teams=[{"id": "b"}, {"id": "a"}],
            scheduled_at=datetime(2025, 12, 1, 10, 0, 0, tzinfo=UTC),
        )

        data2 = BundleAPIBundleMetadata(
            title="Test",
            state="DRAFT",
            preview_teams=[{"id": "a"}, {"id": "b"}],
            scheduled_at="2025-12-01T11:00:00+01:00",  # Same instant in +1 offset
        )

        self.assertEqual(data1.as_dict(), data2.as_dict())


class BundleAPIBundleMetadataHelperTests(TestCase):
    """Tests for helper functions that create BundleAPIBundleMetadata instances.

    These are lightweight integration tests that verify the helpers correctly
    construct BundleAPIBundleMetadata instances. The heavy normalization testing
    is in BundleAPIBundleMetadataTests above.

    Tests:
    - build_bundle_data_for_api: Converts CMS Bundle model -> BundleAPIBundleMetadata
    - build_bundle_data_from_api_response: Converts Bundle API JSON -> BundleAPIBundleMetadata
    """

    def test_build_bundle_data_for_api_creates_normalized_instance(self):
        """Test that build_bundle_data_for_api converts Bundle model to BundleAPIBundleMetadata."""
        bundle = BundleFactory(name="Test Bundle")

        result = build_bundle_data_for_api(bundle)

        self.assertIsInstance(result, BundleAPIBundleMetadata)
        self.assertEqual(result.title, "Test Bundle")
        self.assertEqual(result.managed_by, "WAGTAIL")
        self.assertIsNotNone(result.state)

    def test_build_bundle_data_from_api_response_creates_normalized_instance(self):
        """Test that build_bundle_data_from_api_response creates BundleAPIBundleMetadata correctly."""
        api_response = {
            "title": "Test Bundle",
            "bundle_type": "MANUAL",
            "state": "DRAFT",
            "managed_by": "WAGTAIL",
            "preview_teams": [{"id": "team1"}],
            "scheduled_at": "2025-12-01T10:00:00Z",
        }

        result = build_bundle_data_from_api_response(api_response)

        self.assertIsInstance(result, BundleAPIBundleMetadata)
        self.assertEqual(result.title, "Test Bundle")
        self.assertEqual(result.bundle_type, "MANUAL")
        self.assertEqual(result.state, "DRAFT")
        self.assertEqual(result.managed_by, "WAGTAIL")
        self.assertEqual(result.preview_teams, [{"id": "team1"}])
        self.assertEqual(result.scheduled_at, "2025-12-01T10:00:00+00:00")

    def test_build_bundle_data_from_api_response_normalizes_via_dataclass(self):
        """Test that normalization happens via BundleAPIBundleMetadata.__post_init__.

        Verifies that preview_teams=None and microseconds are normalized.
        """
        api_response = {
            "title": "Test",
            "state": "DRAFT",
            "preview_teams": None,
            "scheduled_at": "2025-12-01T10:30:45.123456Z",
        }

        result = build_bundle_data_from_api_response(api_response)

        self.assertEqual(result.preview_teams, [])
        self.assertEqual(result.scheduled_at, "2025-12-01T10:30:45+00:00")
