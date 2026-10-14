"""
Poll a patient from Open Wearables soon after OW finishes syncing them.

OW publishes the progress of every sync on its Redis channel ``sync:status:all``. When
a run for one of its users ends, this command waits ``--delay`` seconds, so that the
batches a phone sends together land in one poll, then polls the patient linked to that
OW user, as ``ow_poll --patient-id`` would but under that patient's lock only: a full
``ow_poll`` running at the same time holds other patients up, not this one.

Redis drops events while nobody listens, so an hourly ``ow_poll`` backs this up.

``ow.redis_url`` (a JheSetting) names OW's Redis, e.g. ``redis://127.0.0.1:6379/0``.
"""

import json
import logging
import time

import redis
from django.core.management.base import BaseCommand
from django.db import close_old_connections

from core.management.commands.ow_poll import Command as PollCommand
from core.models import OW_USER_ID_SYSTEM, Patient
from core.services.jhe_settings import get_setting

logger = logging.getLogger(__name__)

CHANNEL = "sync:status:all"
DEFAULT_DELAY_SECONDS = 30
# OW stages after which a run stores nothing more. A failed or cancelled run can have
# stored part of its data first.
FINISHED_STAGES = {"completed", "failed", "cancelled"}


class Command(BaseCommand):
    help = "Poll a patient from Open Wearables soon after OW finishes syncing them."

    def add_arguments(self, parser):
        parser.add_argument(
            "--delay",
            type=float,
            default=DEFAULT_DELAY_SECONDS,
            help="Seconds to wait after a sync ends before polling its patient.",
        )

    def handle(self, *args, **options):
        url = get_setting("ow.redis_url", "")
        if not url:
            self.stderr.write("ow_listen aborted: ow.redis_url not configured")
            return

        pubsub = redis.Redis.from_url(url).pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(CHANNEL)
        self.stdout.write(f"ow_listen: listening on {CHANNEL}")
        due = {}
        while True:
            # A long-running process must drop a connection the database closed, as
            # Django does around each request.
            close_old_connections()
            self.step(pubsub, due, options["delay"])

    def step(self, pubsub, due, delay, clock=time.monotonic):
        """Take one event, if one comes within a second, then poll every patient who is due.

        ``due`` maps an OW user id to when to poll them. A sync that ends while its user
        is already due joins that poll.
        """
        ow_user_id = self._finished_user(pubsub.get_message(timeout=1.0))
        if ow_user_id:
            due.setdefault(ow_user_id, clock() + delay)
        now = clock()
        for ow_user_id in [user for user, at in due.items() if at <= now]:
            del due[ow_user_id]
            self.poll(ow_user_id)

    @staticmethod
    def _finished_user(message):
        """The OW user whose sync run the message says has ended, or None."""
        if not message or message.get("type") != "message":
            return None
        try:
            event = json.loads(message["data"])
        except (TypeError, ValueError):
            return None
        if not isinstance(event, dict) or event.get("stage") not in FINISHED_STAGES:
            return None
        return event.get("user_id") or None

    def poll(self, ow_user_id):
        """Poll the patient this OW user is linked to, if any."""
        if not bool(get_setting("module.ow", False)):
            return
        patient = Patient.objects.filter(identifiers__system=OW_USER_ID_SYSTEM, identifiers__value=ow_user_id).first()
        # A patient with a second OW link is polled as their oldest one only (Patient.get_ow_user_id).
        if patient is None or patient.get_ow_user_id() != ow_user_id:
            return

        poller = PollCommand(stdout=self.stdout, stderr=self.stderr)
        run = poller.prepare({})
        if run is None:
            return
        try:
            created = poller.poll_patient(patient, run)
        except Exception:
            logger.exception("ow_listen: poll failed for patient=%s", patient.id)
            return
        logger.info("ow_listen: polled patient=%s after an OW sync, created=%d", patient.id, created)
