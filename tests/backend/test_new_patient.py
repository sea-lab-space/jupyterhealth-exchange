"""
The Patient record JHE makes with a patient user.
"""

from core.models import JheUser


def test_a_new_patient_has_no_birth_date(db):
    """Nobody has entered one yet. The day the account was made would read as a real birth date."""
    user = JheUser.objects.create_user(email="new@example.org", password="x", user_type="patient")

    assert user.patient.birth_date is None
