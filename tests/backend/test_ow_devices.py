"""
A participant's own Open Wearables account: unpairing phones (POST /api/v1/ow/pairings/revoke),
their sources and removing one with its readings (/api/v1/ow/sources), and stopping or deleting
their OW user (/api/v1/ow/users/me).
"""

import uuid
from unittest.mock import MagicMock, patch

import pytest
import requests
from django.core.cache import cache
from rest_framework.test import APIClient

from core.models import (
    OW_USER_ID_SYSTEM,
    DataSource,
    JheSetting,
    JheUser,
    Observation,
    ObservationIdentifier,
    PatientIdentifier,
)
from core.services.data_sources import provider_of

from .utils import Code, add_observations

OW_USER = "550e8400-e29b-41d4-a716-446655440000"
OW_USER_URL = f"https://ow.example.com/api/v1/users/{OW_USER}"
SETTINGS = ("ow.api_url", "ow.api_key")


@pytest.mark.parametrize(
    "label, provider",
    [
        ("Garmin", "garmin"),
        ("Google Health", "google_health"),
        ("WHOOP", "whoop"),
        ("Oura", "oura"),
        ("Apple Watch", "apple"),
        ("iPhone", "apple"),
        ("Bluetooth device", "apple"),
        ("Garmin via Apple Health", "apple"),
        ("Google Health via Apple Health", "apple"),
        ("Apple Health", "apple"),
        ("Garmin via Health Connect", "health_connect"),
        # an app Apple Health names that no rule knows
        ("MyFitnessPal", "apple"),
    ],
)
def test_provider_of(label, provider):
    assert provider_of(label) == provider


def _setting(key, value):
    row, _ = JheSetting.objects.update_or_create(key=key, defaults={"value_type": "string"})
    row.set_value("string", value)
    row.save()
    cache.delete(f"jhe_setting:{key}")


@pytest.fixture
def ow_settings(db):
    _setting("ow.api_url", "https://ow.example.com")
    _setting("ow.api_key", "sk-test-api-key-12345678")
    yield
    for key in SETTINGS:
        cache.delete(f"jhe_setting:{key}")


def readings(patient, label, n=2, system="jhe:import"):
    """``n`` readings filed under ``label``, each with the dedup identifier ingestion gives it."""
    add_observations(patient, Code.HeartRate, n)
    new = Observation.objects.filter(subject_patient=patient, data_source__isnull=True)
    for observation in new:
        ObservationIdentifier.objects.create(observation=observation, system=system, value=str(uuid.uuid4()))
    source = DataSource.objects.filter(name=label).first() or DataSource.objects.create(
        name=label, type="personal_device"
    )
    new.update(data_source=source)


def labels(patient):
    return sorted(set(Observation.objects.filter(subject_patient=patient).values_list("data_source__name", flat=True)))


def participant(email, ow_user=None):
    patient = JheUser.objects.create_user(email=email, password="testpass123", user_type="patient").patient
    if ow_user:
        PatientIdentifier.objects.create(patient=patient, system=OW_USER_ID_SYSTEM, value=ow_user)
    return patient


@pytest.fixture
def me(db):
    """A participant linked to OW, with readings from a phone, a Garmin account and a Garmin export."""
    patient = participant("me@example.org", OW_USER)
    readings(patient, "Apple Watch", system="ow:normalized")
    readings(patient, "iPhone", system="ow:normalized")
    readings(patient, "Garmin via Apple Health")
    readings(patient, "Garmin", system="ow:normalized")
    readings(patient, "Garmin")
    return patient


@pytest.fixture
def someone_else(db):
    patient = participant("else@example.org", "11111111-1111-1111-1111-111111111111")
    readings(patient, "Garmin")
    readings(patient, "Apple Watch")
    return patient


@pytest.fixture
def client(me):
    client = APIClient()
    client.force_authenticate(me.jhe_user)
    return client


def ow_answers(status_code, json=None):
    response = MagicMock(status_code=status_code, text="")
    response.json.return_value = json if json is not None else {}
    return response


