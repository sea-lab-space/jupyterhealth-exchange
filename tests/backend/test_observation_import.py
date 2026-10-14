"""
Tests for POST /api/v1/patients/me/import: a patient storing a batch of their own past readings.
"""

import pytest
from rest_framework.test import APIClient

from core.models import CodeableConcept, Observation, ObservationIdentifier

HR = "omh:heart-rate:2.0"
STEPS = "omh:step-count:3.0"


def _heart_rate(key, value, at="2026-03-01T08:00:00-05:00", source="Apple Watch"):
    return {
        "key": key,
        "code": HR,
        "source": source,
        "body": {"heart_rate": {"value": value, "unit": "beats/min"}, "effective_time_frame": {"date_time": at}},
    }


def _steps(key, value, replace=True, source="Garmin"):
    return {
        "key": key,
        "code": STEPS,
        "source": source,
        "replace": replace,
        "body": {
            "step_count": {"value": value, "unit": "steps"},
            "effective_time_frame": {
                "time_interval": {
                    "start_date_time": "2026-03-01T00:00:00-05:00",
                    "end_date_time": "2026-03-02T00:00:00-05:00",
                }
            },
        },
    }


@pytest.fixture
def codes(db):
    for code in (HR, STEPS):
        CodeableConcept.objects.create(coding_system="https://w3id.org/openmhealth", coding_code=code, text=code)


@pytest.fixture
def patient_client(patient, codes):
    client = APIClient()
    client.default_format = "json"
    client.force_authenticate(patient.jhe_user)
    return client


def _import(client, records):
    return client.post("/api/v1/patients/me/import", {"records": records})


def test_stores_each_reading_under_its_source(patient_client, patient):
    response = _import(patient_client, [_heart_rate("a", 60), _heart_rate("b", 70, source="Garmin")])

    assert response.status_code == 200, response.content
    assert response.json()["created"] == 2
    stored = Observation.objects.filter(subject_patient=patient).select_related("data_source")
    assert sorted((o.data_source.name, o.omh_data["body"]["heart_rate"]["value"]) for o in stored) == [
        ("Apple Watch", 60),
        ("Garmin", 70),
    ]
    assert {o.effective_date_time.isoformat() for o in stored} == {"2026-03-01T13:00:00+00:00"}


def test_uploading_the_same_readings_again_adds_nothing(patient_client, patient):
    _import(patient_client, [_heart_rate("a", 60)])

    report = _import(patient_client, [_heart_rate("a", 60), _heart_rate("a", 60)]).json()

    assert (report["created"], report["duplicates"]) == (0, 2)
    assert Observation.objects.filter(subject_patient=patient).count() == 1


def test_a_replacing_record_updates_its_reading(patient_client, patient):
    """A day's total from an export taken mid-day is superseded by the full day."""
    _import(patient_client, [_steps("day", 4000)])

    report = _import(patient_client, [_steps("day", 9000)]).json()

    assert report["updated"] == 1
    obs = Observation.objects.get(subject_patient=patient)
    assert obs.omh_data["body"]["step_count"]["value"] == 9000


def test_a_record_not_marked_replace_keeps_what_is_stored(patient_client, patient):
    _import(patient_client, [_heart_rate("a", 60)])

    report = _import(patient_client, [{**_heart_rate("a", 99), "replace": False}]).json()

    assert (report["updated"], report["duplicates"]) == (0, 1)
    assert Observation.objects.get(subject_patient=patient).omh_data["body"]["heart_rate"]["value"] == 60


def test_invalid_records_are_reported_and_the_rest_stored(patient_client, patient):
    bad_body = _heart_rate("bad", 60)
    bad_body["body"]["heart_rate"]["unit"] = "furlongs"
    records = [
        _heart_rate("good", 60),
        bad_body,
        {**_heart_rate("unknown", 60), "code": "omh:not-a-measure:1.0"},
        {"code": HR, "body": {}},
    ]

    report = _import(patient_client, records).json()

    assert (report["created"], report["invalid"]) == (1, 3)
    assert [error["index"] for error in report["errors"]] == [1, 2, 3]
    assert Observation.objects.filter(subject_patient=patient).count() == 1


def test_two_patients_importing_the_same_key_each_keep_theirs(patient_client, patient, organization):
    from core.models import JheUser

    other = JheUser.objects.create_user(email="other@example.org", password="x", user_type="patient")
    other_client = APIClient()
    other_client.default_format = "json"
    other_client.force_authenticate(other)

    _import(patient_client, [_heart_rate("a", 60)])
    _import(other_client, [_heart_rate("a", 61)])

    assert Observation.objects.filter(subject_patient=patient).count() == 1
    assert Observation.objects.filter(subject_patient=other.patient).count() == 1
    assert ObservationIdentifier.objects.filter(system="jhe:import").count() == 2


def test_a_practitioner_cannot_import(api_client, codes):
    """There is no record of their own to import into."""
    response = _import(api_client, [_heart_rate("a", 60)])

    assert response.status_code == 403
    assert not Observation.objects.exists()


@pytest.mark.parametrize("body", [{}, {"records": []}, {"records": "nope"}])
def test_records_must_be_a_non_empty_list(patient_client, body):
    assert patient_client.post("/api/v1/patients/me/import", body).status_code == 400


def test_a_batch_is_capped(patient_client, monkeypatch):
    monkeypatch.setattr("core.views.patient.MAX_IMPORT_RECORDS", 2)

    response = _import(patient_client, [_heart_rate(str(n), 60) for n in range(3)])

    assert response.status_code == 400
