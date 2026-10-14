import time
from unittest.mock import MagicMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from core import auth
from core.models import Patient, PatientIdentifier, PractitionerIdentifier

ISS = "https://ehr.example.org/fhir"
AUD = "smart-client-id"
ID_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:id_token"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"
GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"

# Confidential "SoF EHR Launch" client used to authenticate to the exchange endpoint.
CLIENT_ID = "test-sof-client"
CLIENT_SECRET = "test-sof-secret"


@pytest.fixture
def rsa_private_pem():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem, key.public_key()


@pytest.fixture(autouse=True)
def trust_settings(settings, db):
    """Configure the token exchange via JheSettings (auth.sof.*), matching runtime."""
    from django.core.cache import cache

    from core.models import JheSetting

    settings.SITE_URL = "http://testserver"

    def _set(key, value_type, value):
        s, _ = JheSetting.objects.update_or_create(key=key, defaults={"value_type": value_type})
        s.set_value(value_type, value)
        s.save()
        cache.delete(f"jhe_setting:{key}")

    _set("auth.sof.trusted_issuers", "json", [ISS, "not-used"])
    _set("auth.sof.trusted_audience", "json", ["not-used", AUD])
    cache.delete("jhe_setting:site.url")


@pytest.fixture(autouse=True)
def sof_client(db):
    """The confidential client an external SMART app authenticates as."""
    from oauth2_provider.models import get_application_model

    app = get_application_model()(
        name="Test SoF EHR Launch",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,  # DOT hashes on save (hash_client_secret=True)
        client_type="confidential",
        authorization_grant_type="client-credentials",
        hash_client_secret=True,
        redirect_uris="",
        skip_authorization=True,
        algorithm="RS256",
    )
    app.save()
    return app


@pytest.fixture(autouse=True)
def patch_jwks(monkeypatch, rsa_private_pem):
    _, public_key = rsa_private_pem

    class _SigningKey:
        key = public_key

    class _FakeClient:
        def get_signing_key_from_jwt(self, token):
            return _SigningKey()

    monkeypatch.setattr(auth, "discover_jwks_uri", lambda issuer: "https://ehr.example.org/jwks")
    monkeypatch.setattr(auth, "_jwk_client", lambda jwks_uri: _FakeClient())


def make_token(
    priv_pem,
    *,
    iss=ISS,
    aud=AUD,
    fhir_user="Practitioner/test-practitioner",
    exp_delta=3600,
    alg="RS256",
    key=None,
    **extra_claims,
):
    now = int(time.time())
    claims = {"iss": iss, "aud": aud, "sub": "prac-1", "fhirUser": fhir_user, "iat": now, "exp": now + exp_delta}
    return jwt.encode({**claims, **extra_claims}, key or priv_pem, algorithm=alg)


def post(client, token, *, client_id=CLIENT_ID, client_secret=CLIENT_SECRET, http_authorization=None, **overrides):
    data = {
        "subject_token": token,
        "subject_token_type": ID_TOKEN_TYPE,
        "requested_token_type": ACCESS_TOKEN_TYPE,
        "audience": "http://testserver",
        "grant_type": GRANT,
        "scope": "openid",
    }
    # client_secret_post: client credentials in the form body.
    if client_id is not None:
        data["client_id"] = client_id
    if client_secret is not None:
        data["client_secret"] = client_secret
    data.update(overrides)
    extra = {"HTTP_AUTHORIZATION": http_authorization} if http_authorization else {}
    return client.post("/o/token-exchange", data=data, **extra)


@pytest.fixture
def user(user):
    """The conftest practitioner, mapped to the fhirUser id the tokens below carry."""
    PractitionerIdentifier.objects.create(practitioner=user.practitioner, system=ISS, value="test-practitioner")
    return user


def test_valid_id_token_issues_jhe_token(client, user, rsa_private_pem):
    priv, _ = rsa_private_pem
    r = post(client, make_token(priv))
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["access_token"]
    assert body["token_type"] == "Bearer"