class TestRevokePairings:
    URL = "/api/v1/ow/pairings/revoke"

    @patch("core.views.ow.requests.post", return_value=ow_answers(200, {"revoked": 2}))
    def test_unpairs_every_phone_of_the_caller(self, mock_post, client, ow_settings):
        resp = client.post(self.URL)
        assert (resp.status_code, resp.json()) == (200, {"revoked": 2})
        assert mock_post.call_args.args[0] == f"{OW_USER_URL}/tokens/revoke"

    @patch("core.views.ow.requests.post")
    def test_not_linked_has_nothing_to_unpair(self, mock_post, db, ow_settings):
        client = APIClient()
        client.force_authenticate(participant("new@example.org").jhe_user)
        assert client.post(self.URL).json() == {"revoked": 0}
        mock_post.assert_not_called()

    def test_practitioner_returns_403(self, api_client, ow_settings):
        assert api_client.post(self.URL).status_code == 403


class TestListSources:
    URL = "/api/v1/ow/sources"

    def test_groups_own_sources_by_provider(self, client, me, someone_else):
        resp = client.get(self.URL)
        assert resp.status_code == 200
        groups = {g["provider"]: g["names"] for g in resp.json()["sources"]}
        assert groups == {"apple": ["Apple Watch", "Garmin via Apple Health", "iPhone"], "garmin": ["Garmin"]}

    def test_practitioner_has_none(self, api_client):
        assert api_client.get(self.URL).json() == {"sources": []}

    def test_unauthenticated_returns_401(self):
        assert APIClient().get(self.URL).status_code == 401


class TestRemoveSource:
    @patch("core.views.ow.requests.post")
    @patch("core.views.ow.requests.delete", return_value=ow_answers(204))
    def test_cloud_account_goes_with_its_export(self, mock_delete, mock_post, client, me, someone_else, ow_settings):
        resp = client.delete("/api/v1/ow/sources/garmin")

        assert resp.status_code == 200
        assert resp.json() == {"deleted": 4}
        mock_delete.assert_called_once()
        assert mock_delete.call_args.args[0] == f"{OW_USER_URL}/connections/garmin/data"
        assert mock_delete.call_args.kwargs["headers"] == {"X-Open-Wearables-API-Key": "sk-test-api-key-12345678"}
        # a cloud account holds no phone pairing
        mock_post.assert_not_called()
        # the phone's relay of Garmin's app is the phone's
        assert labels(me) == ["Apple Watch", "Garmin via Apple Health", "iPhone"]
        assert labels(someone_else) == ["Apple Watch", "Garmin"]
        assert ObservationIdentifier.objects.count() == Observation.objects.count()

    @patch("core.views.ow.requests.post", return_value=ow_answers(200, {"revoked": 1}))
    @patch("core.views.ow.requests.delete", return_value=ow_answers(204))
    def test_phone_is_unpaired_before_its_data_goes(self, mock_delete, mock_post, client, me, ow_settings):
        calls = MagicMock()
        calls.attach_mock(mock_post, "post")
        calls.attach_mock(mock_delete, "delete")

        resp = client.delete("/api/v1/ow/sources/apple")

        assert resp.status_code == 200
        assert resp.json() == {"deleted": 6}
        assert [c[0] for c in calls.mock_calls if c[0] in ("post", "delete")] == ["post", "delete"]
        assert mock_post.call_args.args[0] == f"{OW_USER_URL}/tokens/revoke"
        assert mock_delete.call_args.args[0] == f"{OW_USER_URL}/connections/apple/data"
        assert labels(me) == ["Garmin"]

    @patch("core.views.ow.requests.delete", return_value=ow_answers(404))
    def test_never_connected_still_deletes_uploads(self, mock_delete, client, me, ow_settings):
        resp = client.delete("/api/v1/ow/sources/garmin")
        assert resp.status_code == 200
        assert labels(me) == ["Apple Watch", "Garmin via Apple Health", "iPhone"]

    @patch("core.views.ow.requests.delete")
    def test_not_linked_to_ow_deletes_uploads_only(self, mock_delete, db, ow_settings):
        patient = participant("uploads-only@example.org")
        readings(patient, "Garmin")
        client = APIClient()
        client.force_authenticate(patient.jhe_user)

        resp = client.delete("/api/v1/ow/sources/garmin")

        assert resp.json() == {"deleted": 2}
        mock_delete.assert_not_called()

    @patch("core.views.ow.requests.delete", return_value=ow_answers(500))
    def test_ow_failure_deletes_nothing(self, mock_delete, client, me, ow_settings):
        before = Observation.objects.count()
        resp = client.delete("/api/v1/ow/sources/garmin")
        assert resp.status_code == 502
        assert Observation.objects.count() == before

    @patch("core.views.ow.requests.delete")
    @patch("core.views.ow.requests.post", return_value=ow_answers(500))
    def test_failed_unpairing_deletes_nothing(self, mock_post, mock_delete, client, me, ow_settings):
        before = Observation.objects.count()
        resp = client.delete("/api/v1/ow/sources/apple")
        assert resp.status_code == 502
        mock_delete.assert_not_called()
        assert Observation.objects.count() == before

    @patch("core.views.ow.requests.delete", side_effect=requests.ConnectionError("down"))
    def test_ow_unreachable_returns_502(self, mock_delete, client, me, ow_settings):
        before = Observation.objects.count()
        assert client.delete("/api/v1/ow/sources/garmin").status_code == 502
        assert Observation.objects.count() == before

    def test_missing_ow_settings_returns_500(self, client, db):
        _setting("ow.api_url", "")
        _setting("ow.api_key", "")
        assert client.delete("/api/v1/ow/sources/garmin").status_code == 500

    def test_unknown_provider_returns_400(self, client, ow_settings):
        assert client.delete("/api/v1/ow/sources/myspace").status_code == 400

    def test_practitioner_returns_403(self, api_client, ow_settings):
        assert api_client.delete("/api/v1/ow/sources/garmin").status_code == 403

    def test_unauthenticated_returns_401(self):
        assert APIClient().delete("/api/v1/ow/sources/garmin").status_code == 401


