from django.apps import apps
from django.http import HttpResponse
from django.test import TestCase
from django.urls import reverse
from modelcluster.models import get_all_child_relations
from wagtail.fields import StreamField
from wagtail.test.utils import WagtailTestUtils
from wagtail.test.utils.form_data import inline_formset, nested_form_data, streamfield

from cms.articles.tests.factories import ArticleSeriesPageFactory
from cms.home.models import HomePage
from cms.methodology.tests.factories import MethodologyIndexPageFactory
from cms.release_calendar.enums import ReleaseStatus
from cms.release_calendar.models import ReleaseCalendarIndex
from cms.standard_pages.tests.factories import IndexPageFactory


class NewPagePreviewTestCase(WagtailTestUtils, TestCase):
    """Checks that previewing an unsaved page does not crash in the page's admin form.

    On the add-page preview the page instance has no locale or pk yet, so form validation
    must not rely on attributes that only exist once the page is saved.
    """

    @classmethod
    def setUpTestData(cls) -> None:
        cls.superuser = cls.create_superuser(username="admin")

    def setUp(self) -> None:
        self.client.force_login(self.superuser)

    def preview_new_page(self, app_label: str, model_name: str, parent_pk: int, data: dict) -> HttpResponse:
        model = apps.get_model(app_label, model_name)

        # Empty StreamFields and inline panels still need their management form data
        form_data: dict = {}
        for field in model._meta.get_fields():
            if isinstance(field, StreamField):
                form_data[field.name] = streamfield([])
        for relation in get_all_child_relations(model):
            form_data[relation.get_accessor_name()] = inline_formset([])
        form_data.update(data)

        url = reverse("wagtailadmin_pages:preview_on_add", args=[app_label, model_name, parent_pk])
        return self.client.post(url, nested_form_data(form_data))

    def test_preview_on_add__pages_with_custom_admin_forms(self) -> None:
        """Every page type with a custom base_form_class can be previewed before it is saved."""
        cases = [
            ("standard_pages", "informationpage", IndexPageFactory().pk),
            ("methodology", "methodologypage", MethodologyIndexPageFactory().pk),
            ("articles", "statisticalarticlepage", ArticleSeriesPageFactory().pk),
            ("topics", "topicpage", HomePage.objects.first().pk),
            ("release_calendar", "releasecalendarpage", ReleaseCalendarIndex.objects.first().pk),
        ]

        for app_label, model_name, parent_pk in cases:
            with self.subTest(page_type=f"{app_label}.{model_name}"):
                response = self.preview_new_page(
                    app_label, model_name, parent_pk, {"title": "New page", "slug": "new-page"}
                )

                self.assertEqual(response.status_code, 200)

    def test_preview_on_add__release_calendar_with_release_date_text(self) -> None:
        """Previewing a new provisional release calendar page with a release date text does not crash."""
        parent_pk = ReleaseCalendarIndex.objects.first().pk

        response = self.preview_new_page(
            "release_calendar",
            "releasecalendarpage",
            parent_pk,
            {
                "title": "New release",
                "slug": "new-release",
                "status": ReleaseStatus.PROVISIONAL,
                "release_date_text": "November 2024",
            },
        )

        self.assertEqual(response.status_code, 200)

    def test_preview_on_add__release_calendar_with_next_release_date_text(self) -> None:
        """Previewing a new release calendar page with a next release date text does not crash."""
        parent_pk = ReleaseCalendarIndex.objects.first().pk

        response = self.preview_new_page(
            "release_calendar",
            "releasecalendarpage",
            parent_pk,
            {
                "title": "New release",
                "slug": "new-release",
                "status": ReleaseStatus.PROVISIONAL,
                "next_release_date_text": "To be confirmed",
            },
        )

        self.assertEqual(response.status_code, 200)