def test_issued_token_linked_to_client_and_user(client, user, sof_client, rsa_private_pem):
    """The issued access token must be linked to the authenticated client and the
    resolved Practitioner (not an orphan token with application=NULL)."""
    from oauth2_provider.models import get_access_token_model

    priv, _ = rsa_private_pem
    r = post(client, make_token(priv))
    assert r.status_code == 200, r.content
    tok = get_access_token_model().objects.get(token=r.json()["access_token"])
    assert tok.application_id == sof_client.id
    assert tok.user_id == user.id


def test_missing_client_auth_unauthorized(client, user, rsa_private_pem):
    priv, _ = rsa_private_pem
    r = post(client, make_token(priv), client_id=None, client_secret=None)
    assert r.status_code == 401


def test_wrong_client_secret_unauthorized(client, user, rsa_private_pem):
    priv, _ = rsa_private_pem
    r = post(client, make_token(priv), client_secret="wrong-secret")
    assert r.status_code == 401


def test_http_basic_client_auth_accepted(client, user, rsa_private_pem):
    """Client credentials via HTTP Basic (client_secret_basic) must also work — both
    placements are documented. Regression: the oauthlib Request was built with an
    'Authorization' header key, but django-oauth-toolkit's _extract_basic_auth reads
    'HTTP_AUTHORIZATION', so Basic auth could never succeed."""
    import base64

    priv, _ = rsa_private_pem
    creds = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    r = post(
        client,
        make_token(priv),
        client_id=None,
        client_secret=None,
        http_authorization=f"Basic {creds}",
    )
    assert r.status_code == 200, r.content
    assert r.json()["access_token"]


def test_http_basic_wrong_secret_unauthorized(client, user, rsa_private_pem):
    import base64

    priv, _ = rsa_private_pem
    creds = base64.b64encode(f"{CLIENT_ID}:wrong-secret".encode()).decode()
    r = post(
        client,
        make_token(priv),
        client_id=None,
        client_secret=None,
        http_authorization=f"Basic {creds}",
    )
    assert r.status_code == 401


def test_trailing_slash_issuer_accepted(client, user, rsa_private_pem):
    """A token whose `iss` has a trailing slash (e.g. MedPlum's
    'https://api.medplum.com/') must still verify when the trusted issuer is
    configured without the slash. Regression: the expected issuer passed to
    signature verification must match the token's `iss` exactly, not a stripped form."""
    priv, _ = rsa_private_pem
    r = post(client, make_token(priv, iss=ISS + "/"))
    assert r.status_code == 200, r.content
    assert r.json()["access_token"]


def test_untrusted_issuer_forbidden(client, user, rsa_private_pem):
    priv, _ = rsa_private_pem
    r = post(client, make_token(priv, iss="https://evil.example"))
    assert r.status_code == 403


def test_expired_token_unauthorized(client, user, rsa_private_pem):
    priv, _ = rsa_private_pem  # expired beyond the clock-skew leeway
    r = post(client, make_token(priv, exp_delta=-3600))
    assert r.status_code == 401


def test_hs256_token_unauthorized(client, user, rsa_private_pem):
    priv, _ = rsa_private_pem
    r = post(client, make_token(priv, alg="HS256", key="shared-secret"))
    assert r.status_code == 401


def test_unknown_practitioner_not_found(client, db, rsa_private_pem):
    priv, _ = rsa_private_pem  # no `user` fixture -> identifier not in DB
    r = post(client, make_token(priv, fhir_user="Practitioner/ghost"))
    assert r.status_code == 404


def test_fhir_user_neither_practitioner_nor_patient_forbidden(client, user, rsa_private_pem):
    priv, _ = rsa_private_pem
    r = post(client, make_token(priv, fhir_user="RelatedPerson/test-practitioner"))
    assert r.status_code == 403


