"""
`ow_poll` files each reading under the device or app that recorded it, and totals
steps per source and local day and sleep per source and night.
"""

from datetime import datetime, timedelta
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.cache import cache
from django.core.management import call_command

from core.management.commands.ow_poll import OW_TYPE_TO_CODE, STEP_COUNT_CODE, TOTAL_SLEEP_TIME_CODE
from core.models import OW_USER_ID_SYSTEM, DataSource, JheSetting, Observation, ObservationIdentifier, PatientIdentifier

from .utils import add_patient_to_study, create_study

NOW = datetime.fromisoformat("2026-10-12T12:00:00+00:00")

# What OW 0.9 reports for one person's iPhone, Apple Watch and two apps writing through them.
WATCH = {"provider": "apple", "source": "Jan’s Apple Watch", "device": "Watch6,17", "device_type": "watch"}
WATCH_APP = {"provider": "apple", "source": "HRVEmotionPrototype", "device": "Watch6,17", "device_type": "watch"}
PHONE = {"provider": "apple", "source": "JAN", "device": "iPhone16,2", "device_type": "phone"}
RELAYED = {"provider": "apple", "source": "Google Health", "device": "iPhone16,2", "device_type": "phone"}


def _set_jhe_setting(key, value, value_type):
    setting, _ = JheSetting.objects.update_or_create(key=key, defaults={"value_type": value_type})
    setting.set_value(value_type, value)
    setting.save()
    cache.delete(f"jhe_setting:{key}")


def _at(value):
    return datetime.fromisoformat(value)


class FakeOW:
    """OW's read API over a list of samples and sleep sessions, windowed the way OW windows them."""

    def __init__(self):
        self.samples = []
        self.sessions = []

    def sample(self, series, timestamp, value, source, zone_offset="-04:00", is_daily_total=None, stored=None):
        """A sample OW stored at ``stored``, by default just before NOW."""
        self.samples.append(
            {
                "timestamp": timestamp,
                "zone_offset": zone_offset,
                "type": series,
                "value": value,
                "source": source,
                "is_daily_total": is_daily_total,
                "_stored": stored or NOW - timedelta(minutes=1),
            }
        )

    def session(self, start, end, asleep_hours, source, is_nap=False, zone_offset="-04:00"):
        self.sessions.append(
            {
                "id": f"session-{len(self.sessions)}",
                "start_time": start,
                "end_time": end,
                "zone_offset": zone_offset,
                "source": source,
                "duration_seconds": int((_at(end) - _at(start)).total_seconds()),
                "sleep_duration_seconds": int(asleep_hours * 3600),
                "time_in_bed_seconds": int((_at(end) - _at(start)).total_seconds()),
                "stages": None,
                "is_nap": is_nap,
            }
        )

    def get(self, url, params=None, **kwargs):
        path = url.split("/users/user-123/", 1)[1]
        if path == "timeseries":
            low, high = _at(params["start_time"]), _at(params["end_time"])
            stored_after = _at(params["created_after"]) if "created_after" in params else None
            data = [
                {k: v for k, v in s.items() if k != "_stored"}
                for s in self.samples
                if s["type"] == params["types"]
                and low <= _at(s["timestamp"]) < high
                and (stored_after is None or s["_stored"] > stored_after)
            ]
        elif path == "events/sleep":
            low, high = _at(params["start_date"]), _at(params["end_date"])
            data = [s for s in self.sessions if _at(s["start_time"]) >= low and _at(s["end_time"]) < high]
        else:
            data = []
        response = MagicMock(status_code=200)
        response.json.return_value = {"data": data, "pagination": {"has_more": False}}
        return response


