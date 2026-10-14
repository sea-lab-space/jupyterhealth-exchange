"""
Tests for `ow_link_patients`: copying patients' Open Wearables links from another service.
"""

from io import StringIO

import pytest
from django.core.management import call_command

from core.models import OW_USER_ID_SYSTEM, JheUser, PatientIdentifier

ISSUER = "https://signin.example.org"


@pytest.fixture
def signed_in(db):
    """Two patients known by the identifiers a sign-in service gave them."""
    patients = []
    for n in (1, 2):
        user = JheUser.objects.create_user(email=f"p{n}@example.org", password="x", user_type="patient")
        PatientIdentifier.objects.create(patient=user.patient, system=ISSUER, value=str(n))
        patients.append(user.patient)
    return patients


def _link(lines):
    out, err = StringIO(), StringIO()
    call_command("ow_link_patients", stdin=StringIO(lines), stdout=out, stderr=err)
    return out.getvalue(), err.getvalue()


def test_links_each_patient_to_their_ow_user(signed_in):
    out, err = _link(f"{ISSUER}|1 ow-a\n\n{ISSUER}|2 ow-b\n")

    assert [p.get_ow_user_id() for p in signed_in] == ["ow-a", "ow-b"]
    assert "Linked 2 patients" in out
    assert err == ""


def test_running_it_again_changes_nothing(signed_in):
    _link(f"{ISSUER}|1 ow-a\n")
    out, err = _link(f"{ISSUER}|1 ow-a\n")

    assert PatientIdentifier.objects.filter(system=OW_USER_ID_SYSTEM).count() == 1
    assert "Linked 0 patients" in out
    assert err == ""


def test_a_linked_patient_keeps_their_ow_user(signed_in):
    _link(f"{ISSUER}|1 ow-a\n")
    _out, err = _link(f"{ISSUER}|1 ow-other\n")

    assert signed_in[0].get_ow_user_id() == "ow-a"
    assert "linked to another OW user already" in err


def test_an_ow_user_is_never_linked_to_a_second_patient(signed_in):
    _link(f"{ISSUER}|1 ow-a\n")
    _out, err = _link(f"{ISSUER}|2 ow-a\n")

    assert signed_in[1].get_ow_user_id() is None
    assert "linked to another patient" in err


@pytest.mark.parametrize(
    "line, message",
    [
        (f"{ISSUER}|9 ow-a", "no patient has the identifier"),
        (f"{ISSUER}|1", "expected"),
        ("ow-a", "expected"),
        (f"{ISSUER}|1 ow-a extra", "expected"),
    ],
)
def test_a_line_it_cannot_apply_is_reported(signed_in, line, message):
    _out, err = _link(line + "\n")

    assert message in err
    assert not PatientIdentifier.objects.filter(system=OW_USER_ID_SYSTEM).exists()
