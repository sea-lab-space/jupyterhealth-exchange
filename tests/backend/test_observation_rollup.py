"""
GET /api/v1/patients/{id}/daily, /series and /deliveries: readings rolled up in the database.
"""

import time
from datetime import UTC, datetime, timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from core.fhir.effective_time_frame import extract_effective_time_frame
from core.models import CodeableConcept, DataSource, JheUser, Observation, Organization

from .utils import add_patient_to_study, create_study

HR = "omh:heart-rate:2.0"
STEPS = "omh:step-count:3.0"
SLEEP = "ieee:total-sleep-time:1.0"
WEIGHT = "omh:body-weight:3.0"
GLUCOSE = "omh:blood-glucose:4.0"
BP = "omh:blood-pressure:4.0"
CODES = [HR, STEPS, SLEEP, WEIGHT, GLUCOSE, BP]


@pytest.fixture
def concepts(db):
    return {
        code: CodeableConcept.objects.get_or_create(
            coding_system="https://w3id.org/ieee1752" if code.startswith("ieee") else "https://w3id.org/openmhealth",
            coding_code=code,
            defaults={"text": code},
        )[0]
        for code in CODES
    }


def store(patient, concepts, code, source, body):
    omh = {"header": {}, "body": body}
    dt, start, end = extract_effective_time_frame(omh)
    return Observation(
        subject_patient=patient,
        codeable_concept=concepts[code],
        data_source=DataSource.objects.get_or_create(name=source, defaults={"type": "personal_device"})[0],
        omh_data=omh,
        effective_date_time=dt,
        effective_period_start=start,
        effective_period_end=end,
    )


def hr(bpm, at):
    return {"heart_rate": {"value": bpm, "unit": "beats/min"}, "effective_time_frame": {"date_time": at}}


def interval(start, end):
    return {"effective_time_frame": {"time_interval": {"start_date_time": start, "end_date_time": end}}}


@pytest.fixture
def history(patient, concepts):
    rows = [
        # 23:30 in Los Angeles on Jan 1 is Jan 2 in UTC; it belongs to Jan 1
        (HR, "Apple Watch", hr(60, "2024-01-01T08:00:00-08:00")),
        (HR, "Apple Watch", hr(80, "2024-01-01T23:30:00-08:00")),
        (HR, "Garmin", hr(66, "2024-01-01T12:00:00-08:00")),
        (HR, "Apple Watch", hr(70, "2024-01-02T08:00:00-08:00")),
        (
            STEPS,
            "iPhone",
            {
                "step_count": {"value": 8000, "unit": "steps"},
                **interval("2024-01-01T00:00:00-08:00", "2024-01-02T00:00:00-08:00"),
            },
        ),
        (
            STEPS,
            "Apple Watch",
            {
                "step_count": {"value": 9000, "unit": "steps"},
                **interval("2024-01-01T00:00:00-08:00", "2024-01-02T00:00:00-08:00"),
            },
        ),
        # a night is dated by the morning it ends
        (
            SLEEP,
            "Apple Watch",
            {
                "total_sleep_time": {"value": 25200, "unit": "sec"},
                **interval("2024-01-01T23:00:00-08:00", "2024-01-02T06:30:00-08:00"),
            },
        ),
        (
            WEIGHT,
            "Withings",
            {
                "body_weight": {"value": 160, "unit": "lb"},
                "effective_time_frame": {"date_time": "2024-01-02T07:00:00-08:00"},
            },
        ),
        (
            GLUCOSE,
            "Dexcom",
            {
                "blood_glucose": {"value": 5.5, "unit": "mmol/L"},
                "effective_time_frame": {"date_time": "2024-01-02T07:00:00-08:00"},
            },
        ),
        (
            BP,
            "Omron",
            {
                "systolic_blood_pressure": {"value": 120, "unit": "mmHg"},
                "diastolic_blood_pressure": {"value": 80, "unit": "mmHg"},
                "effective_time_frame": {"date_time": "2024-01-02T07:30:00-08:00"},
            },
        ),
    ]
    Observation.objects.bulk_create([store(patient, concepts, code, source, body) for code, source, body in rows])
    return patient


def get(user, patient, what, **params):
    client = APIClient()
    client.force_authenticate(user)
    return client.get(f"/api/v1/patients/{patient.id}/{what}", params)


def daily(user, patient, **params):
    return get(user, patient, "daily", **params)


def rows_by(report):
    return {(r["date"], r["code"], r["source"]): r for r in report["days"]}