@pytest.fixture
def ow(db, organization, patient):
    """A patient linked to OW and consented to everything ow_poll fills."""
    study = create_study(organization=organization, codes=sorted(set(OW_TYPE_TO_CODE.values())))
    add_patient_to_study(patient=patient, study=study)
    PatientIdentifier.objects.create(patient=patient, system=OW_USER_ID_SYSTEM, value="user-123")
    cache.clear()
    _set_jhe_setting("module.ow", True, "bool")
    _set_jhe_setting("ow.sync_in_progress", "", "string")
    _set_jhe_setting("ow.api_url", "https://ow.example.test", "string")
    _set_jhe_setting("ow.api_key", "test-key", "string")
    _set_jhe_setting("ow.poll_window_days", 7, "int")
    yield FakeOW()
    cache.clear()


def _poll(ow, now=NOW):
    with (
        patch("core.management.commands.ow_poll.requests.get", side_effect=ow.get),
        patch("core.management.commands.ow_poll.timezone.now", return_value=now),
    ):
        call_command("ow_poll", stdout=StringIO())


def _readings(code):
    return Observation.objects.filter(codeable_concept__coding_code=code).select_related("data_source")


def _totals(code):
    """{(source, key's day): value} for every total stored under ``code``."""
    field = "step_count" if code == STEP_COUNT_CODE else "total_sleep_time"
    return {
        (obs.data_source.name, obs.identifiers.get().value.rsplit(":", 1)[1]): obs.omh_data["body"][field]["value"]
        for obs in _readings(code)
    }


# Sources


def test_each_reading_is_filed_under_its_source(ow):
    ow.sample("heart_rate", "2026-10-12T10:00:00Z", 60, WATCH)
    ow.sample("heart_rate", "2026-10-12T10:01:00Z", 70, PHONE)
    ow.sample("heart_rate", "2026-10-12T10:02:00Z", 80, RELAYED)

    _poll(ow)

    names = {obs.data_source.name for obs in _readings("omh:heart-rate:2.0")}
    assert names == {"Apple Watch", "iPhone", "Google Health via Apple Health"}


def test_a_source_is_one_data_source_however_many_readings(ow):
    ow.sample("heart_rate", "2026-10-12T10:00:00Z", 60, WATCH)
    ow.sample("heart_rate", "2026-10-12T10:01:00Z", 61, WATCH)
    _poll(ow)
    ow.sample("heart_rate", "2026-10-12T11:00:00Z", 62, WATCH)
    _poll(ow)

    assert DataSource.objects.filter(name="Apple Watch").count() == 1
    assert _readings("omh:heart-rate:2.0").filter(data_source__name="Apple Watch").count() == 3


def test_two_sources_measuring_at_one_instant_are_both_kept(ow):
    """OW keeps one sample per data source, series and instant, and so does the poll."""
    ow.sample("heart_rate", "2026-10-12T10:00:00Z", 60, WATCH)
    ow.sample("heart_rate", "2026-10-12T10:00:00Z", 62, WATCH_APP)
    ow.sample("heart_rate", "2026-10-12T10:00:00Z", 64, RELAYED)

    _poll(ow)

    values = sorted(obs.omh_data["body"]["heart_rate"]["value"] for obs in _readings("omh:heart-rate:2.0"))
    assert values == [60, 62, 64]


def test_no_name_of_a_person_is_stored_with_a_reading(ow):
    """HealthKit names a person's devices after them; neither the source nor the key may carry it."""
    ow.sample("heart_rate", "2026-10-12T10:00:00Z", 60, WATCH)
    ow.sample("heart_rate", "2026-10-12T10:01:00Z", 70, PHONE)
    ow.sample("steps", "2026-10-12T10:00:00Z", 100, PHONE)

    _poll(ow)

    assert not DataSource.objects.filter(name__icontains="jan").exists()
    assert not ObservationIdentifier.objects.filter(value__icontains="jan").exists()


def test_a_reading_keeps_the_offset_it_was_taken_in(ow):
    ow.sample("heart_rate", "2026-10-12T02:30:00Z", 60, WATCH, zone_offset="-04:00")

    _poll(ow)

    obs = _readings("omh:heart-rate:2.0").get()
    assert obs.omh_data["body"]["effective_time_frame"] == {"date_time": "2026-10-11T22:30:00-04:00"}
    assert obs.effective_date_time == _at("2026-10-12T02:30:00+00:00")


