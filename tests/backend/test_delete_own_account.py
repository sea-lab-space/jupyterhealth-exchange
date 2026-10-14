"""DELETE /api/v1/users/profile: a person deletes their own account."""

from oauth2_provider.models import get_access_token_model
from rest_framework.test import APIClient

from core.models import JheUser, Observation, Patient, PatientIdentifier, Practitioner, PractitionerOrganization

from .test_oauth_client_credentials_userinfo import _issue_client_credentials_token, _practitioner_client_application
from .utils import Code, add_observations, add_patient_to_study, create_study

PROFILE_URL = "/api/v1/users/profile"


def _client(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def test_a_patient_deletes_their_account_and_readings(organization, patient):
    study = create_study(organization=organization, codes=[Code.HeartRate])
    add_patient_to_study(patient, study)
    add_observations(patient=patient, code=Code.HeartRate, n=2)
    assert Observation.objects.filter(subject_patient=patient).count() == 2
    PatientIdentifier.objects.create(patient=patient, system="https://app.example.org", value="25")
    user_id = patient.jhe_user_id

    r = _client(patient.jhe_user).delete(PROFILE_URL)

    assert r.status_code == 204, r.content
    assert not JheUser.objects.filter(id=user_id).exists()
    assert not Patient.objects.filter(id=patient.id).exists()
    assert not Observation.objects.filter(subject_patient_id=patient.id).exists()
    assert not PatientIdentifier.objects.exists()


def test_a_practitioner_deletes_their_account(user, organization):
    practitioner_id = user.practitioner.id

    r = _client(user).delete(PROFILE_URL)

    assert r.status_code == 204, r.content
    assert not JheUser.objects.filter(id=user.id).exists()
    assert not Practitioner.objects.filter(id=practitioner_id).exists()
    assert not PractitionerOrganization.objects.filter(organization=organization).exists()


def test_a_superuser_cannot_delete_their_own_account(superuser):
    r = _client(superuser).delete(PROFILE_URL)

    assert r.status_code == 403
    assert JheUser.objects.filter(id=superuser.id).exists()


def test_a_clients_token_cannot_delete_its_owner(user):
    """A client-credentials token resolves to the client's owner, but is not them."""
    client = APIClient()
    token = _issue_client_credentials_token(client, _practitioner_client_application(user))

    r = client.delete(PROFILE_URL, HTTP_AUTHORIZATION=f"Bearer {token}")

    assert r.status_code == 403
    assert JheUser.objects.filter(id=user.id).exists()


def test_the_persons_own_token_deletes_them(patient):
    """The path SeaCollect takes: a real bearer token issued to the person."""
    from datetime import timedelta

    from django.utils import timezone

    token = get_access_token_model().objects.create(
        user=patient.jhe_user, token="patient-token", expires=timezone.now() + timedelta(hours=1), scope="openid"
    )

    r = APIClient().delete(PROFILE_URL, HTTP_AUTHORIZATION=f"Bearer {token.token}")

    assert r.status_code == 204, r.content
    assert not JheUser.objects.filter(id=patient.jhe_user_id).exists()
