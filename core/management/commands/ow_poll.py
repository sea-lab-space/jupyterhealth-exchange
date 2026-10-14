"""
Open Wearables polling pipeline.

Cron-driven sidecar command that pulls observations from Open Wearables and
ingests them as JHE Observations.

Modes (selected via the ``ow.ingest_mode`` JheSetting):

* ``normalized`` (default): query the OW route each type is served from
  (see ``OW_TYPE_TO_ROUTE``), convert each sample with ``omh_shim.convert(source="ow_normalized")``
  and persist as Observations. Dedup is enforced by a paired
  ``ObservationIdentifier`` row with ``system="ow:normalized"`` and
  ``value=<the fetcher's dedupe key>``.

* ``raw``: walks the OW S3/MinIO bucket and converts via
  ``omh_shim.convert(source="oura_raw")``. Dedup uses the same pattern with
  ``system="ow:raw"``.

The command no-ops in two situations:

1. ``module.ow`` JheSetting is false (operator-controlled master switch).
2. ``ow.sync_in_progress`` holds a recent ISO timestamp (a previous tick is
   still running). Locks older than ``LOCK_STALE_AFTER`` are treated as
   abandoned (e.g. crashed worker) and force-reclaimed.

OW connection config (``ow.api_url``, ``ow.api_key``) is read from JheSettings
via ``get_setting()``, matching ``core/views/ow.py``.

``OW_TYPE_TO_CODE`` maps each polled type to the CodeableConcept its
Observations are filed under, and is intersected with the patient's consented
scopes so a poll can never widen consent. A type only belongs there once
omh-shim converts it to a schema id JHE can resolve: ``core/utils.py`` resolves
the ``omh`` and ``ieee`` namespaces only, which is why ``heart_rate_variability``
is absent (omh-shim 2.0 dropped the ``local:heart-rate-variability:1.0`` schema
it used to target).

``OW_TYPE_TO_ROUTE`` names the OW route each type is fetched from; a type absent
from it is served by ``timeseries``. The three sleep event types each fetch
``events/sleep`` independently, which keeps one data type mapping to one code.

``OW_TYPE_TO_SHIM_TYPE`` covers the keys that are not omh-shim data types:
``workout`` converts as ``physical_activity``. It needs its own key for its own OW
request and dedupe key.

Resting heart rate is not polled. OW serves it as its own series, but OMH has no
schema for it: filed under heart rate, it could not be told from the heart rate
measured at that instant.

Every reading is filed under the DataSource named after the device or app that
recorded it (``core.services.data_sources``): "Apple Watch", "Garmin", "Google Health
via Apple Health". A device or app writing through OW is one OW data source, so the
timeseries dedupe key names it too, and two of them measuring at the same instant
are both kept.

``TOTAL_TYPES`` are built here rather than converted, because each is a total over
many OW records rather than one of them: ``steps`` is one ``omh:step-count:3.0``
reading per source and local day, from the step samples, and ``sleep_duration`` one
``ieee:total-sleep-time:1.0`` reading per source and night, from the sleep sessions.
OW's own daily summaries keep a single source per day, so they are not read.

``OW_TYPE_TO_SERIES`` exists because most keys of ``OW_TYPE_TO_CODE`` are omh-shim
data types, not OW series names, and the two namespaces are not the same. They
happen to agree for most types, which is what makes the disagreement easy to miss:
omh-shim calls them ``body_weight`` and ``body_height`` while OW's SeriesType enum
calls them ``weight`` and ``height``. The ``types`` query parameter and the
timeseries dedupe key use the OW name; the ``convert()`` call keeps the omh-shim name.

``RAW_SUPPORTED_TYPES`` stays heart rate only. Raw mode has never ingested
anything (#746), so it is not widened alongside the normalized types.

``ow.poll_window_days`` (default 1) sets how far back OW is asked for samples.
It only bounds a patient's first poll: once they have an Observation the resume
watermark is the later bound, so a wide window costs nothing afterwards. Raise
it when patients link with device history already recorded, or pass ``--days``
for a one-off backfill.

``ow.sleep_lookback_days`` (default 7) sets how far back the ``LOOKBACK_TYPES``
are refetched on every poll, the daily step totals among them. See ``Fetcher.lookback``.
"""