def test_body_temperature_is_polled(ow):
    ow.sample("body_temperature", "2026-10-12T10:00:00Z", 36.6, WATCH)

    _poll(ow)

    obs = _readings("omh:body-temperature:4.0").get()
    assert obs.omh_data["body"]["body_temperature"] == {"value": 36.6, "unit": "C"}
    assert obs.data_source.name == "Apple Watch"


# Daily steps


def test_steps_are_totalled_per_source_and_local_day(ow):
    ow.sample("steps", "2026-10-11T02:00:00Z", 100, WATCH)  # 22:00 on the 10th, at -04:00
    ow.sample("steps", "2026-10-11T14:00:00Z", 200, WATCH)
    ow.sample("steps", "2026-10-11T15:00:00Z", 300, WATCH)
    ow.sample("steps", "2026-10-11T14:00:00Z", 1000, PHONE)

    _poll(ow)

    assert _totals(STEP_COUNT_CODE) == {
        ("Apple Watch", "2026-10-10"): 100,
        ("Apple Watch", "2026-10-11"): 500,
        ("iPhone", "2026-10-11"): 1000,
    }
    day = _readings(STEP_COUNT_CODE).get(identifiers__value="user-123:steps:Apple Watch:2026-10-11")
    assert day.omh_data["body"]["effective_time_frame"] == {
        "time_interval": {"start_date_time": "2026-10-11T00:00:00-04:00", "end_date_time": "2026-10-12T00:00:00-04:00"}
    }


def test_a_sources_own_daily_total_is_its_days_steps(ow):
    ow.sample("steps", "2026-10-11T14:00:00Z", 300, WATCH)
    ow.sample("steps", "2026-10-11T16:00:00Z", 5000, WATCH, is_daily_total=True)

    _poll(ow)

    assert _totals(STEP_COUNT_CODE) == {("Apple Watch", "2026-10-11"): 5000}


def test_two_data_sources_under_one_label_take_the_larger_total(ow):
    """Two channels of one device count the same steps, so they are never added."""
    ow.sample("steps", "2026-10-11T14:00:00Z", 500, WATCH)
    ow.sample("steps", "2026-10-11T14:00:00Z", 700, WATCH_APP)

    _poll(ow)

    assert _totals(STEP_COUNT_CODE) == {("Apple Watch", "2026-10-11"): 700}


def test_a_days_total_is_updated_as_it_grows(ow):
    ow.sample("steps", "2026-10-12T10:00:00Z", 100, WATCH)
    _poll(ow)
    ow.sample("steps", "2026-10-12T11:00:00Z", 250, WATCH)
    _poll(ow)

    assert _totals(STEP_COUNT_CODE) == {("Apple Watch", "2026-10-12"): 350}
    assert _readings(STEP_COUNT_CODE).count() == 1


def test_a_day_the_fetch_holds_only_part_of_is_not_totalled(ow):
    """The first poll's window starts 7 days before NOW, on the 5th: earlier days are fetched only in part."""
    ow.sample("steps", "2026-10-04T20:00:00Z", 100, WATCH)
    ow.sample("steps", "2026-10-05T03:00:00Z", 100, WATCH)  # 23:00 on the 4th, at -04:00
    ow.sample("steps", "2026-10-05T05:00:00Z", 100, WATCH)

    _poll(ow)

    assert _totals(STEP_COUNT_CODE) == {("Apple Watch", "2026-10-05"): 100}


def test_a_day_long_past_is_totalled_again_when_its_steps_arrive(ow):
    """A phone that was offline for weeks delivers a day older than any lookback, which is
    totalled again from all its samples, those stored before included."""
    ow.sample("steps", "2026-09-01T14:00:00Z", 1000, PHONE, stored=NOW - timedelta(days=40))
    ow.sample("steps", "2026-10-12T10:00:00Z", 100, PHONE)
    _poll(ow)
    assert _totals(STEP_COUNT_CODE) == {("iPhone", "2026-10-12"): 100}

    ow.sample("steps", "2026-09-01T15:00:00Z", 500, PHONE, stored=NOW + timedelta(minutes=30))
    _poll(ow, now=NOW + timedelta(hours=2))

    assert _totals(STEP_COUNT_CODE) == {("iPhone", "2026-10-12"): 100, ("iPhone", "2026-09-01"): 1500}


