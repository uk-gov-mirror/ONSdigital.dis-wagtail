from unittest.mock import Mock

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from cms.bundles.bundle_api_sync_service import BundleAPISyncService
from cms.bundles.clients.api import BundleAPIClient, BundleAPIClientError, BundleAPIClientError404
from cms.bundles.enums import BundleStatus
from cms.bundles.tests.factories import BundleDatasetFactory, BundleFactory
from cms.datasets.tests.factories import DatasetFactory


@override_settings(DIS_DATASETS_BUNDLE_API_ENABLED=True)
class BundleAPISyncServiceTests(TestCase):
    """Tests for BundleAPISyncService."""

    def setUp(self):
        self.bundle = BundleFactory(name="Test Bundle", status=BundleStatus.DRAFT)
        self.api_client = Mock(spec=BundleAPIClient)
        self.original_datasets: set = set()

    def _create_service(self, bundle=None, api_client=None, original_datasets=None):
        """Helper to create a BundleAPISyncService instance."""
        return BundleAPISyncService(
            bundle=bundle or self.bundle,
            api_client=api_client or self.api_client,
            original_datasets=original_datasets or self.original_datasets,
        )

    def _mock_api_bundle(self, state="DRAFT", title="Test Bundle", etag="etag-123", **kwargs):
        """Helper to create a standard API bundle response."""
        return {
            "state": state,
            "title": title,
            "etag_header": etag,
            "bundle_type": kwargs.get("bundle_type", "MANUAL"),
            "managed_by": kwargs.get("managed_by", "WAGTAIL"),
            "preview_teams": kwargs.get("preview_teams", []),
            "scheduled_at": kwargs.get("scheduled_at"),
        }

    def _mock_api_content_item(self, content_id, dataset_id, edition_id="2024", version_id=1):
        """Helper to create a standard API content item."""
        return {
            "id": content_id,
            "metadata": {
                "dataset_id": dataset_id,
                "edition_id": edition_id,
                "version_id": version_id,
            },
        }

    def _setup_bundle_with_api_id(self, bundle_id="bundle-123", etag="etag-123"):
        """Helper to set up bundle with API ID and ETag."""
        self.bundle.bundle_api_bundle_id = bundle_id
        self.bundle.bundle_api_etag = etag
        self.bundle.save()

    def test_metadata_is_in_sync_when_identical(self):
        """Test metadata_is_in_sync returns True when local and remote metadata match."""
        self._setup_bundle_with_api_id()
        service = self._create_service()

        # API returns bundle with matching metadata
        self.api_client.get_bundle.return_value = {
            "state": "DRAFT",  # matches self.bundle.status
            "title": "Test Bundle",  # matches self.bundle.name
            "etag_header": "etag-123",
            "bundle_type": "MANUAL",  # matches default of BundleAPIBundleMetadata
            "managed_by": "WAGTAIL",  # matches default of BundleAPIBundleMetadata
            "preview_teams": [],  # matches default of BundleAPIBundleMetadata
            "scheduled_at": None,  # matches default of BundleAPIBundleMetadata
        }

        self.assertTrue(service.metadata_is_in_sync)

    def test_metadata_is_not_in_sync_when_different(self):
        """Test metadata_is_in_sync returns False when local and remote metadata differ."""
        self._setup_bundle_with_api_id()
        service = self._create_service()

        test_cases = [
            ("title", {"title": "Different Title"}),
            (
                "bundle_type",
                {
                    "bundle_type": "SCHEDULED",
                },
            ),
            ("state", {"state": "PUBLISHED"}),
            ("preview_teams", {"preview_teams": ["team1"]}),
            ("scheduled_at", {"scheduled_at": "2025-12-01T00:00:00Z"}),
        ]

        for field_name, override_kwargs in test_cases:
            with self.subTest(field=field_name):
                self.api_client.get_bundle.return_value = self._mock_api_bundle(**override_kwargs)
                result = service.metadata_is_in_sync
                self.assertFalse(result, f"Expected metadata_is_in_sync to be False for field '{field_name}'")

    def test_sync_creates_remote_bundle_when_missing_and_has_datasets(self):
        """Test sync creates remote bundle when it doesn't exist and there are datasets."""
        dataset = DatasetFactory(namespace="test-ns", edition="2024", version=1)
        BundleDatasetFactory(parent=self.bundle, dataset=dataset)

        self.bundle.bundle_api_bundle_id = ""
        service = self._create_service()

        self.api_client.create_bundle.return_value = {
            "id": "new-bundle-123",
            "etag_header": "new-etag",
            "state": "DRAFT",
        }
        self.api_client.get_bundle.return_value = self._mock_api_bundle(etag="new-etag")
        self.api_client.get_bundle_contents.return_value = {"items": []}
        self.api_client.add_content_to_bundle.return_value = {
            "id": "content-123",
            "etag_header": "content-etag",
            "metadata": {
                "dataset_id": "test-ns",
                "edition_id": "2024",
                "version_id": 1,
            },
        }

        service.sync()

        self.api_client.create_bundle.assert_called_once()
        self.bundle.refresh_from_db()
        self.assertEqual(self.bundle.bundle_api_bundle_id, "new-bundle-123")
        # The etag could be from bundle create or content sync
        self.assertIn(self.bundle.bundle_api_etag, ["new-etag", "content-etag"])

    def test_sync_does_nothing_when_no_remote_bundle_and_no_datasets(self):
        """Test sync does nothing when there's no remote bundle and no datasets."""
        self.bundle.bundle_api_bundle_id = ""
        service = self._create_service()

        service.sync()

        self.api_client.create_bundle.assert_not_called()
        self.api_client.get_bundle.assert_not_called()

    def test_sync_refreshes_etag_if_stale(self):
        """Test sync refreshes local ETag if it differs from API."""
        self.bundle.bundle_api_bundle_id = "bundle-123"
        self.bundle.bundle_api_etag = "old-etag"
        self.bundle.save()

        service = self._create_service()

        self.api_client.get_bundle.return_value = {
            "etag_header": "new-etag",
        }

        # Test the etag refresh method directly
        result = service._refresh_local_etag_if_stale()

        # Should return True because etag was stale
        self.assertTrue(result)

        # The bundle should be updated with the new etag
        self.bundle.refresh_from_db()
        self.assertEqual(self.bundle.bundle_api_etag, "new-etag")

    def test_sync_only_syncs_state_for_approved_bundles(self):
        """Test sync only syncs state for APPROVED bundles (immutable)."""
        self.bundle.status = BundleStatus.APPROVED
        self._setup_bundle_with_api_id()

        service = self._create_service()

        self.api_client.get_bundle.return_value = {
            "etag_header": "etag-123",
            "state": "DRAFT",
        }
        self.api_client.update_bundle_state.return_value = {
            "etag_header": "new-etag",
        }

        service.sync()

        self.api_client.update_bundle_state.assert_called_once_with(
            "bundle-123", state=BundleStatus.APPROVED, etag="etag-123"
        )
        self.api_client.get_bundle_contents.assert_not_called()
        self.api_client.update_bundle.assert_not_called()

    def test_sync_only_syncs_state_for_published_bundles(self):
        """Test sync only syncs state for PUBLISHED bundles (immutable)."""
        self.bundle.status = BundleStatus.PUBLISHED
        self._setup_bundle_with_api_id()

        service = self._create_service()

        self.api_client.get_bundle.return_value = {
            "etag_header": "etag-123",
            "state": "APPROVED",
        }
        self.api_client.update_bundle_state.return_value = {
            "etag_header": "new-etag",
        }

        service.sync()

        self.api_client.update_bundle_state.assert_called_once_with(
            "bundle-123", state=BundleStatus.PUBLISHED, etag="etag-123"
        )

    def test_sync_syncs_contents_and_metadata_for_draft_bundles(self):
        """Test sync syncs both contents and metadata for DRAFT bundles."""
        dataset = DatasetFactory(namespace="test-ns", edition="2024", version="1")
        BundleDatasetFactory(parent=self.bundle, dataset=dataset, bundle_api_content_id="content-123")

        self._setup_bundle_with_api_id()

        service = self._create_service()

        self.api_client.get_bundle.return_value = {
            "etag_header": "etag-123",
            "state": "DRAFT",
            "title": "Different Title",
        }
        self.api_client.get_bundle_contents.return_value = {
            "items": [
                {
                    "id": "content-123",
                    "metadata": {
                        "dataset_id": "test-ns",
                        "edition_id": "2024",
                        "version_id": "1",
                    },
                }
            ]
        }
        self.api_client.update_bundle.return_value = {"etag_header": "new-etag"}

        service.sync()

        self.api_client.update_bundle.assert_called_once()

    def test_sync_deletes_remote_bundle_on_create_failure(self):
        """Test sync deletes remote bundle if any step fails after creation."""
        BundleDatasetFactory(parent=self.bundle)

        self.bundle.bundle_api_bundle_id = ""
        service = self._create_service()

        self.api_client.create_bundle.return_value = {
            "id": "new-bundle-123",
            "etag_header": "new-etag",
        }
        self.api_client.get_bundle.side_effect = [{"etag_header": "new-etag"}]
        self.api_client.get_bundle_contents.side_effect = BundleAPIClientError("API Error")
        self.api_client.delete_bundle.return_value = None

        with self.assertRaises(ValidationError):
            service.sync()

        self.api_client.delete_bundle.assert_called_once_with("new-bundle-123")

    # validate complex logic or error handling that's difficult to reach via sync()

    def test_sync_refreshes_stale_etag(self):
        """Test sync refreshes local ETag when it differs from API."""
        dataset = DatasetFactory(namespace="test-ns", edition="2024", version=1)
        BundleDatasetFactory(parent=self.bundle, dataset=dataset, bundle_api_content_id="content-123")
        self._setup_bundle_with_api_id(etag="old-etag")

        service = self._create_service()
        self.api_client.get_bundle.return_value = self._mock_api_bundle(etag="new-etag")
        self.api_client.get_bundle_contents.return_value = {
            "items": [self._mock_api_content_item("content-123", "test-ns")]
        }

        service.sync()

        self.bundle.refresh_from_db()
        self.assertEqual(self.bundle.bundle_api_etag, "new-etag")

    def test_sync_does_not_modify_current_etag(self):
        """Test sync leaves ETag unchanged when it's already current."""
        dataset = DatasetFactory(namespace="test-ns", edition="2024", version=1)
        BundleDatasetFactory(parent=self.bundle, dataset=dataset, bundle_api_content_id="content-123")
        self._setup_bundle_with_api_id(etag="current-etag")

        service = self._create_service()
        self.api_client.get_bundle.return_value = self._mock_api_bundle(etag="current-etag")
        self.api_client.get_bundle_contents.return_value = {
            "items": [self._mock_api_content_item("content-123", "test-ns")]
        }

        service.sync()

        self.bundle.refresh_from_db()
        self.assertEqual(self.bundle.bundle_api_etag, "current-etag")

    def test_sync_updates_state_for_approved_bundle_when_different(self):
        """Test sync updates state for APPROVED bundle when it differs from API."""
        self.bundle.status = BundleStatus.APPROVED
        self._setup_bundle_with_api_id()

        service = self._create_service()
        self.api_client.get_bundle.return_value = self._mock_api_bundle(state="DRAFT")
        self.api_client.update_bundle_state.return_value = {"etag_header": "new-etag"}

        service.sync()

        self.api_client.update_bundle_state.assert_called_once_with(
            "bundle-123", state=BundleStatus.APPROVED, etag="etag-123"
        )
        self.bundle.refresh_from_db()
        self.assertEqual(self.bundle.bundle_api_etag, "new-etag")

    def test_sync_skips_state_update_when_already_matching(self):
        """Test sync skips state update when it already matches API."""
        dataset = DatasetFactory(namespace="test-ns", edition="2024", version=1)
        BundleDatasetFactory(parent=self.bundle, dataset=dataset, bundle_api_content_id="content-123")

        self.bundle.status = BundleStatus.DRAFT
        self._setup_bundle_with_api_id()

        service = self._create_service()

        self.api_client.get_bundle.return_value = self._mock_api_bundle()
        self.api_client.get_bundle_contents.return_value = {
            "items": [
                {
                    "id": "content-123",
                    "metadata": {"dataset_id": "test-ns", "edition_id": "2024", "version_id": 1},
                }
            ]
        }

        service.sync()

        self.api_client.update_bundle_state.assert_not_called()
        # Also verify no metadata update was made (should be in sync)
        self.api_client.update_bundle.assert_not_called()

    def test_sync_raises_validation_error_on_state_sync_failure(self):
        """Test sync raises ValidationError when state sync fails."""
        self.bundle.status = BundleStatus.APPROVED
        self._setup_bundle_with_api_id()

        service = self._create_service()

        self.api_client.get_bundle.return_value = {
            "state": "DRAFT",
            "etag_header": "etag-123",
        }
        self.api_client.update_bundle_state.side_effect = BundleAPIClientError("API Error")

        with self.assertRaises(ValidationError) as context:
            service.sync()

        self.assertIn("Failed to sync bundle state", str(context.exception))

    def test_sync_deletes_remote_bundle_when_no_datasets(self):
        """Test sync deletes remote bundle when CMS has no datasets."""
        self._setup_bundle_with_api_id()

        # Ensure no datasets exist
        self.assertEqual(self.bundle.bundled_datasets.count(), 0)

        service = self._create_service()

        self.api_client.get_bundle.return_value = self._mock_api_bundle()
        self.api_client.delete_bundle.return_value = None

        service.sync()

        self.api_client.delete_bundle.assert_called_once_with("bundle-123")
        self.bundle.refresh_from_db()
        self.assertEqual(self.bundle.bundle_api_bundle_id, "")
        self.assertEqual(self.bundle.bundle_api_etag, "")

    def test_sync_skips_reconciliation_when_no_drift_and_etag_current(self):
        """Test sync skips content reconciliation when no drift and ETag is current."""
        dataset = DatasetFactory(namespace="test-ns", edition="2024", version=1)
        bundled_dataset = BundleDatasetFactory(parent=self.bundle, dataset=dataset, bundle_api_content_id="content-123")

        self.bundle.bundle_api_bundle_id = "bundle-123"
        self.bundle.bundle_api_etag = "current-etag"
        self.bundle.save()

        # Original datasets match current - no drift
        service = self._create_service(original_datasets={bundled_dataset})

        self.api_client.get_bundle.return_value = self._mock_api_bundle(etag="current-etag")

        service.sync()

        self.api_client.get_bundle_contents.assert_not_called()
        self.api_client.add_content_to_bundle.assert_not_called()
        self.api_client.delete_content_from_bundle.assert_not_called()

    def test_sync_reconciles_when_etag_stale(self):
        """Test sync reconciles content when ETag is stale even without drift."""
        dataset = DatasetFactory(namespace="test-ns", edition="2024", version=1)
        bundled_dataset = BundleDatasetFactory(parent=self.bundle, dataset=dataset, bundle_api_content_id="content-123")

        self.bundle.bundle_api_bundle_id = "bundle-123"
        self.bundle.bundle_api_etag = "old-etag"
        self.bundle.save()

        # Original datasets match current - no drift
        service = self._create_service(original_datasets={bundled_dataset})

        self.api_client.get_bundle.return_value = self._mock_api_bundle(etag="new-etag")
        self.api_client.get_bundle_contents.return_value = {
            "items": [
                {
                    "id": "content-123",
                    "metadata": {
                        "dataset_id": "test-ns",
                        "edition_id": "2024",
                        "version_id": 1,
                    },
                }
            ]
        }

        service.sync()

        self.api_client.get_bundle_contents.assert_called_once_with("bundle-123")

    def test_sync_adds_new_datasets(self):
        """Test sync adds datasets that are in CMS but not in API."""
        dataset = DatasetFactory(namespace="test-ns", edition="2024", version=1)
        bundled_dataset = BundleDatasetFactory(parent=self.bundle, dataset=dataset, bundle_api_content_id="")

        self._setup_bundle_with_api_id()

        service = self._create_service()

        self.api_client.get_bundle.return_value = self._mock_api_bundle()
        self.api_client.get_bundle_contents.return_value = {"items": []}
        self.api_client.add_content_to_bundle.return_value = {
            "id": "new-content-123",
            "etag_header": "new-etag",
            "metadata": {
                "dataset_id": "test-ns",
                "edition_id": "2024",
                "version_id": 1,
            },
        }

        service.sync()

        self.api_client.add_content_to_bundle.assert_called_once()
        bundled_dataset.refresh_from_db()
        self.assertEqual(bundled_dataset.bundle_api_content_id, "new-content-123")

    def test_sync_deletes_removed_datasets(self):
        """Test sync deletes datasets that are in API but not in CMS."""
        dataset1 = DatasetFactory(namespace="test-ns", edition="2024", version=1)
        dataset2 = DatasetFactory(namespace="other-ns", edition="2024", version=2)

        bundled_dataset1 = BundleDatasetFactory(
            parent=self.bundle, dataset=dataset1, bundle_api_content_id="content-123"
        )
        BundleDatasetFactory(parent=self.bundle, dataset=dataset2, bundle_api_content_id="content-456")

        self._setup_bundle_with_api_id()

        original_datasets = set(self.bundle.bundled_datasets.all())

        # Remove only the first dataset from the bundle - keep the second one
        bundled_dataset1.delete()

        self.assertEqual(self.bundle.bundled_datasets.count(), 1)

        # But original_datasets should still contain both items
        self.assertEqual(len(original_datasets), 2)

        service = self._create_service(original_datasets=original_datasets)

        self.api_client.get_bundle.return_value = self._mock_api_bundle()
        # API still has both content items, but CMS only has the second one
        self.api_client.get_bundle_contents.return_value = {
            "items": [
                {
                    "id": "content-123",
                    "metadata": {
                        "dataset_id": "test-ns",
                        "edition_id": "2024",
                        "version_id": 1,
                    },
                },
                {
                    "id": "content-456",
                    "metadata": {
                        "dataset_id": "other-ns",
                        "edition_id": "2024",
                        "version_id": 2,
                    },
                },
            ]
        }
        self.api_client.delete_content_from_bundle.return_value = {"etag_header": "new-etag"}

        service.sync()

        self.api_client.delete_content_from_bundle.assert_called_once_with(
            bundle_id="bundle-123", content_id="content-123"
        )

    def test_sync_backfills_missing_content_ids(self):
        """Test sync backfills content IDs for items already in API."""
        dataset = DatasetFactory(namespace="test-ns", edition="2024", version="1")
        bundled_dataset = BundleDatasetFactory(
            parent=self.bundle,
            dataset=dataset,
            bundle_api_content_id="",
        )

        self.bundle.bundle_api_bundle_id = "bundle-123"
        self.bundle.save()

        service = self._create_service()

        self.api_client.get_bundle.return_value = self._mock_api_bundle()
        self.api_client.get_bundle_contents.return_value = {
            "items": [
                {
                    "id": "existing-content-123",
                    "metadata": {
                        "dataset_id": "test-ns",
                        "edition_id": "2024",
                        "version_id": "1",
                    },
                }
            ]
        }

        service.sync()

        # Verify content ID was backfilled via sync()
        bundled_dataset.refresh_from_db()
        self.assertEqual(bundled_dataset.bundle_api_content_id, "existing-content-123")
        self.api_client.add_content_to_bundle.assert_not_called()

    def test_sync_raises_validation_error_when_adding_content_fails(self):
        """Test sync raises ValidationError when adding content to API fails."""
        dataset = DatasetFactory(namespace="test-ns", edition="2024", version="1")
        BundleDatasetFactory(parent=self.bundle, dataset=dataset, bundle_api_content_id="")

        self._setup_bundle_with_api_id()

        service = self._create_service()

        self.api_client.get_bundle.return_value = self._mock_api_bundle()
        self.api_client.get_bundle_contents.return_value = {"items": []}
        self.api_client.add_content_to_bundle.side_effect = BundleAPIClientError("API Error")

        with self.assertRaises(ValidationError) as context:
            service.sync()

        self.assertIn("Failed to add dataset to bundle", str(context.exception))

    def test_sync_raises_validation_error_when_api_doesnt_return_content_id(self):
        """Test sync raises ValidationError when API doesn't return content ID."""
        dataset = DatasetFactory(namespace="test-ns", edition="2024", version="1")
        BundleDatasetFactory(parent=self.bundle, dataset=dataset, bundle_api_content_id="")

        self._setup_bundle_with_api_id()

        service = self._create_service()

        self.api_client.get_bundle.return_value = self._mock_api_bundle()
        self.api_client.get_bundle_contents.return_value = {"items": []}
        # Return response without matching metadata (no content ID for our dataset)
        self.api_client.add_content_to_bundle.return_value = {
            "etag_header": "new-etag",
            "metadata": {
                "dataset_id": "different-ns",
                "edition_id": "2024",
                "version_id": "1",
            },
        }

        with self.assertRaises(ValidationError) as context:
            service.sync()

        self.assertIn("did not return a content ID", str(context.exception))

    def test_sync_raises_validation_error_when_deleting_content_fails(self):
        """Test sync raises ValidationError when deleting content from API fails."""
        dataset1 = DatasetFactory(namespace="test-ns", edition="2024", version=1)
        dataset2 = DatasetFactory(namespace="keep-ns", edition="2024", version=2)

        bundled_dataset1 = BundleDatasetFactory(
            parent=self.bundle, dataset=dataset1, bundle_api_content_id="content-to-delete"
        )
        BundleDatasetFactory(parent=self.bundle, dataset=dataset2, bundle_api_content_id="content-to-keep")

        self.bundle.bundle_api_bundle_id = "bundle-123"
        self.bundle.bundle_api_etag = "old-etag"
        self.bundle.save()

        original_datasets = set(self.bundle.bundled_datasets.all())
        bundled_dataset1.delete()

        service = self._create_service(original_datasets=original_datasets)

        self.api_client.get_bundle.return_value = self._mock_api_bundle(etag="new-etag")
        # API still has both contents - one should be deleted, one should remain
        self.api_client.get_bundle_contents.return_value = {
            "items": [
                {
                    "id": "content-to-delete",
                    "metadata": {"dataset_id": "test-ns", "edition_id": "2024", "version_id": 1},
                },
                {
                    "id": "content-to-keep",
                    "metadata": {"dataset_id": "keep-ns", "edition_id": "2024", "version_id": 2},
                },
            ]
        }
        self.api_client.delete_content_from_bundle.side_effect = BundleAPIClientError("API Error")

        with self.assertRaises(ValidationError) as context:
            service.sync()

        self.assertIn("Failed to delete dataset from bundle", str(context.exception))

    def test_sync_updates_metadata_when_out_of_sync(self):
        """Test sync updates metadata when it differs from API."""
        dataset = DatasetFactory(namespace="test-ns", edition="2024", version=1)
        BundleDatasetFactory(parent=self.bundle, dataset=dataset, bundle_api_content_id="content-123")

        self.bundle.bundle_api_bundle_id = "bundle-123"
        self.bundle.bundle_api_etag = "old-etag"
        self.bundle.save()

        service = self._create_service()

        self.api_client.get_bundle.return_value = self._mock_api_bundle(
            title="Different Title",  # triggers sync
            etag="old-etag",
        )
        self.api_client.get_bundle_contents.return_value = {
            "items": [
                {
                    "id": "content-123",
                    "metadata": {"dataset_id": "test-ns", "edition_id": "2024", "version_id": 1},
                }
            ]
        }
        self.api_client.update_bundle.return_value = {"etag_header": "new-etag"}

        service.sync()

        self.api_client.update_bundle.assert_called_once()
        self.bundle.refresh_from_db()
        self.assertEqual(self.bundle.bundle_api_etag, "new-etag")

    def test_sync_skips_metadata_update_when_in_sync(self):
        """Test sync skips metadata update when already in sync."""
        dataset = DatasetFactory(namespace="test-ns", edition="2024", version=1)
        BundleDatasetFactory(parent=self.bundle, dataset=dataset, bundle_api_content_id="content-123")

        self._setup_bundle_with_api_id()

        service = self._create_service()

        self.api_client.get_bundle.return_value = self._mock_api_bundle()
        self.api_client.get_bundle_contents.return_value = {
            "items": [
                {
                    "id": "content-123",
                    "metadata": {"dataset_id": "test-ns", "edition_id": "2024", "version_id": 1},
                }
            ]
        }

        service.sync()

        self.api_client.update_bundle.assert_not_called()

    def test_sync_creates_in_review_bundle_as_draft_then_syncs_metadata(self):
        """Test sync creates IN_REVIEW bundles as DRAFT in API, then syncs metadata to push IN_REVIEW status."""
        dataset = DatasetFactory(namespace="test-ns", edition="2024", version=1)
        BundleDatasetFactory(parent=self.bundle, dataset=dataset)

        # Bundle is IN_REVIEW but has no remote bundle yet
        self.bundle.status = BundleStatus.IN_REVIEW
        self.bundle.bundle_api_bundle_id = ""
        self.bundle.save()

        service = self._create_service()

        # Track the state passed to create_bundle
        created_bundle_data = None

        def capture_create_bundle(bundle_data):
            nonlocal created_bundle_data
            created_bundle_data = bundle_data
            return {
                "id": "new-bundle-123",
                "etag_header": "create-etag",
                "state": "DRAFT",  # API returns DRAFT
            }

        self.api_client.create_bundle.side_effect = capture_create_bundle

        self.api_client.get_bundle.return_value = self._mock_api_bundle(etag="create-etag")
        self.api_client.get_bundle_contents.return_value = {"items": []}
        self.api_client.add_content_to_bundle.return_value = {
            "id": "content-123",
            "etag_header": "content-etag",
            "metadata": {"dataset_id": "test-ns", "edition_id": "2024", "version_id": 1},
        }
        self.api_client.update_bundle.return_value = {"etag_header": "metadata-sync-etag"}

        service.sync()

        self.api_client.create_bundle.assert_called_once()
        self.assertIsNotNone(created_bundle_data)
        self.assertEqual(created_bundle_data.state, BundleStatus.DRAFT)

        self.api_client.update_bundle.assert_called_once()
        call_args = self.api_client.update_bundle.call_args
        synced_bundle_data = call_args[1]["bundle_data"]
        self.assertEqual(synced_bundle_data.state, BundleStatus.IN_REVIEW)

        # Verify final state
        self.bundle.refresh_from_db()
        self.assertEqual(self.bundle.bundle_api_bundle_id, "new-bundle-123")
        self.assertEqual(self.bundle.status, BundleStatus.IN_REVIEW)  # Local status unchanged

    def test_sync_wraps_non_validation_errors(self):
        """Test sync wraps non-ValidationError exceptions in ValidationError."""
        dataset = DatasetFactory(namespace="test-ns", edition="2024", version=1)
        BundleDatasetFactory(parent=self.bundle, dataset=dataset, bundle_api_content_id="content-123")

        self.bundle.bundle_api_bundle_id = "bundle-123"
        self.bundle.save()

        service = self._create_service()

        self.api_client.get_bundle.return_value = {"etag_header": "etag-123"}
        self.api_client.get_bundle_contents.side_effect = RuntimeError("Unexpected error")

        with self.assertRaises(ValidationError) as context:
            service.sync()

        self.assertIn("Failed to sync bundle with Bundle API", str(context.exception))

    def test_sync_reraises_validation_errors_without_wrapping(self):
        """Test sync re-raises ValidationError without wrapping it (line 116 coverage)."""
        dataset = DatasetFactory(namespace="test-ns", edition="2024", version=1)
        BundleDatasetFactory(parent=self.bundle, dataset=dataset, bundle_api_content_id="")  # No content ID = drift

        self._setup_bundle_with_api_id()

        service = self._create_service()

        self.api_client.get_bundle.return_value = self._mock_api_bundle()

        # Make get_bundle_contents raise a ValidationError directly
        original_error = ValidationError("Original validation error")
        self.api_client.get_bundle_contents.side_effect = original_error

        # Should re-raise the same ValidationError, not wrap it
        with self.assertRaises(ValidationError) as context:
            service.sync()

        self.assertIn("Original validation error", str(context.exception))

    def test_sync_raises_error_when_metadata_update_fails(self):
        """Test sync raises BundleAPIClientError when metadata update fails."""
        dataset = DatasetFactory(namespace="test-ns", edition="2024", version=1)
        BundleDatasetFactory(parent=self.bundle, dataset=dataset, bundle_api_content_id="content-123")

        self._setup_bundle_with_api_id()

        service = self._create_service()

        self.api_client.get_bundle.return_value = self._mock_api_bundle(title="Different Title")
        self.api_client.get_bundle_contents.return_value = {
            "items": [
                {
                    "id": "content-123",
                    "metadata": {"dataset_id": "test-ns", "edition_id": "2024", "version_id": 1},
                }
            ]
        }

        # Make update_bundle fail
        self.api_client.update_bundle.side_effect = BundleAPIClientError("API Error")

        with self.assertRaises(ValidationError) as context:
            service.sync()

        self.assertIn("Failed to sync bundle with Bundle API", str(context.exception))

    def test_sync_handles_add_and_delete_simultaneously(self):
        """Test sync handles adding and deleting datasets in the same sync."""
        dataset1 = DatasetFactory(namespace="ns1", edition="2024", version=1)
        dataset2 = DatasetFactory(namespace="ns2", edition="2024", version=2)

        BundleDatasetFactory(
            parent=self.bundle,
            dataset=dataset1,
            bundle_api_content_id="",
        )
        BundleDatasetFactory(
            parent=self.bundle,
            dataset=dataset2,
            bundle_api_content_id="content-456",
        )

        self.bundle.bundle_api_bundle_id = "bundle-123"
        self.bundle.bundle_api_etag = "old-etag"  # Stale to force reconciliation
        self.bundle.save()

        dataset3 = DatasetFactory(namespace="ns3", edition="2024", version=3)
        removed_dataset = BundleDatasetFactory(
            parent=self.bundle, dataset=dataset3, bundle_api_content_id="content-789"
        )
        original_datasets = set(self.bundle.bundled_datasets.all())
        removed_dataset.delete()  # Remove dataset3

        service = self._create_service(original_datasets=original_datasets)

        self.api_client.get_bundle.return_value = self._mock_api_bundle(etag="new-etag")
        # API has content-789 (to be deleted) and content-456 (to keep), missing ns1
        self.api_client.get_bundle_contents.return_value = {
            "items": [
                {
                    "id": "content-456",
                    "metadata": {"dataset_id": "ns2", "edition_id": "2024", "version_id": 2},
                },
                {
                    "id": "content-789",  # Should be deleted
                    "metadata": {"dataset_id": "ns3", "edition_id": "2024", "version_id": 3},
                },
            ]
        }
        self.api_client.add_content_to_bundle.return_value = {
            "id": "content-123",
            "etag_header": "add-etag",
            "metadata": {"dataset_id": "ns1", "edition_id": "2024", "version_id": 1},
        }
        self.api_client.delete_content_from_bundle.return_value = {"etag_header": "delete-etag"}

        service.sync()

        self.api_client.add_content_to_bundle.assert_called_once()
        self.api_client.delete_content_from_bundle.assert_called_once_with(
            bundle_id="bundle-123", content_id="content-789"
        )

        # Verify the final ETag is from the delete operation (happens last)
        self.bundle.refresh_from_db()
        self.assertEqual(self.bundle.bundle_api_etag, "delete-etag")

    def test_sync_handles_add_delete_and_backfill_simultaneously(self):
        """Test sync handles adding, deleting, and backfilling datasets in a single sync."""
        # Existing dataset with content ID (keep)
        dataset1 = DatasetFactory(namespace="ns1", edition="2024", version=1)
        BundleDatasetFactory(parent=self.bundle, dataset=dataset1, bundle_api_content_id="content-1")

        # New dataset to add
        dataset2 = DatasetFactory(namespace="ns2", edition="2024", version=2)
        BundleDatasetFactory(parent=self.bundle, dataset=dataset2, bundle_api_content_id="")

        # Dataset to backfill (exists in API but no local content ID)
        dataset3 = DatasetFactory(namespace="ns3", edition="2024", version=3)
        bundled_dataset3 = BundleDatasetFactory(parent=self.bundle, dataset=dataset3, bundle_api_content_id="")

        # Dataset to delete
        dataset4 = DatasetFactory(namespace="ns4", edition="2024", version=4)
        removed_dataset = BundleDatasetFactory(parent=self.bundle, dataset=dataset4, bundle_api_content_id="content-4")

        self.bundle.bundle_api_bundle_id = "bundle-123"
        self.bundle.bundle_api_etag = "old-etag"  # Stale to force reconciliation
        self.bundle.save()

        original_datasets = set(self.bundle.bundled_datasets.all())
        removed_dataset.delete()

        service = self._create_service(original_datasets=original_datasets)

        self.api_client.get_bundle.return_value = self._mock_api_bundle(etag="new-etag")
        # API has content-1 (keep), content-3 (backfill), content-4 (delete)
        self.api_client.get_bundle_contents.return_value = {
            "items": [
                {"id": "content-1", "metadata": {"dataset_id": "ns1", "edition_id": "2024", "version_id": 1}},
                {"id": "content-3", "metadata": {"dataset_id": "ns3", "edition_id": "2024", "version_id": 3}},
                {"id": "content-4", "metadata": {"dataset_id": "ns4", "edition_id": "2024", "version_id": 4}},
            ]
        }

        self.api_client.add_content_to_bundle.return_value = {
            "id": "content-2",
            "etag_header": "add-etag",
            "metadata": {"dataset_id": "ns2", "edition_id": "2024", "version_id": 2},
        }

        self.api_client.delete_content_from_bundle.return_value = {"etag_header": "delete-etag"}

        service.sync()

        # Verify backfill happened
        bundled_dataset3.refresh_from_db()
        self.assertEqual(bundled_dataset3.bundle_api_content_id, "content-3")

        self.api_client.add_content_to_bundle.assert_called_once()

        self.api_client.delete_content_from_bundle.assert_called_once_with(
            bundle_id="bundle-123", content_id="content-4"
        )

        # Verify final ETag is from delete (happens last)
        self.bundle.refresh_from_db()
        self.assertEqual(self.bundle.bundle_api_etag, "delete-etag")

    def test_sync_forces_reconciliation_when_etag_is_stale(self):
        """Test sync forces full reconciliation when ETag is stale, even without local drift."""
        dataset = DatasetFactory(namespace="test-ns", edition="2024", version=1)
        bundled_dataset = BundleDatasetFactory(parent=self.bundle, dataset=dataset, bundle_api_content_id="content-123")

        self.bundle.bundle_api_bundle_id = "bundle-123"
        self.bundle.bundle_api_etag = "old-etag"
        self.bundle.save()

        # No local drift - original datasets match current
        service = self._create_service(original_datasets={bundled_dataset})

        self.api_client.get_bundle.return_value = {
            "title": "Test Bundle",
            "state": "DRAFT",
            "etag_header": "new-etag",
        }

        # API contents match CMS
        self.api_client.get_bundle_contents.return_value = {
            "items": [
                {
                    "id": "content-123",
                    "metadata": {"dataset_id": "test-ns", "edition_id": "2024", "version_id": 1},
                }
            ]
        }

        service.sync()

        self.api_client.get_bundle_contents.assert_called_once()

        # Verify ETag was refreshed
        self.bundle.refresh_from_db()
        self.assertEqual(self.bundle.bundle_api_etag, "new-etag")

    def test_sync_with_in_review_status_syncs_contents_and_metadata(self):
        """Test sync with IN_REVIEW status syncs both contents and metadata, not just state."""
        dataset = DatasetFactory(namespace="test-ns", edition="2024", version=1)
        BundleDatasetFactory(parent=self.bundle, dataset=dataset, bundle_api_content_id="content-123")

        self.bundle.status = BundleStatus.IN_REVIEW
        self._setup_bundle_with_api_id()

        service = self._create_service()

        self.api_client.get_bundle.return_value = {
            "etag_header": "etag-123",
            "state": "IN_REVIEW",
            "title": "Different Title",
        }
        self.api_client.get_bundle_contents.return_value = {
            "items": [
                {
                    "id": "content-123",
                    "metadata": {"dataset_id": "test-ns", "edition_id": "2024", "version_id": 1},
                }
            ]
        }
        self.api_client.update_bundle.return_value = {"etag_header": "new-etag"}

        service.sync()

        self.api_client.update_bundle.assert_called_once()
        self.api_client.update_bundle_state.assert_not_called()

    def test_sync_deletes_remote_bundle_on_metadata_sync_failure(self):
        """Test sync deletes remote bundle if metadata sync fails after bundle creation."""
        dataset = DatasetFactory(namespace="test-ns", edition="2024", version=1)
        BundleDatasetFactory(parent=self.bundle, dataset=dataset)

        self.bundle.bundle_api_bundle_id = ""
        service = self._create_service()

        self.api_client.create_bundle.return_value = {
            "id": "new-bundle-123",
            "etag_header": "new-etag",
            "state": "DRAFT",
        }
        # Return different title to trigger metadata sync
        self.api_client.get_bundle.return_value = self._mock_api_bundle(title="Different Title", etag="new-etag")
        self.api_client.get_bundle_contents.return_value = {"items": []}
        self.api_client.add_content_to_bundle.return_value = {
            "id": "content-123",
            "etag_header": "content-etag",
            "metadata": {"dataset_id": "test-ns", "edition_id": "2024", "version_id": 1},
        }
        # Metadata sync fails
        self.api_client.update_bundle.side_effect = BundleAPIClientError("Metadata sync failed")
        self.api_client.delete_bundle.return_value = None

        with self.assertRaises(ValidationError):
            service.sync()

        self.api_client.delete_bundle.assert_called_once()

    def test_sync_handles_save_failure_after_bundle_creation(self):
        """Test sync handles exception when saving bundle metadata after API creation."""
        dataset = DatasetFactory(namespace="test-ns", edition="2024", version=1)
        BundleDatasetFactory(parent=self.bundle, dataset=dataset)

        self.bundle.bundle_api_bundle_id = ""
        service = self._create_service()

        self.api_client.create_bundle.return_value = {
            "id": "new-bundle-123",
            "etag_header": "new-etag",
        }
        self.api_client.delete_bundle.return_value = None

        # Use side_effect to fail on first save attempt (simpler than manual counting)
        self.bundle.save = Mock(side_effect=[RuntimeError("Database error during save"), None])

        with self.assertRaises(ValidationError) as context:
            service.sync()

        self.assertIn("Failed to save bundle API metadata", str(context.exception))
        self.api_client.delete_bundle.assert_called_once_with("new-bundle-123")

    def test_sync_wraps_non_validation_error_from_cleanup_delete(self):
        """Test sync wraps non-ValidationError from cleanup delete (covers line 429)."""
        dataset = DatasetFactory(namespace="test-ns", edition="2024", version=1)
        BundleDatasetFactory(parent=self.bundle, dataset=dataset)

        self.bundle.bundle_api_bundle_id = ""
        service = self._create_service()

        self.api_client.create_bundle.return_value = {
            "id": "new-bundle-123",
            "etag_header": "new-etag",
        }

        # First save fails, triggering cleanup delete
        self.bundle.save = Mock(side_effect=RuntimeError("Save failed"))

        # Delete raises BundleAPIClientError (not ValidationError) - should be wrapped
        self.api_client.delete_bundle.side_effect = BundleAPIClientError("Network error during cleanup")

        with self.assertRaises(ValidationError) as context:
            service.sync()

        self.assertIn("Failed to delete bundle from Bundle API", str(context.exception))
        self.api_client.delete_bundle.assert_called_once_with("new-bundle-123")

    def test_sync_handles_404_when_deleting_empty_bundle(self):
        """Test sync handles 404 gracefully when deleting non-existent bundle."""
        self._setup_bundle_with_api_id()

        # No datasets - should trigger deletion
        self.assertEqual(self.bundle.bundled_datasets.count(), 0)

        service = self._create_service()

        self.api_client.get_bundle.return_value = self._mock_api_bundle()
        # Bundle already deleted (404)
        self.api_client.delete_bundle.side_effect = BundleAPIClientError404("Not found")

        # Should complete successfully
        service.sync()

        self.api_client.delete_bundle.assert_called_once_with("bundle-123")
        # Bundle IDs should be cleared even though 404
        self.bundle.refresh_from_db()
        self.assertEqual(self.bundle.bundle_api_bundle_id, "")
        self.assertEqual(self.bundle.bundle_api_etag, "")

    def test_sync_reraises_validation_error_from_delete_operation(self):
        """Test sync re-raises ValidationError from delete without wrapping."""
        dataset = DatasetFactory(namespace="test-ns", edition="2024", version=1)
        BundleDatasetFactory(parent=self.bundle, dataset=dataset)

        self.bundle.bundle_api_bundle_id = ""
        service = self._create_service()

        self.api_client.create_bundle.return_value = {
            "id": "new-bundle-123",
            "etag_header": "new-etag",
        }

        # First save fails, triggering cleanup
        original_validation_error = ValidationError("Validation error from delete")
        self.bundle.save = Mock(side_effect=RuntimeError("Save failed"))
        self.api_client.delete_bundle.side_effect = original_validation_error

        with self.assertRaises(ValidationError) as context:
            service.sync()

        # Should re-raise the original ValidationError, not wrap it
        self.assertEqual(context.exception, original_validation_error)

    def test_sync_handles_missing_id_in_create_response(self):
        """Test sync raises ValidationError when API create response is missing 'id' field."""
        dataset = DatasetFactory(namespace="test-ns", edition="2024", version=1)
        BundleDatasetFactory(parent=self.bundle, dataset=dataset)

        self.bundle.bundle_api_bundle_id = ""
        service = self._create_service()

        self.api_client.create_bundle.return_value = {
            "etag_header": "new-etag",
            # Missing 'id' key - will trigger KeyError
        }

        with self.assertRaises(ValidationError) as context:
            service.sync()

        self.assertIn("Failed to create bundle in Bundle API", str(context.exception))

    def test_sync_with_approved_bundle_matching_state_skips_update(self):
        """Test sync with APPROVED bundle skips state update when already matching."""
        dataset = DatasetFactory(namespace="test-ns", edition="2024", version=1)
        BundleDatasetFactory(parent=self.bundle, dataset=dataset, bundle_api_content_id="content-123")

        self.bundle.status = BundleStatus.APPROVED
        self._setup_bundle_with_api_id()

        service = self._create_service()

        self.api_client.get_bundle.return_value = {
            "state": "APPROVED",
            "etag_header": "etag-123",
        }

        service.sync()

        self.api_client.update_bundle_state.assert_not_called()


@override_settings(DIS_DATASETS_BUNDLE_API_ENABLED=False)
class BundleAPISyncServiceDisabledTests(TestCase):
    """Tests for BundleAPISyncService when API is disabled."""

    def test_sync_does_nothing_when_api_disabled(self):
        """Test sync does nothing when Bundle API is disabled."""
        bundle = BundleFactory(name="Test Bundle", status=BundleStatus.DRAFT, bundle_api_bundle_id="abc")

        api_client = Mock(spec=BundleAPIClient)
        service = BundleAPISyncService(
            bundle=bundle,
            api_client=api_client,
            original_datasets=set(),
        )

        # When
        service.sync()

        # No API calls should be made
        api_client.create_bundle.assert_not_called()
        api_client.get_bundle.assert_not_called()
