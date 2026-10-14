"""Bulk import of a patient's own readings, for history they bring with them.

A file of past data (an Apple Health export, a Garmin account export) is parsed elsewhere into
records of ``{key, code, source, body}``: a stable key for the reading, its OMH or IEEE code, the
name of the device or app it came from, and the body of its data point. This stores a batch of
them in a few queries rather than a request each, which is the difference between minutes and
hours for a year of heart rate.

A key already stored is skipped, so uploading the same file twice adds nothing. A record marked
``replace`` overwrites what its key holds instead, when it differs: a day's step total from an
export taken mid-day is superseded by the full day in a later export.

Every body must validate against its code's schema, as a single Observation's would. Nothing is
refused for want of consent: consent decides who may read a patient's readings, not which of
their own readings they may keep.
"""

import uuid
from collections import Counter

from django.db import transaction
from django.utils import timezone
from jsonschema import ValidationError, validators
from referencing import Registry

from core.fhir.effective_time_frame import extract_effective_time_frame
from core.models import CodeableConcept, DataSource, Observation, ObservationIdentifier
from core.utils import NoNetwork, build_schema_registry, code_to_schema

IMPORT_SYSTEM = "jhe:import"
IMPORT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "jhe:observation-import")
MAX_RECORDS = 5000
MAX_ERRORS_REPORTED = 20
MODALITIES = {"sensed", "self-reported"}
UNKNOWN_SOURCE = "Unknown source"


def _validators(codes):
    """One body validator per code that names a schema, built once for the whole batch."""
    registry = build_schema_registry().combine(Registry(retrieve=NoNetwork()))
    found = {}
    for code in codes:
        try:
            schema = code_to_schema(code)
            found[code] = validators.validator_for(schema)(schema, registry=registry)
        except (KeyError, ValueError):
            continue
    return found


def _data_source(name, cache):
    if name not in cache:
        cache[name] = DataSource.objects.filter(name=name).order_by("id").first() or DataSource.objects.create(
            name=name, type="personal_device"
        )
    return cache[name]


def _problem(record, concepts, checks):
    """Why a record cannot be stored, or None."""
    if not isinstance(record, dict) or not record.get("key") or not isinstance(record.get("body"), dict):
        return "Each record needs a key, a code and a body."
    code = record.get("code")
    if code not in concepts or code not in checks:
        return f"Unknown code: {code}"
    try:
        checks[code].validate(record["body"])
    except ValidationError as error:
        return f"{code}: {error.message}"
    return None


def import_records(patient, records):
    """Store what is new and valid among ``records`` as the patient's; report what happened."""
    codes = {record.get("code") for record in records if isinstance(record, dict)}
    concepts = {cc.coding_code: cc for cc in CodeableConcept.objects.filter(coding_code__in=codes)}
    checks = _validators(concepts)
    now = timezone.now().isoformat()

    report = {"received": len(records), "created": 0, "updated": 0, "duplicates": 0, "invalid": 0}
    errors = []
    staged = {}  # identifier value -> (Observation, replace)
    data_sources = {}

    for index, record in enumerate(records):
        problem = _problem(record, concepts, checks)
        if problem:
            report["invalid"] += 1
            if len(errors) < MAX_ERRORS_REPORTED:
                errors.append({"index": index, "error": problem})
            continue

        value = str(uuid.uuid5(IMPORT_NAMESPACE, f"{patient.id}|{record['key']}"))
        if value in staged:
            report["duplicates"] += 1
            continue

        namespace, name, version = record["code"].split(":")
        omh_data = {
            "header": {
                "uuid": value,
                "schema_id": {"namespace": namespace, "name": name, "version": version},
                "source_creation_date_time": now,
                "modality": record.get("modality") if record.get("modality") in MODALITIES else "sensed",
            },
            "body": record["body"],
        }
        effective, start, end = extract_effective_time_frame(omh_data)
        source = record.get("source") if isinstance(record.get("source"), str) and record["source"].strip() else None
        staged[value] = (
            Observation(
                subject_patient=patient,
                codeable_concept=concepts[record["code"]],
                data_source=_data_source((source or UNKNOWN_SOURCE).strip(), data_sources),
                omh_data=omh_data,
                status="final",
                effective_date_time=effective,
                effective_period_start=start,
                effective_period_end=end,
            ),
            record.get("replace") is True,
        )

    stored = {
        identifier.value: identifier.observation
        for identifier in ObservationIdentifier.objects.filter(
            system=IMPORT_SYSTEM, value__in=list(staged)
        ).select_related("observation")
    }
    fresh = [(value, observation) for value, (observation, _) in staged.items() if value not in stored]
    changed = []
    for value, old in stored.items():
        new, replace = staged[value]
        if replace and (old.omh_data.get("body") != new.omh_data["body"] or old.data_source_id != new.data_source_id):
            for field in ("omh_data", "data_source", "effective_date_time", "effective_period_start"):
                setattr(old, field, getattr(new, field))
            old.effective_period_end = new.effective_period_end
            old.last_updated = timezone.now()
            changed.append(old)

    with transaction.atomic():
        Observation.objects.bulk_update(
            changed,
            [
                "omh_data",
                "data_source",
                "effective_date_time",
                "effective_period_start",
                "effective_period_end",
                "last_updated",
            ],
            batch_size=1000,
        )
        created = Observation.objects.bulk_create([observation for _, observation in fresh], batch_size=1000)
        ObservationIdentifier.objects.bulk_create(
            [
                ObservationIdentifier(observation=observation, system=IMPORT_SYSTEM, value=value)
                for (value, _), observation in zip(fresh, created, strict=True)
            ],
            batch_size=1000,
        )

    by_source = Counter(observation.data_source.name for observation in [*created, *changed])
    report["created"] = len(created)
    report["updated"] = len(changed)
    report["duplicates"] += len(stored) - len(changed)
    # A list rather than a dict keyed by source: the API renderer rewrites underscored keys.
    report["sources"] = [{"source": name, "count": n} for name, n in by_source.most_common()]
    report["errors"] = errors
    return report