import hashlib
import logging
import re
import uuid
from collections import namedtuple
from datetime import UTC, datetime, time, timedelta
from datetime import timezone as fixed_offset

import requests
from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.db import IntegrityError, transaction
from django.db.models.functions import Coalesce
from django.utils import timezone
from omh_shim import convert

from core.models import (
    OW_USER_ID_SYSTEM,
    CodeableConcept,
    DataSource,
    JheSetting,
    JheUser,
    Observation,
    ObservationIdentifier,
)
from core.services.data_sources import source_label
from core.services.jhe_settings import get_setting
from core.services.ow_ingest import list_new_objects, read_object

logger = logging.getLogger(__name__)

POLL_OVERLAP = timedelta(minutes=5)
DEFAULT_POLL_WINDOW_DAYS = 1
DEFAULT_SLEEP_LOOKBACK_DAYS = 7
PAGE_LIMIT = 100
MAX_PAGES = 200
HEART_RATE_CODE = "omh:heart-rate:2.0"
BLOOD_GLUCOSE_CODE = "omh:blood-glucose:4.0"
STEP_COUNT_CODE = "omh:step-count:3.0"
TOTAL_SLEEP_TIME_CODE = "ieee:total-sleep-time:1.0"

OW_TYPE_TO_CODE = {
    "heart_rate": HEART_RATE_CODE,
    "blood_glucose": BLOOD_GLUCOSE_CODE,
    "oxygen_saturation": "omh:oxygen-saturation:2.0",
    "respiratory_rate": "omh:respiratory-rate:2.0",
    "body_temperature": "omh:body-temperature:4.0",
    "body_weight": "omh:body-weight:3.0",
    "body_height": "omh:body-height:2.0",
    "steps": STEP_COUNT_CODE,
    "sleep_episode": "ieee:sleep-episode:1.0",
    "sleep_stage_summary": "ieee:sleep-stage-summary:1.0",
    "time_in_bed": "ieee:time-in-bed:1.0",
    "sleep_duration": TOTAL_SLEEP_TIME_CODE,
    "workout": "ieee:physical-activity:1.0",
}

OW_TYPE_TO_ROUTE = {
    "sleep_episode": "events/sleep",
    "sleep_stage_summary": "events/sleep",
    "time_in_bed": "events/sleep",
    "sleep_duration": "events/sleep",
    "workout": "events/workouts",
}
OW_TYPE_TO_SERIES = {
    "body_weight": "weight",
    "body_height": "height",
}
OW_TYPE_TO_SHIM_TYPE = {
    "workout": "physical_activity",
}
TOTAL_TYPES = {"steps", "sleep_duration"}
# How much earlier than its window a total is fetched, so that every local day or night
# it returns from the window's first UTC date on is whole. A local day starts up to 14
# hours before the UTC one, and a night's sessions start up to a day before it ends.
TOTAL_LEAD = {"steps": timedelta(days=2), "sleep_duration": timedelta(days=3)}
LOOKBACK_TYPES = {
    "sleep_episode",
    "sleep_stage_summary",
    "time_in_bed",
    "sleep_duration",
    "steps",
    "workout",
    "respiratory_rate",
}
NORMALIZED_SYSTEM = "ow:normalized"
RAW_SYSTEM = "ow:raw"
RAW_TRACE_ID_HEART_RATE = "/v2/usercollection/heartrate"
RAW_SUPPORTED_TYPES = {"heart_rate"}
_SYNC_LOCK_KEY = "ow.sync_in_progress"
# A lock older than this is considered abandoned (worker crashed mid-poll)
# and is force-reclaimed by the next tick. Sized at ~2x the default cron
# interval (15 min) so a healthy long-running poll is never preempted.
LOCK_STALE_AFTER = timedelta(minutes=30)

