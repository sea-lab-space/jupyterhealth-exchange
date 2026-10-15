"""One patient's readings, rolled up in the database for charts.

A chart of six months of heart rate is a few hundred daily points drawn from a hundred thousand
readings or more. Sending every reading to be decoded elsewhere takes minutes at that size;
summing them here takes a fraction of a second. So the rollup is done in SQL, over the OMH and
IEEE documents themselves, by the rules a reader applying them by hand would use:

- the day is the calendar day the reading was recorded in, in the offset its device used;
  a night of sleep, and a reading that covers an interval, belong to the day they end on;
  a day's step total and a blood-pressure reading to the day they start on;
- values are in one unit per measure (hours of sleep, kg, mg/dL, degrees C);
- nothing is ever combined across sources: each (day, measure, source) is its own row;
- a whole day's total, as watches and phones report daily steps, is never added to another:
  the same device's day often arrives twice (synced live, and again in an uploaded export).
  A day's sum is its largest whole-day total, or the sum of its shorter readings if larger.

A chart of a few days of dense readings, such as a watch's heart rate, is drawn from the
readings averaged per interval (``series``) for the same reason. ``deliveries`` counts what
arrived, by when JHE stored it, for a roster that asks whether data is still coming.

Who may read is decided as for the observations themselves (``Observation.fhir_search``): a
patient reads all of their own; a practitioner all of a patient they share an organization
with, and a study narrows that to what the study asks for.

Ported from SeaCollect's JHE patches 0012, 0013 and 0019, from before SeaCollect left JHE.
"""

from datetime import UTC, datetime, time, timedelta

from django.core.exceptions import PermissionDenied
from django.db.models import (
    Avg,
    Case,
    Count,
    DateTimeField,
    DurationField,
    ExpressionWrapper,
    F,
    FloatField,
    Func,
    Max,
    Min,
    Sum,
    Value,
    When,
)
from django.db.models.fields.json import KT
from django.db.models.functions import Cast, Coalesce, Substr
from django.utils import timezone

from core.fhir.scope import authorize_practitioner_scope
from core.models import Observation, StudyPatient, StudyScopeRequest

HR = "omh:heart-rate:2.0"
STEPS = "omh:step-count:3.0"
SLEEP_DURATION = "omh:sleep-duration:2.0"
TOTAL_SLEEP_TIME = "ieee:total-sleep-time:1.0"
SPO2 = "omh:oxygen-saturation:2.0"
RR = "omh:respiratory-rate:2.0"
WEIGHT = "omh:body-weight:3.0"
GLUCOSE = "omh:blood-glucose:4.0"
BP = "omh:blood-pressure:4.0"
TEMPERATURE = "omh:body-temperature:4.0"


def _text(path):
    return KT(f"omh_data__body__{path}")


def _number(path):
    return Cast(_text(path), FloatField())


def _unit_is(path, unit):
    return {f"omh_data__body__{path}__unit": unit}


def _code(code):
    return {"codeable_concept__coding_code": code}


def _hours(path):
    return Case(
        When(**_unit_is(path, "h"), then=_number(f"{path}__value")),
        When(**_unit_is(path, "sec"), then=_number(f"{path}__value") / 3600),
        default=_number(f"{path}__value") / 60,
    )


# The measure's value in one unit. Units the schemas allow but no device sends are left as is.
VALUE = Case(
    When(**_code(HR), then=_number("heart_rate__value")),
    When(**_code(STEPS), then=_number("step_count__value")),
    When(**_code(SLEEP_DURATION), then=_hours("sleep_duration")),
    When(**_code(TOTAL_SLEEP_TIME), then=_hours("total_sleep_time")),
    When(**_code(SPO2), then=_number("oxygen_saturation__value")),
    When(**_code(RR), then=_number("respiratory_rate__value")),
    When(
        **_code(WEIGHT),
        then=Case(
            When(**_unit_is("body_weight", "lb"), then=_number("body_weight__value") * 0.45359237),
            When(**_unit_is("body_weight", "g"), then=_number("body_weight__value") / 1000),
            default=_number("body_weight__value"),
        ),
    ),
    When(
        **_code(GLUCOSE),
        then=Case(
            When(**_unit_is("blood_glucose", "mmol/L"), then=_number("blood_glucose__value") * 18.016),
            default=_number("blood_glucose__value"),
        ),
    ),
    When(**_code(BP), then=_number("systolic_blood_pressure__value")),
    When(
        **_code(TEMPERATURE),
        then=Case(
            When(**_unit_is("body_temperature", "F"), then=(_number("body_temperature__value") - 32) * 5 / 9),
            When(**_unit_is("body_temperature", "K"), then=_number("body_temperature__value") - 273.15),
            default=_number("body_temperature__value"),
        ),
    ),
    default=Value(None),
    output_field=FloatField(),
)
# The second value a measure has: blood pressure's diastolic.
SECOND = Case(
    When(**_code(BP), then=_number("diastolic_blood_pressure__value")), default=Value(None), output_field=FloatField()
)

_AT = _text("effective_time_frame__date_time")
_START = _text("effective_time_frame__time_interval__start_date_time")
_END = _text("effective_time_frame__time_interval__end_date_time")
# YYYY-MM-DD as written in the document, i.e. in the device's own offset.
DAY = Substr(
    Case(When(codeable_concept__coding_code__in=[STEPS, BP], then=Coalesce(_AT, _START)), default=Coalesce(_END, _AT)),
    1,
    10,
)
MEASURED = Coalesce(F("effective_date_time"), F("effective_period_start"))
# A reading this long is a day's total. Shorter than 24 hours to allow for DST.
WHOLE_DAY = timedelta(hours=20)
SPAN = ExpressionWrapper(F("effective_period_end") - F("effective_period_start"), output_field=DurationField())


