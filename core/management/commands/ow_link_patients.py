"""
Link patients to Open Wearables users that another service created.

Reads lines of ``<system>|<value> <OW user id>`` from stdin: an identifier of the
patient's, such as the one a deployment signs them in with, and their OW user. Each
patient found gets the OW user as their ``OW_USER_ID_SYSTEM`` identifier, which is
what ``ow_poll`` polls.

JHE links an OW user itself when it creates one (``POST /api/v1/ow/users``). A
deployment whose sign-in service created its patients' OW users copies the links with
this instead, run by an operator. No API lets anyone claim an OW user, because claiming
one would hand over its readings.

A patient already linked keeps their link, and an OW user linked to one patient is
never linked to another; both are reported. Running it again changes nothing.
"""

import sys

from django.core.management.base import BaseCommand

from core.models import OW_USER_ID_SYSTEM, PatientIdentifier


class Command(BaseCommand):
    help = "Link patients to their Open Wearables users, from lines of '<system>|<value> <OW user id>' on stdin."
    stealth_options = ("stdin",)

    def handle(self, *args, **options):
        linked = 0
        for number, line in enumerate(options.get("stdin") or sys.stdin, start=1):
            if not line.strip():
                continue
            try:
                identifier, ow_user_id = line.split()
                system, value = identifier.rsplit("|", 1)
            except ValueError:
                self.stderr.write(f"line {number}: expected '<system>|<value> <OW user id>'")
                continue

            found = PatientIdentifier.objects.filter(system=system, value=value).select_related("patient").first()
            if found is None:
                self.stderr.write(f"line {number}: no patient has the identifier {identifier}")
                continue
            current = found.patient.get_ow_user_id()
            if current == ow_user_id:
                continue
            if current:
                self.stderr.write(f"line {number}: {identifier} is linked to another OW user already, kept")
                continue
            if PatientIdentifier.objects.filter(system=OW_USER_ID_SYSTEM, value=ow_user_id).exists():
                self.stderr.write(f"line {number}: OW user {ow_user_id} is linked to another patient, kept")
                continue

            PatientIdentifier.objects.create(patient=found.patient, system=OW_USER_ID_SYSTEM, value=ow_user_id)
            linked += 1

        self.stdout.write(f"Linked {linked} patients to their OW users.")