def test_each_source_is_its_own_row_on_its_own_local_day(history):
    rows = rows_by(daily(history.jhe_user, history, start="2024-01-01", end="2024-01-02").json())
    watch = rows["2024-01-01", HR, "Apple Watch"]
    assert (watch["count"], watch["avg"], watch["min"], watch["max"]) == (2, 70, 60, 80)
    assert rows["2024-01-01", HR, "Garmin"]["avg"] == 66
    assert rows["2024-01-02", HR, "Apple Watch"]["count"] == 1
    assert rows["2024-01-01", STEPS, "iPhone"]["sum"] == 8000
    assert rows["2024-01-01", STEPS, "Apple Watch"]["sum"] == 9000


def test_a_days_total_that_arrives_twice_is_counted_once(patient, concepts):
    def steps(value, start, end):
        return {"step_count": {"value": value, "unit": "steps"}, **interval(start, end)}

    day = ("2024-01-01T00:00:00-08:00", "2024-01-02T00:00:00-08:00")
    rows = [
        # the watch's day, synced live and again from an uploaded export
        (STEPS, "Garmin", steps(8000, *day)),
        (STEPS, "Garmin", steps(8012, *day)),
        # a writer that sends the day in pieces: those add up
        (STEPS, "Pedometer", steps(3000, "2024-01-01T08:00:00-08:00", "2024-01-01T09:00:00-08:00")),
        (STEPS, "Pedometer", steps(4000, "2024-01-01T17:00:00-08:00", "2024-01-01T18:00:00-08:00")),
    ]
    Observation.objects.bulk_create([store(patient, concepts, code, source, body) for code, source, body in rows])
    report = rows_by(daily(patient.jhe_user, patient, start="2024-01-01", end="2024-01-01").json())
    assert report["2024-01-01", STEPS, "Garmin"]["sum"] == 8012
    assert report["2024-01-01", STEPS, "Pedometer"]["sum"] == 7000


def test_values_come_back_in_one_unit_per_measure(history):
    rows = rows_by(daily(history.jhe_user, history, start="2024-01-01", end="2024-01-02").json())
    assert rows["2024-01-02", SLEEP, "Apple Watch"]["avg"] == 7
    assert round(rows["2024-01-02", WEIGHT, "Withings"]["avg"], 2) == 72.57
    assert round(rows["2024-01-02", GLUCOSE, "Dexcom"]["avg"]) == 99
    bp = rows["2024-01-02", BP, "Omron"]
    assert (bp["avg"], bp["avg2"]) == (120, 80)


def test_the_default_window_ends_at_the_newest_reading(history):
    report = daily(history.jhe_user, history, days=1).json()
    assert (report["start"], report["end"]) == ("2024-01-02", "2024-01-02")
    assert {r["date"] for r in report["days"]} == {"2024-01-02"}


def test_sources_cover_the_whole_history_and_latest_is_each_sources_own(history):
    report = daily(history.jhe_user, history, days=1).json()
    sources = {(s["code"], s["source"]): s["count"] for s in report["sources"]}
    assert sources[HR, "Apple Watch"] == 3 and sources[HR, "Garmin"] == 1
    latest = {(item["code"], item["source"]): item for item in report["latest"]}
    assert len(latest) == len(report["latest"])
    assert latest[HR, "Apple Watch"]["value"] == 70 and latest[HR, "Garmin"]["value"] == 66
    assert latest[STEPS, "iPhone"]["value"] == 8000 and latest[STEPS, "Apple Watch"]["value"] == 9000
    assert (latest[BP, "Omron"]["value"], latest[BP, "Omron"]["value2"]) == (120, 80)


def test_codes_limit_the_measures(history):
    report = daily(history.jhe_user, history, days=2, codes=f"{HR},{BP}").json()
    assert {r["code"] for r in report["days"]} == {HR, BP}
    assert {s["code"] for s in report["sources"]} == {HR, BP}


def test_a_practitioner_in_the_patients_organization_reads_everything(user, history):
    report = daily(user, history, start="2024-01-01", end="2024-01-02").json()
    assert {r["code"] for r in report["days"]} == set(CODES)


def test_a_study_narrows_it_to_what_the_study_asks_for(user, organization, history):
    study = create_study(organization=organization, codes=[HR])
    add_patient_to_study(history, study, consent=False)
    report = daily(user, history, start="2024-01-01", end="2024-01-02", study_id=study.id).json()
    assert {r["code"] for r in report["days"]} == {HR}
    assert {s["code"] for s in report["sources"]} == {HR}


def test_a_study_the_patient_is_not_in_shows_nothing(user, organization, history):
    study = create_study(organization=organization, codes=[HR])
    assert daily(user, history, days=7, study_id=study.id).json()["days"] == []


def test_nobody_else_can_read_it(history):
    stranger = JheUser.objects.create_user(email="stranger@example.org", password="x", user_type="patient")
    assert daily(stranger, history, days=7).status_code == 403
    outsider = JheUser.objects.create_user(email="outsider@example.org", password="x", user_type="practitioner")
    assert daily(outsider, history, days=7).status_code == 403
    assert get(outsider, history, "deliveries").status_code == 403