def test_patient_id_token_issues_a_token_for_the_patient(client, patient, sof_client, rsa_private_pem):
    from oauth2_provider.models import get_access_token_model

    PatientIdentifier.objects.create(patient=patient, system=ISS, value="test-patient")
    priv, _ = rsa_private_pem

    r = post(client, make_token(priv, fhir_user="Patient/test-patient"))

    assert r.status_code == 200, r.content
    tok = get_access_token_model().objects.get(token=r.json()["access_token"])
    assert tok.application_id == sof_client.id
    assert tok.user_id == patient.jhe_user_id


def test_practitioner_id_does_not_match_a_patient(client, user, rsa_private_pem):
    """The fhirUser type picks the table: a Patient claim never resolves to a practitioner's id."""
    priv, _ = rsa_private_pem
    r = post(client, make_token(priv, fhir_user="Patient/test-practitioner"))
    assert r.status_code == 404


def test_patient_without_a_jhe_login_not_found(client, organization, rsa_private_pem):
    patient = Patient.objects.create(name_family="No", name_given="Login")
    PatientIdentifier.objects.create(patient=patient, system=ISS, value="test-patient")
    priv, _ = rsa_private_pem

    r = post(client, make_token(priv, fhir_user="Patient/test-patient"))

    assert r.status_code == 404


@pytest.fixture
def provisioning(db):
    """Let ISS create the people its id_tokens name."""
    from django.core.cache import cache

    from core.models import JheSetting

    setting = JheSetting.objects.create(key="auth.sof.provisioning_issuers", value_type="json")
    setting.set_value("json", [ISS + "/"])
    setting.save()
    cache.delete("jhe_setting:auth.sof.provisioning_issuers")
    yield
    cache.delete("jhe_setting:auth.sof.provisioning_issuers")  # the rollback does not reach the cache


NEW_PERSON = {"email": "new@example.org", "given_name": "Ada", "family_name": "Lovelace"}


@pytest.mark.parametrize("resource_type", ["Practitioner", "Patient"])
def test_provisioning_issuer_creates_someone_new(client, provisioning, rsa_private_pem, resource_type):
    from oauth2_provider.models import get_access_token_model

    from core.models import JheUser

    priv, _ = rsa_private_pem
    token = make_token(priv, fhir_user=f"{resource_type}/new-1", **NEW_PERSON)

    first = post(client, token)
    again = post(client, token)

    assert first.status_code == 200, first.content
    assert again.status_code == 200, again.content
    user = JheUser.objects.get(email="new@example.org")
    assert (user.user_type, user.first_name, user.last_name) == (resource_type.lower(), "Ada", "Lovelace")
    assert not user.has_usable_password()
    profile = user.practitioner if resource_type == "Practitioner" else user.patient
    assert list(profile.identifiers.values_list("system", "value")) == [(ISS, "new-1")]
    tokens = get_access_token_model().objects.filter(
        token__in=[first.json()["access_token"], again.json()["access_token"]]
    )
    assert {t.user_id for t in tokens} == {user.id}


def test_only_provisioning_issuers_create_people(client, db, rsa_private_pem):
    from core.models import JheUser

    priv, _ = rsa_private_pem
    r = post(client, make_token(priv, fhir_user="Patient/new-1", **NEW_PERSON))

    assert r.status_code == 404
    assert not JheUser.objects.filter(email="new@example.org").exists()


def test_creating_someone_needs_an_email(client, provisioning, rsa_private_pem):
    priv, _ = rsa_private_pem
    r = post(client, make_token(priv, fhir_user="Patient/new-1"))

    assert r.status_code == 400
    assert not PatientIdentifier.objects.exists()