Fetcher = namedtuple("Fetcher", "system records convert dedupe_key source lookback", defaults=(None,))
"""One source of samples.

``system`` is the ObservationIdentifier system its rows are filed under,
``records`` a callable taking (start_time, end_time) and yielding raw sample
dicts, ``convert`` a callable turning one record into the OMH data point stored,
``dedupe_key`` a callable taking one record and returning the string that
identifies it, or None when the record cannot be identified and must be skipped,
and ``source`` a callable naming the DataSource one record is filed under.

``lookback`` replaces the resume watermark with a fixed window when a source
revises records after first publishing them. Oura can update a sleep record
after it is first created, and the watermark would already have advanced past
it, so it would never be fetched again and the upsert would never fire.
Workouts use it too: they sync as late as sleep.
A day's steps and a night's sleep do because each is rebuilt from its records,
and grows as they arrive.
Respiratory rate does because OW files it once per night at the start of sleep
and rewrites that sample in place when Oura revises the night.
A source that only ever appends leaves this None and resumes from the watermark.
"""

_ZONE_OFFSET = re.compile(r"([+-])(\d{2}):(\d{2})")


def _zone(offset):
    """An OW ``zone_offset`` ("-04:00") as a tzinfo, or None when it names none."""
    match = _ZONE_OFFSET.fullmatch(offset or "")
    if not match or int(match[2]) >= 24:
        return None
    sign = -1 if match[1] == "-" else 1
    return fixed_offset(sign * timedelta(hours=int(match[2]), minutes=int(match[3])))


def _iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


def _writer(record):
    """Identify the OW data source that wrote a record, without naming it.

    OW keeps one data source per user, provider, device model and source name, and one
    sample per data source, series and instant. The names are hashed because HealthKit
    names a person's devices after them ("Jan's Apple Watch") and dedupe keys are not
    meant to carry that.
    """
    source = record.get("source")
    if not isinstance(source, dict):
        return ""
    parts = (source.get("provider"), source.get("device"), source.get("source"))
    return hashlib.sha256("|".join(str(part or "") for part in parts).encode()).hexdigest()[:16]


def _label(record):
    """The DataSource name for an OW record, from the source metadata OW puts on it."""
    source = record.get("source")
    if not isinstance(source, dict):
        return source_label()
    return source_label(
        provider=source.get("provider"),
        source_name=source.get("source"),
        device_model=source.get("device"),
        device_type=source.get("device_type"),
    )


def _data_point(schema_id, body):
    """An IEEE 1752.1 data point around a body built here, with the header omh-shim gives."""
    namespace, name, version = schema_id.split(":")
    return {
        "header": {
            "uuid": str(uuid.uuid4()),
            "schema_id": {"namespace": namespace, "name": name, "version": version},
            "source_creation_date_time": _iso(datetime.now(UTC)),
            "modality": "sensed",
        },
        "body": body,
    }


def _write_sync_lock(value: str) -> None:
    """Write the ow.sync_in_progress JheSetting and bust the cache."""
    with transaction.atomic():
        setting, _ = JheSetting.objects.select_for_update().update_or_create(
            key=_SYNC_LOCK_KEY,
            defaults={"value_type": "string"},
        )
        setting.set_value("string", value)
        setting.save()
    cache.delete(f"jhe_setting:{_SYNC_LOCK_KEY}")