def test_a_study_of_another_organization_is_refused(user, history):
    elsewhere = create_study(organization=Organization.objects.create(name="Elsewhere", type="other"), codes=[HR])
    assert daily(user, history, days=7, study_id=elsewhere.id).status_code == 403


def test_bad_parameters_are_a_400(history):
    assert daily(history.jhe_user, history, days=0).status_code == 400
    assert daily(history.jhe_user, history, start="2024-13-01", end="2024-01-02").status_code == 400


def test_six_months_of_dense_heart_rate_rolls_up_quickly(patient, concepts):
    start = datetime(2024, 1, 1, tzinfo=UTC)
    watch = DataSource.objects.get_or_create(name="Apple Watch", defaults={"type": "personal_device"})[0]
    batch = []
    for i in range(50_000):
        at = (start + timedelta(minutes=5 * i)).isoformat()
        batch.append(
            Observation(
                subject_patient=patient,
                codeable_concept=concepts[HR],
                data_source=watch,
                omh_data={"header": {}, "body": hr(60 + i % 40, at)},
                effective_date_time=start + timedelta(minutes=5 * i),
            )
        )
    Observation.objects.bulk_create(batch, batch_size=5000)
    started = time.perf_counter()
    report = daily(patient.jhe_user, patient, days=180).json()
    elapsed = time.perf_counter() - started
    assert sum(r["count"] for r in report["days"]) == 50_000
    assert elapsed < 5


def series(user, patient, **params):
    return get(user, patient, "series", **params)


@pytest.fixture
def dense(patient, concepts):
    start = datetime(2024, 3, 1, tzinfo=UTC)
    rows = [(HR, "Garmin", hr(60 + i % 3, (start + timedelta(minutes=i)).isoformat())) for i in range(60)]
    rows += [(HR, "Apple Watch", hr(70, (start + timedelta(minutes=5 * i)).isoformat())) for i in range(12)]
    rows += [
        (
            STEPS,
            "Garmin",
            {"step_count": {"value": 500, "unit": "steps"}, **interval("2024-03-01T00:00:00Z", "2024-03-01T01:00:00Z")},
        )
    ]
    Observation.objects.bulk_create([store(patient, concepts, code, source, body) for code, source, body in rows])
    return patient


def test_dense_readings_come_back_averaged_per_interval_and_source(dense):
    report = series(dense.jhe_user, dense, code=HR, start="2024-03-01", end="2024-03-02", minutes=15).json()
    points = {(p["t"], p["source"]): p for p in report["points"]}
    assert len(points) == 8  # four quarter hours, two sources, steps left out
    first = points["2024-03-01T00:00:00Z", "Garmin"]
    assert (first["count"], first["min"], first["max"], first["avg"]) == (15, 60, 62, 61)
    assert points["2024-03-01T00:45:00Z", "Apple Watch"]["count"] == 3


def test_a_study_narrows_a_series_too(user, organization, dense):
    study = create_study(organization=organization, codes=[STEPS])
    add_patient_to_study(dense, study)
    narrowed = {"start": "2024-03-01", "end": "2024-03-02", "study_id": study.id}
    assert series(user, dense, code=HR, **narrowed).json()["points"] == []
    assert len(series(user, dense, code=STEPS, **narrowed).json()["points"]) == 1
    assert len(series(user, dense, code=HR, start="2024-03-01", end="2024-03-02").json()["points"]) == 8


def test_a_series_needs_a_code_a_start_and_a_sensible_interval(dense):
    owner = dense.jhe_user
    assert series(owner, dense, start="2024-03-01").status_code == 400
    assert series(owner, dense, code=HR).status_code == 400
    assert series(owner, dense, code=HR, start="2024-03-01", minutes=0).status_code == 400
    assert series(owner, dense, code=HR, start="2024-03-02", end="2024-03-01").status_code == 400
    assert series(owner, dense, code=HR, start="2020-01-01", end="2024-01-01", minutes=1).status_code == 400
    stranger = JheUser.objects.create_user(email="stranger@example.org", password="x", user_type="patient")
    assert series(stranger, dense, code=HR, start="2024-03-01").status_code == 403


def test_deliveries_count_what_arrived_and_when_the_newest_did(user, history):
    newest = Observation.objects.filter(subject_patient=history).order_by("-id").first()
    arrived = timezone.now() + timedelta(minutes=5)
    Observation.objects.filter(id=newest.id).update(created=arrived)
    # A revision moves last_updated, not the arrival.
    Observation.objects.filter(subject_patient=history).exclude(id=newest.id).update(last_updated=arrived)

    report = get(user, history, "deliveries").json()
    assert report["count"] == 10
    assert datetime.fromisoformat(report["lastDelivered"]) == arrived
    assert get(user, history, "deliveries", codes=HR).json()["count"] == 4