class TestStopCollecting:
    URL = "/api/v1/ow/users/me/stop"

    @patch("core.views.ow.requests.post", return_value=ow_answers(200, {"revoked": 1}))
    @patch("core.views.ow.requests.get", return_value=ow_answers(200, [{"provider": "garmin"}, {"provider": "oura"}]))
    @patch("core.views.ow.requests.delete", return_value=ow_answers(204))
    def test_unpairs_and_disconnects_and_keeps_readings(
        self, mock_delete, mock_get, mock_post, client, me, ow_settings
    ):
        before = Observation.objects.filter(subject_patient=me).count()

        assert client.post(self.URL).status_code == 204

        assert mock_post.call_args.args[0] == f"{OW_USER_URL}/tokens/revoke"
        assert [c.args[0] for c in mock_delete.call_args_list] == [
            f"{OW_USER_URL}/connections/garmin",
            f"{OW_USER_URL}/connections/oura",
        ]
        assert Observation.objects.filter(subject_patient=me).count() == before
        assert me.get_ow_user_id() == OW_USER

    def test_practitioner_returns_403(self, api_client, ow_settings):
        assert api_client.post(self.URL).status_code == 403


class TestDeleteOwUser:
    URL = "/api/v1/ow/users/me"

    @patch("core.views.ow.requests.delete", return_value=ow_answers(200))
    def test_deletes_the_ow_user_and_the_link(self, mock_delete, client, me, ow_settings):
        assert client.delete(self.URL).status_code == 204
        assert mock_delete.call_args.args[0] == OW_USER_URL
        assert me.get_ow_user_id() is None

    @patch("core.views.ow.requests.delete", return_value=ow_answers(500))
    def test_ow_failure_keeps_the_link(self, mock_delete, client, me, ow_settings):
        assert client.delete(self.URL).status_code == 502
        assert me.get_ow_user_id() == OW_USER

    @patch("core.views.ow.requests.delete", return_value=ow_answers(404))
    def test_an_ow_user_already_gone_is_done(self, mock_delete, client, me, ow_settings):
        assert client.delete(self.URL).status_code == 204

    def test_practitioner_returns_403(self, api_client, ow_settings):
        assert api_client.delete(self.URL).status_code == 403