def test_creating_someone_never_takes_over_an_existing_email(client, provisioning, patient, rsa_private_pem):
    """Matching by email would let any provisioning issuer sign in as an existing user."""
    priv, _ = rsa_private_pem
    email = patient.jhe_user.email.upper()

    r = post(client, make_token(priv, fhir_user="Patient/new-1", email=email))

    assert r.status_code == 409
    assert not patient.identifiers.exists()


def test_same_id_from_another_trusted_issuer_not_found(client, user, rsa_private_pem):
    """Ids are scoped by issuer: another EHR asserting the same Practitioner id is someone else."""
    from django.core.cache import cache

    from core.models import JheSetting

    other_issuer = "https://other-ehr.example.org/fhir"
    setting = JheSetting.objects.get(key="auth.sof.trusted_issuers")
    setting.set_value("json", [ISS, other_issuer])
    setting.save()
    cache.delete("jhe_setting:auth.sof.trusted_issuers")
    priv, _ = rsa_private_pem

    r = post(client, make_token(priv, iss=other_issuer))

    assert r.status_code == 404


def test_identifier_stored_with_trailing_slash_matches(client, user, rsa_private_pem):
    user.practitioner.identifiers.update(system=ISS + "/")
    priv, _ = rsa_private_pem

    r = post(client, make_token(priv))

    assert r.status_code == 200, r.content


def test_absolute_fhir_user_url_matches_on_the_bare_id(client, user, rsa_private_pem):
    priv, _ = rsa_private_pem

    r = post(client, make_token(priv, fhir_user=f"{ISS}/Practitioner/test-practitioner"))

    assert r.status_code == 200, r.content


def test_ambiguous_identifier_not_found(client, user, rsa_private_pem, caplog):
    """Two practitioners whose rows differ only by a trailing slash cannot both be the caller."""
    from core.models import JheUser

    other = JheUser.objects.create_user(email="dupe@example.org", user_type="practitioner")
    PractitionerIdentifier.objects.create(practitioner=other.practitioner, system=ISS + "/", value="test-practitioner")
    priv, _ = rsa_private_pem

    r = post(client, make_token(priv))

    assert r.status_code == 404
    assert "Multiple practitioners hold identifier 'test-practitioner'" in caplog.text


@patch("core.views.ow.requests.post")
def test_linking_open_wearables_keeps_token_exchange_working(mock_post, client, user, rsa_private_pem):
    """Regression: linking OW used to overwrite the EHR id that token exchange matches on."""
    from django.core.cache import cache
    from rest_framework.test import APIClient

    from core.models import JheSetting, Patient

    for key, value in (("ow.api_url", "https://ow.example.com"), ("ow.api_key", "test-key")):
        JheSetting.objects.update_or_create(key=key, defaults={"value_type": "string", "value_string": value})
        cache.delete(f"jhe_setting:{key}")
    Patient.objects.create(jhe_user=user)
    mock_post.return_value = MagicMock(status_code=201)
    mock_post.return_value.json.return_value = {"id": "new-ow-user-id-123"}
    ow_client = APIClient()
    ow_client.force_authenticate(user)
    assert ow_client.post("/api/v1/ow/users").status_code == 200
    priv, _ = rsa_private_pem

    r = post(client, make_token(priv))

    assert r.status_code == 200, r.content


def test_audience_mismatch_bad_request(client, user, rsa_private_pem):
    """audience != SITE_URL must be rejected with HTTP 400."""
    priv, _ = rsa_private_pem
    r = post(client, make_token(priv), audience="https://wrong.example.org")
    assert r.status_code == 400


def test_same_practitioner_under_both_slash_forms_matches(client, user, rsa_private_pem):
    """One practitioner may hold the id under both spellings of the issuer (the copy strips the slash,
    the Medplum docs keep it); that is still one practitioner, not an ambiguous match."""
    PractitionerIdentifier.objects.create(practitioner=user.practitioner, system=ISS + "/", value="test-practitioner")
    priv, _ = rsa_private_pem

    r = post(client, make_token(priv))

    assert r.status_code == 200, r.content