class Command(BaseCommand):
    help = "Poll Open Wearables for new observations."

    def add_arguments(self, parser):
        parser.add_argument(
            "--patient-id",
            type=int,
            default=None,
            help="Poll only the specified patient (by Patient.id). For debugging/backfill.",
        )
        parser.add_argument(
            "--days",
            type=int,
            default=None,
            help="Override the ow.poll_window_days setting for this run. For backfill.",
        )

    def handle(self, *args, **options):
        if not bool(get_setting("module.ow", False)):
            self.stdout.write("ow_poll skipped: module.ow=false")
            return

        if not self._acquire_lock():
            return

        try:
            self._run_poll(options)
        finally:
            self._release_lock()

    def _acquire_lock(self) -> bool:
        """Acquire ow.sync_in_progress (ISO timestamp). Return False if held.

        Atomic check-and-set under ``select_for_update`` so two concurrent
        cron ticks can't both win. A lock whose timestamp is older than
        ``LOCK_STALE_AFTER`` is treated as abandoned and reclaimed with a
        warning so a crashed previous tick auto-heals.
        """
        now = timezone.now()
        with transaction.atomic():
            setting, _ = JheSetting.objects.select_for_update().get_or_create(
                key=_SYNC_LOCK_KEY,
                defaults={"value_type": "string", "value_string": ""},
            )
            current = setting.get_value() or ""
            if current:
                acquired_at = self._parse_lock_timestamp(current)
                if acquired_at is not None and (now - acquired_at) < LOCK_STALE_AFTER:
                    self.stdout.write(f"ow_poll skipped: ow.sync_in_progress since {current}")
                    return False
                logger.warning(
                    "ow_poll: reclaiming stale ow.sync_in_progress lock (acquired_at=%s)",
                    current,
                )
            setting.set_value("string", now.isoformat())
            setting.save()
        cache.delete(f"jhe_setting:{_SYNC_LOCK_KEY}")
        return True

    def _release_lock(self) -> None:
        _write_sync_lock("")

    @staticmethod
    def _parse_lock_timestamp(value: str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def _poll_window(options):
        """How far back to ask OW for samples when a patient has nothing to resume from.

        A patient who links after already wearing a device has no prior Observation,
        so this window is the only thing deciding whether their history is reachable.
        Once they have one, the resume watermark is always the later bound, which is
        why widening this costs nothing on subsequent polls.
        """
        days = options.get("days")
        if days is None:
            try:
                days = int(get_setting("ow.poll_window_days", DEFAULT_POLL_WINDOW_DAYS))
            except (TypeError, ValueError):
                days = DEFAULT_POLL_WINDOW_DAYS
        return timedelta(days=max(days, 1))

    @staticmethod
    def _sleep_lookback():
        """How far back the ``LOOKBACK_TYPES`` are refetched, from ``ow.sleep_lookback_days``."""
        try:
            days = int(get_setting("ow.sleep_lookback_days", DEFAULT_SLEEP_LOOKBACK_DAYS))
        except (TypeError, ValueError):
            days = DEFAULT_SLEEP_LOOKBACK_DAYS
        return timedelta(days=max(days, 1))

    def _run_poll(self, options):
        mode = str(get_setting("ow.ingest_mode", "normalized") or "normalized").lower()
        if mode not in ("normalized", "raw"):
            self.stderr.write(f"ow_poll aborted: unknown ow.ingest_mode '{mode}'")
            return

        ow_api_url = (get_setting("ow.api_url", "") or "").rstrip("/")
        ow_api_key = get_setting("ow.api_key", "")
        if mode == "normalized" and (not ow_api_url or not ow_api_key):
            self.stderr.write("ow_poll aborted: ow.api_url / ow.api_key not configured")
            return

        codes = {}
        for ow_type, coding_code in OW_TYPE_TO_CODE.items():
            try:
                codes[ow_type] = CodeableConcept.objects.get(coding_code=coding_code)
            except CodeableConcept.DoesNotExist:
                self.stderr.write(f"CodeableConcept '{coding_code}' not found. Run seed first.")
        if not codes:
            return

        self._data_sources = {}
        poll_window = self._poll_window(options)

        users = JheUser.objects.filter(patient_profile__identifiers__system=OW_USER_ID_SYSTEM).distinct()
        patient_id = options.get("patient_id")
        if patient_id is not None:
            users = users.filter(patient_profile__id=patient_id)

        total_created = 0
        for user in users:
            patient = getattr(user, "patient_profile", None)
            if patient is None:
                continue
            consented_codes = {s.coding_code for s in patient.consolidated_consented_scopes()}
            polled = {t: c for t, c in codes.items() if c.coding_code in consented_codes}
            if not polled:
                continue
            ow_user_id = patient.get_ow_user_id()

            for ow_type, code in polled.items():
                try:
                    if mode == "normalized":
                        fetcher = self._normalized_fetcher(user, ow_user_id, ow_api_url, ow_api_key, ow_type)
                    else:
                        fetcher = self._raw_fetcher(user, ow_user_id, ow_type)
                    if fetcher is None:
                        continue
                    total_created += self._poll_user(user, patient, ow_type, code, fetcher, poll_window)
                except Exception:
                    logger.exception("ow_poll failed for jhe_user_id=%s type=%s", user.id, ow_type)

        self.stdout.write(self.style.SUCCESS(f"OW poll complete (mode={mode}). Created {total_created} observations."))

    def _fetch_page(self, user, ow_api_url, ow_api_key, ow_user_id, path, params):
        """Yield every OW record across pages.

        Every OW list route returns the same envelope, so one loop serves them
        all. ``params`` carries whatever that route needs, because the timeseries
        route windows on ``start_time``/``end_time`` while the events and
        summaries routes window on ``start_date``/``end_date``.
        """
        cursor = None
        for _ in range(MAX_PAGES):
            page_params = dict(params, limit=PAGE_LIMIT)
            if cursor:
                page_params["cursor"] = cursor

            try:
                resp = requests.get(
                    f"{ow_api_url}/api/v1/users/{ow_user_id}/{path}",
                    params=page_params,
                    headers={"X-Open-Wearables-API-Key": ow_api_key},
                    timeout=30,
                )
            except requests.RequestException as e:
                logger.error("OW request failed for user=%s path=%s: %s", user.id, path, e)
                return

            if resp.status_code != 200:
                logger.error(
                    "OW error for user=%s path=%s: %s %s",
                    user.id,
                    path,
                    resp.status_code,
                    resp.text[:300],
                )
                return

            data = resp.json()
            records = data.get("data", data) if isinstance(data, dict) else data
            if not isinstance(records, list):
                logger.warning("OW returned non-list payload for user=%s path=%s", user.id, path)
                return
            yield from records

            pagination = data.get("pagination") or {} if isinstance(data, dict) else {}
            next_cursor = pagination.get("next_cursor")
            if not pagination.get("has_more") or not next_cursor or next_cursor == cursor:
                return
            cursor = next_cursor

        logger.warning("OW hit MAX_PAGES for user=%s path=%s", user.id, path)

    def _timeseries_dedupe_key(self, ow_user_id, ow_type, record):
        """Identify one timeseries sample.

        OW timeseries samples carry no id of their own, but OW keeps one sample per
        data source, series and instant, so those and the patient are the natural
        key. The OW user id is part of the key because ObservationIdentifier is
        unique on (system, value) globally, not per patient, so two patients
        reporting the same value at the same instant would otherwise collide and the
        second row would be dropped. The data source is, so a watch and a phone
        measuring at the same instant are both kept. Raw samples name none.

        The series comes off the sample rather than from the data type, because
        the two namespaces differ (see ``OW_TYPE_TO_SERIES``).
        """
        timestamp = record.get("timestamp")
        if not timestamp:
            return None
        series = record.get("type") or OW_TYPE_TO_SERIES.get(ow_type, ow_type)
        writer = _writer(record)
        return f"{ow_user_id}:{series}:{writer}:{timestamp}" if writer else f"{ow_user_id}:{series}:{timestamp}"

    def _event_dedupe_key(self, ow_user_id, ow_type, record):
        """Identify one OW event record.

        Events carry their own id, so it is the natural key. The OW user id is
        prefixed for the same reason as the timeseries key: ObservationIdentifier
        is unique on (system, value) globally rather than per patient. The data
        type is part of the key because one OW record feeds several data types --
        a SleepSession is read as both a sleep episode and a sleep stage summary --
        so the id alone cannot tell those Observations apart.
        """
        record_id = record.get("id")
        if not record_id:
            return None
        return f"{ow_user_id}:{ow_type}:{record_id}"

    def _summary_dedupe_key(self, ow_user_id, ow_type, record):
        """Identify one OW daily summary.

        Summaries carry no id, only the day they summarise, and a patient has one
        summary of a given kind per day.
        """
        day = record.get("date")
        if not day:
            return None
        return f"{ow_user_id}:{ow_type}:{day}"

    def _normalized_fetcher(self, user, ow_user_id, ow_api_url, ow_api_key, ow_type):
        """Samples from whichever OW route serves this data type."""
        if ow_type in TOTAL_TYPES:
            return self._total_fetcher(user, ow_user_id, ow_api_url, ow_api_key, ow_type)
        path = OW_TYPE_TO_ROUTE.get(ow_type, "timeseries")
        if path == "timeseries":
            series = OW_TYPE_TO_SERIES.get(ow_type, ow_type)
            params = lambda start_time, end_time: {
                "types": series,
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat(),
            }
            dedupe_key = lambda record: self._timeseries_dedupe_key(ow_user_id, ow_type, record)
        else:
            params = lambda start_time, end_time: {
                "start_date": start_time.isoformat(),
                "end_date": end_time.isoformat(),
            }
            if path.startswith("events/"):
                dedupe_key = lambda record: self._event_dedupe_key(ow_user_id, ow_type, record)
            else:
                dedupe_key = lambda record: self._summary_dedupe_key(ow_user_id, ow_type, record)

        shim_type = OW_TYPE_TO_SHIM_TYPE.get(ow_type, ow_type)
        return Fetcher(
            system=NORMALIZED_SYSTEM,
            records=lambda start_time, end_time: self._fetch_page(
                user, ow_api_url, ow_api_key, ow_user_id, path, params(start_time, end_time)
            ),
            convert=lambda record: convert(source="ow_normalized", data_type=shim_type, sample=record, tz=UTC),
            dedupe_key=dedupe_key,
            source=_label,
            lookback=self._sleep_lookback() if ow_type in LOOKBACK_TYPES else None,
        )

    def _total_fetcher(self, user, ow_user_id, ow_api_url, ow_api_key, ow_type):
        """Each source's daily steps or nightly sleep, totalled from the OW records they come from."""
        if ow_type == "steps":
            totals = lambda start_time, end_time: self._daily_steps(
                self._fetch_page(
                    user,
                    ow_api_url,
                    ow_api_key,
                    ow_user_id,
                    "timeseries",
                    {"types": "steps", "start_time": start_time.isoformat(), "end_time": end_time.isoformat()},
                ),
                start_time,
            )
            convert = lambda total: _data_point(
                STEP_COUNT_CODE,
                {
                    "step_count": {"value": total["steps"], "unit": "steps"},
                    "effective_time_frame": {"time_interval": total["interval"]},
                },
            )
        else:
            totals = lambda start_time, end_time: self._nightly_sleep(
                self._fetch_page(
                    user,
                    ow_api_url,
                    ow_api_key,
                    ow_user_id,
                    "events/sleep",
                    {"start_date": start_time.isoformat(), "end_date": end_time.isoformat()},
                ),
                start_time,
            )
            convert = lambda total: _data_point(
                TOTAL_SLEEP_TIME_CODE,
                {
                    "total_sleep_time": {"value": total["seconds"], "unit": "sec"},
                    "effective_time_frame": {"time_interval": total["interval"]},
                },
            )
        return Fetcher(
            system=NORMALIZED_SYSTEM,
            records=lambda start_time, end_time: totals(start_time - TOTAL_LEAD[ow_type], end_time),
            convert=convert,
            dedupe_key=lambda total: f"{ow_user_id}:{ow_type}:{total['label']}:{total['day']}",
            source=lambda total: total["label"],
            lookback=self._sleep_lookback(),
        )

    @staticmethod
    def _daily_steps(samples, fetched_from):
        """Each source's steps per local day.

        A day's total is the OW data source's own daily total where it reports one
        (``is_daily_total``, as Garmin does), else the sum of its samples. Two data
        sources under one label, two channels of one device, take the larger total,
        never the sum. A sample counts toward the day it was taken on in its own
        offset, and only days wholly inside the fetch are returned (see ``TOTAL_LEAD``).
        """
        per_writer = {}
        for sample in samples:
            timestamp, value = sample.get("timestamp"), sample.get("value")
            if not timestamp or value is None:
                continue
            day = datetime.fromisoformat(timestamp).astimezone(_zone(sample.get("zone_offset")) or UTC).date()
            entry = per_writer.setdefault(
                (day, _writer(sample)), {"label": _label(sample), "daily": None, "summed": 0.0, "offset": ""}
            )
            if sample.get("is_daily_total"):
                entry["daily"] = value if entry["daily"] is None else max(entry["daily"], value)
            else:
                entry["summed"] += value
            entry["offset"] = max(entry["offset"], sample.get("zone_offset") or "")

        first_day = (fetched_from + TOTAL_LEAD["steps"]).astimezone(UTC).date()
        days = {}
        for (day, _writer_id), entry in per_writer.items():
            steps = round(entry["daily"] if entry["daily"] is not None else entry["summed"])
            key = (day, entry["label"])
            if day < first_day or (key in days and days[key]["steps"] >= steps):
                continue
            start = datetime.combine(day, time.min, tzinfo=_zone(entry["offset"]) or UTC)
            days[key] = {
                "day": day.isoformat(),
                "label": entry["label"],
                "steps": steps,
                "interval": {"start_date_time": _iso(start), "end_date_time": _iso(start + timedelta(days=1))},
            }
        return days.values()

    @staticmethod
    def _nightly_sleep(sessions, fetched_from):
        """Each source's sleep per night.

        A night is the local day a session ends on. Its total is the time asleep of
        every session of one OW data source ending that day, naps included, over the
        span from the first bedtime to the last waking. Two data sources under one
        label take the larger total. A session of time in bed alone is no sleep. Only
        nights whose sessions all started inside the fetch are returned (see
        ``TOTAL_LEAD``).
        """
        per_writer = {}
        for session in sessions:
            seconds = session.get("sleep_duration_seconds")
            if not seconds or seconds <= 0 or not session.get("start_time") or not session.get("end_time"):
                continue
            zone = _zone(session.get("zone_offset")) or UTC
            start = datetime.fromisoformat(session["start_time"]).astimezone(zone)
            end = datetime.fromisoformat(session["end_time"]).astimezone(zone)
            entry = per_writer.setdefault(
                (end.date(), _writer(session)), {"label": _label(session), "seconds": 0, "start": start, "end": end}
            )
            entry["seconds"] += seconds
            entry["start"] = min(entry["start"], start)
            entry["end"] = max(entry["end"], end)

        first_night = (fetched_from + TOTAL_LEAD["sleep_duration"]).astimezone(UTC).date()
        nights = {}
        for (night, _writer_id), entry in per_writer.items():
            key = (night, entry["label"])
            if night < first_night or (key in nights and nights[key]["seconds"] >= entry["seconds"]):
                continue
            nights[key] = {
                "day": night.isoformat(),
                "label": entry["label"],
                "seconds": int(entry["seconds"]),
                "interval": {"start_date_time": _iso(entry["start"]), "end_date_time": _iso(entry["end"])},
            }
        return nights.values()

    def _raw_fetcher(self, user, ow_user_id, ow_type):
        """Samples from Oura payloads OW archived to its bucket.

        Returns None for a type raw mode cannot serve, so the caller skips it
        without listing the bucket.
        """
        if ow_type not in RAW_SUPPORTED_TYPES:
            return None
        return Fetcher(
            system=RAW_SYSTEM,
            records=lambda start_time, end_time: self._fetch_raw_objects(user, ow_user_id, start_time),
            convert=lambda record: convert(source="oura_raw", data_type=ow_type, sample=record, tz=UTC),
            dedupe_key=lambda record: self._timeseries_dedupe_key(ow_user_id, ow_type, record),
            source=lambda record: "Oura",
        )

    def _fetch_raw_objects(self, user, ow_user_id, start_time):
        """Yield every record inside the heart-rate S3 objects newer than start_time."""
        try:
            objects = list_new_objects(ow_user_id, start_time)
        except Exception as e:
            logger.error("OW raw S3 list failed for user=%s: %s", user.id, e)
            return

        for obj in objects:
            if RAW_TRACE_ID_HEART_RATE not in obj.key:
                continue
            try:
                payload = read_object(obj.key)
            except Exception:
                logger.warning("Skipping unreadable raw object %s", obj.key, exc_info=True)
                continue
            yield from payload.get("data", [])

    def _poll_user(self, user, patient, ow_type, code, fetcher, poll_window):
        """Ingest one data type for one patient from one source."""
        end_time = timezone.now()
        if fetcher.lookback is None:
            start_time = self._resume_start_time(patient, code, fetcher.system, end_time - poll_window)
        else:
            start_time = end_time - max(fetcher.lookback, poll_window)

        created = 0
        for record in fetcher.records(start_time, end_time):
            try:
                omh_record = fetcher.convert(record)
            except Exception:
                logger.warning("Skipping unconvertible record for user=%s type=%s", user.id, ow_type, exc_info=True)
                continue

            identifier = fetcher.dedupe_key(record)
            if not identifier:
                continue

            data_source = self._data_source(fetcher.source(record))
            if self._save_observation(patient, code, data_source, omh_record, fetcher.system, identifier):
                created += 1

        logger.info("Poll completed for jhe_user=%s patient=%s created=%d", user.id, patient.id, created)
        return created

    def _data_source(self, name):
        """The DataSource named ``name``, created the first time a reading names it."""
        if name not in self._data_sources:
            self._data_sources[name] = DataSource.objects.filter(name=name).order_by(
                "id"
            ).first() or DataSource.objects.create(name=name, type="personal_device")
        return self._data_sources[name]

    def _resume_start_time(self, patient, code, system, window_start):
        """Return the time this poll should ask OW from.

        Resuming from the most recent ingested row avoids refetching the whole
        window every tick. POLL_OVERLAP is subtracted so a sample written on the
        boundary of the previous run is not missed.

        Sample time, not write time: Oura sleep only syncs when the patient opens
        the app, so a night can arrive after rows measured later were already
        written. Resuming from the write time would skip it permanently.

        An interval-shaped body populates ``effective_period_start`` and leaves
        ``effective_date_time`` null, so the two are coalesced. Ordering on the
        raw column instead would sort nulls first under a descending Postgres
        sort, pinning the watermark to whichever interval row happened to exist.
        """
        sample_time = Coalesce("effective_date_time", "effective_period_start")
        last_obs = (
            Observation.objects.filter(
                subject_patient=patient,
                codeable_concept=code,
                identifiers__system=system,
            )
            .annotate(sample_time=sample_time)
            .exclude(sample_time=None)
            .order_by("-sample_time")
            .first()
        )
        if not last_obs:
            return window_start
        return max(window_start, last_obs.sample_time - POLL_OVERLAP)

    def _save_observation(self, patient, code, data_source, omh_record, system, value):
        """Create one Observation and its dedupe identifier, or overwrite the existing one.

        Last write wins: Oura can update a sleep record after it is first created,
        so a record whose key is already stored replaces the stored body rather
        than being skipped. An unchanged body is left alone: the header's uuid and
        creation time are regenerated on every convert, so rewriting it would bump
        ``last_updated`` on every tick of the lookback window.

        Returns True only when a row was created. A concurrent tick can win the
        race between the lookup and the insert, so IntegrityError is treated as
        already-ingested rather than an error. Any other failure, on create or
        update, is logged and the record skipped, so the rest of the poll still lands.

        The lookup is scoped to the patient so a key already stored on another
        patient's row, for example after an OW link moved between patients, is never
        overwritten: the second insert hits the unique constraint and is skipped.
        """
        try:
            with transaction.atomic():
                existing = (
                    Observation.objects.filter(
                        subject_patient=patient, identifiers__system=system, identifiers__value=value
                    )
                    .order_by("id")
                    .first()
                )
                if existing is not None:
                    if existing.omh_data.get("body") == omh_record.get("body"):
                        return False
                    existing.omh_data = omh_record
                    existing.save()
                    return False

                obs = Observation.objects.create(
                    subject_patient=patient,
                    codeable_concept=code,
                    data_source=data_source,
                    omh_data=omh_record,
                    status="final",
                )
                ObservationIdentifier.objects.create(
                    observation=obs,
                    system=system,
                    value=value,
                )
            return True
        except IntegrityError:
            return False
        except Exception:
            logger.warning(
                "Failed to persist observation for patient=%s identifier=%s",
                patient.id,
                value,
                exc_info=True,
            )
            return False