def visible(patient, user, study_id=None, codes=None):
    """The patient's observations this user may read, of ``codes`` if given and of what
    ``study_id`` asks for if given. Raises PermissionDenied for a practitioner who shares no
    organization with the patient or names a study outside theirs, and for anyone else who is not
    the patient."""
    own = user.get_patient()
    if own is None or own.id != patient.id:
        if not user.is_practitioner():
            raise PermissionDenied("Current User does not have authorization to access this Patient.")
        authorize_practitioner_scope(user.id, study_id=study_id, patient_id=patient.id)
    observations = Observation.objects.filter(subject_patient=patient)
    if study_id:
        if not StudyPatient.objects.filter(study_id=study_id, patient=patient).exists():
            return observations.none()
        asked = StudyScopeRequest.objects.filter(study_id=study_id).values("scope_code_id")
        observations = observations.filter(codeable_concept_id__in=asked)
    if codes is not None:
        observations = observations.filter(codeable_concept__coding_code__in=codes)
    return observations


def window(observations, days=None, start=None, end=None):
    """The days to roll up: as asked, or the last ``days`` (180 if neither) up to the newest
    reading."""
    if start and end:
        return start, end
    newest = observations.annotate(_at=MEASURED).aggregate(newest=Max("_at"))["newest"]
    end = newest.astimezone(UTC).date() if newest else timezone.now().date()
    return end - timedelta(days=(days or 180) - 1), end


def rollup(observations, start, end):
    """Per (day, code, source) aggregates for days start..end inclusive, plus each (code,
    source)'s count and span over the whole history and its newest reading."""
    # The effective columns are instants; widen by a day each side so every local day in the
    # window is caught, then keep exactly the days asked for.
    lower = datetime.combine(start - timedelta(days=1), time.min, tzinfo=UTC)
    upper = datetime.combine(end + timedelta(days=2), time.min, tzinfo=UTC)
    in_window = observations.annotate(_at=MEASURED).filter(_at__gte=lower, _at__lt=upper)
    days = (
        in_window.annotate(day=DAY, v=VALUE, v2=SECOND, span=SPAN)
        .filter(day__gte=start.isoformat(), day__lte=end.isoformat(), v__isnull=False)
        .values("day", "codeable_concept__coding_code", "data_source__name")
        .annotate(
            count=Count("id"),
            whole=Max(Case(When(span__gte=WHOLE_DAY, then=F("v")), output_field=FloatField())),
            parts=Sum(Case(When(span__gte=WHOLE_DAY, then=Value(None)), default=F("v"), output_field=FloatField())),
            avg=Avg("v"),
            min=Min("v"),
            max=Max("v"),
            avg2=Avg("v2"),
        )
        .order_by("day", "codeable_concept__coding_code", "data_source__name")
    )
    sources = (
        observations.annotate(_at=MEASURED)
        .values("codeable_concept__coding_code", "data_source__name")
        .annotate(count=Count("id"), first=Min("_at"), last=Max("_at"))
        .order_by("-count")
    )
    latest = (
        observations.annotate(_at=MEASURED, v=VALUE, v2=SECOND)
        .filter(_at__isnull=False, v__isnull=False)
        .order_by("codeable_concept_id", "data_source_id", F("_at").desc())
        .distinct("codeable_concept_id", "data_source_id")
        .values("codeable_concept__coding_code", "data_source__name", "_at", "v", "v2")
    )
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": [
            {
                "date": row["day"],
                "code": row["codeable_concept__coding_code"],
                "source": row["data_source__name"],
                "count": row["count"],
                "sum": max((v for v in (row["whole"], row["parts"]) if v is not None), default=None),
                "avg": row["avg"],
                "min": row["min"],
                "max": row["max"],
                "avg2": row["avg2"],
            }
            for row in days
        ],
        "sources": [
            {
                "code": row["codeable_concept__coding_code"],
                "source": row["data_source__name"],
                "count": row["count"],
                "first": row["first"],
                "last": row["last"],
            }
            for row in sources
        ],
        "latest": [
            {
                "code": row["codeable_concept__coding_code"],
                "source": row["data_source__name"],
                "at": row["_at"],
                "value": row["v"],
                "value2": row["v2"],
            }
            for row in latest
        ],
    }


def series(observations, code, start, end, minutes):
    """One measure's readings from ``start`` to ``end`` (instants), averaged per ``minutes``
    and per source. Intervals are aligned to UTC and named by their start."""
    interval = Func(
        Value(timedelta(minutes=minutes)),
        F("_at"),
        Value(datetime(2000, 1, 1, tzinfo=UTC)),
        function="date_bin",
        output_field=DateTimeField(),
    )
    rows = (
        observations.filter(codeable_concept__coding_code=code)
        .annotate(_at=MEASURED, v=VALUE)
        .filter(_at__gte=start, _at__lt=end, v__isnull=False)
        .annotate(t=interval)
        .values("t", "data_source__name")
        .annotate(count=Count("id"), avg=Avg("v"), min=Min("v"), max=Max("v"))
        .order_by("t", "data_source__name")
    )
    return {
        "code": code,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "minutes": minutes,
        "points": [
            {
                "t": row["t"],
                "source": row["data_source__name"],
                "count": row["count"],
                "avg": row["avg"],
                "min": row["min"],
                "max": row["max"],
            }
            for row in rows
        ],
    }


def deliveries(observations):
    """How many readings have arrived, and when JHE stored the newest."""
    found = observations.aggregate(count=Count("id"), last=Max("created"))
    return {"count": found["count"], "last_delivered": found["last"]}
