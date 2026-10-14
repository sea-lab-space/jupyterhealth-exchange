"""
Tests for `ow_listen`: polling a patient soon after Open Wearables finishes syncing them.
"""

import json
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.core.management import call_command

from core.management.commands.ow_listen import Command
from core.models import OW_USER_ID_SYSTEM, CodeableConcept, JheSetting, PatientIdentifier


def _set_jhe_setting(key, value, value_type):
    setting, _ = JheSetting.objects.update_or_create(key=key, defaults={"value_type": value_type})
    setting.set_value(value_type, value)
    setting.save()
    cache.delete(f"jhe_setting:{key}")


def _event(user_id, stage="completed"):
    return {"type": "message", "channel": b"sync:status:all", "data": json.dumps({"user_id": user_id, "stage": stage})}


class FakePubSub:
    def __init__(self, *messages):
        self.messages = list(messages)

    def get_message(self, timeout=None):
        return self.messages.pop(0) if self.messages else None


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def listener():
    command = Command(stdout=StringIO(), stderr=StringIO())
    with patch.object(Command, "poll") as poll:
        yield command, poll


def test_a_finished_sync_is_polled_after_the_delay(listener):
    command, poll = listener
    clock, due = Clock(), {}

    command.step(FakePubSub(_event("ow-1")), due, 30, clock)
    poll.assert_not_called()

    clock.now += 31
    command.step(FakePubSub(), due, 30, clock)

    poll.assert_called_once_with("ow-1")
    assert due == {}


def test_syncs_ending_before_the_poll_share_it(listener):
    command, poll = listener
    clock, due = Clock(), {}
    command.step(FakePubSub(_event("ow-1")), due, 30, clock)
    clock.now += 10
    command.step(FakePubSub(_event("ow-1", stage="failed")), due, 30, clock)

    clock.now += 21
    command.step(FakePubSub(), due, 30, clock)

    poll.assert_called_once_with("ow-1")


@pytest.mark.parametrize(
    "message",
    [
        _event("ow-1", stage="started"),
        _event("ow-1", stage="processing"),
        {"type": "message", "data": b"not json"},
        {"type": "message", "data": json.dumps(["not", "an", "event"])},
        {"type": "subscribe", "data": 1},
        _event(None),
        None,
    ],
)
def test_anything_but_a_finished_run_is_ignored(listener, message):
    command, poll = listener
    clock, due = Clock(), {}

    command.step(FakePubSub(message), due, 0, clock)

    poll.assert_not_called()
    assert due == {}


@pytest.fixture
def linked(db, patient):
    CodeableConcept.objects.create(
        coding_system="https://w3id.org/openmhealth", coding_code="omh:heart-rate:2.0", text="Heart rate"
    )
    PatientIdentifier.objects.create(patient=patient, system=OW_USER_ID_SYSTEM, value="ow-1")
    cache.clear()
    _set_jhe_setting("module.ow", True, "bool")
    _set_jhe_setting("ow.api_url", "https://ow.example.test", "string")
    _set_jhe_setting("ow.api_key", "test-key", "string")
    yield patient
    cache.clear()


def test_the_linked_patient_is_polled(linked):
    with patch("core.management.commands.ow_poll.Command.poll_patient", return_value=0) as poll_patient:
        Command(stdout=StringIO(), stderr=StringIO()).poll("ow-1")

    poll_patient.assert_called_once()
    assert poll_patient.call_args.args[0] == linked


def test_an_unknown_ow_user_is_not_polled(linked):
    with patch("core.management.commands.ow_poll.Command.poll_patient") as poll_patient:
        Command(stdout=StringIO(), stderr=StringIO()).poll("ow-unknown")

    poll_patient.assert_not_called()


def test_a_patients_second_ow_link_is_not_polled(linked):
    """Every caller talks to a patient's oldest OW user, so a sync of another is not theirs."""
    PatientIdentifier.objects.create(patient=linked, system=OW_USER_ID_SYSTEM, value="ow-2")

    with patch("core.management.commands.ow_poll.Command.poll_patient") as poll_patient:
        Command(stdout=StringIO(), stderr=StringIO()).poll("ow-2")

    poll_patient.assert_not_called()


def test_nothing_is_polled_while_the_module_is_off(linked):
    _set_jhe_setting("module.ow", False, "bool")

    with patch("core.management.commands.ow_poll.Command.poll_patient") as poll_patient:
        Command(stdout=StringIO(), stderr=StringIO()).poll("ow-1")

    poll_patient.assert_not_called()


def test_a_failed_poll_does_not_stop_the_listener(linked, caplog):
    with patch("core.management.commands.ow_poll.Command.poll_patient", side_effect=RuntimeError("boom")):
        Command(stdout=StringIO(), stderr=StringIO()).poll("ow-1")

    assert "poll failed" in caplog.text


def test_aborts_without_a_redis_url(db):
    cache.clear()
    err = StringIO()

    call_command("ow_listen", stdout=StringIO(), stderr=err)

    assert "ow.redis_url" in err.getvalue()