# Reading by arrival


def test_a_sample_older_than_the_newest_reading_is_kept_when_it_arrives(ow):
    """The old JHE resumed from the newest reading, and lost what a phone delivered late."""
    ow.sample("heart_rate", "2026-10-12T11:00:00Z", 60, WATCH)
    _poll(ow)

    ow.sample("heart_rate", "2026-10-12T09:00:00Z", 70, PHONE, stored=NOW + timedelta(minutes=30))
    _poll(ow, now=NOW + timedelta(hours=2))

    values = sorted(obs.omh_data["body"]["heart_rate"]["value"] for obs in _readings("omh:heart-rate:2.0"))
    assert values == [60, 70]


def test_a_reading_is_stored_once_however_often_it_is_read(ow):
    ow.sample("heart_rate", "2026-10-12T11:00:00Z", 60, WATCH, stored=NOW - timedelta(minutes=10))
    _poll(ow)
    _poll(ow, now=NOW + timedelta(minutes=20))  # inside the overlap: the sample is read again

    assert _readings("omh:heart-rate:2.0").count() == 1


# Nightly sleep


def test_sleep_is_totalled_per_source_and_night(ow):
    ow.session("2026-10-10T03:00:00Z", "2026-10-10T10:00:00Z", 6, WATCH)  # 23:00 to 06:00 local
    ow.session("2026-10-10T18:00:00Z", "2026-10-10T19:00:00Z", 0.5, WATCH, is_nap=True)
    ow.session("2026-10-10T02:00:00Z", "2026-10-10T10:00:00Z", 7, RELAYED)
    ow.session("2026-10-10T02:00:00Z", "2026-10-10T10:00:00Z", 0, PHONE)  # a bedtime window, in bed only

    _poll(ow)

    assert _totals(TOTAL_SLEEP_TIME_CODE) == {
        ("Apple Watch", "2026-10-10"): 6.5 * 3600,
        ("Google Health via Apple Health", "2026-10-10"): 7 * 3600,
    }
    night = _readings(TOTAL_SLEEP_TIME_CODE).get(data_source__name="Apple Watch")
    assert night.omh_data["body"]["effective_time_frame"] == {
        "time_interval": {"start_date_time": "2026-10-09T23:00:00-04:00", "end_date_time": "2026-10-10T15:00:00-04:00"}
    }


def test_a_night_is_the_local_day_its_sleep_ends(ow):
    ow.session("2026-10-11T01:00:00Z", "2026-10-11T03:30:00Z", 2.5, WATCH)  # ends 23:30 on the 10th, local

    _poll(ow)

    assert _totals(TOTAL_SLEEP_TIME_CODE) == {("Apple Watch", "2026-10-10"): 2.5 * 3600}


def test_two_data_sources_under_one_label_take_the_longer_night(ow):
    ow.session("2026-10-10T03:00:00Z", "2026-10-10T10:00:00Z", 6, WATCH)
    ow.session("2026-10-10T03:00:00Z", "2026-10-10T10:00:00Z", 5, WATCH_APP)

    _poll(ow)

    assert _totals(TOTAL_SLEEP_TIME_CODE) == {("Apple Watch", "2026-10-10"): 6 * 3600}


def test_a_night_the_fetch_holds_only_part_of_is_not_totalled(ow):
    """Sessions are fetched from three days before the 5th; nights from the 5th on are whole."""
    ow.session("2026-10-04T03:00:00Z", "2026-10-04T10:00:00Z", 6, WATCH)
    ow.session("2026-10-05T03:00:00Z", "2026-10-05T10:00:00Z", 6, WATCH)

    _poll(ow)

    assert _totals(TOTAL_SLEEP_TIME_CODE) == {("Apple Watch", "2026-10-05"): 6 * 3600}
