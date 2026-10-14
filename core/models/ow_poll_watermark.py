from django.db import models


class OwPollWatermark(models.Model):
    """How far ``ow_poll`` has read one patient's samples of one type, by when OW stored them.

    ``read_until`` is when the last complete read began: everything OW had stored by then
    has been read, whenever it was recorded. A read that missed a page leaves it where it was.
    """

    patient = models.ForeignKey("Patient", on_delete=models.CASCADE, related_name="ow_poll_watermarks")
    ow_type = models.CharField()
    read_until = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["patient", "ow_type"], name="core_owpollwatermark_unique_patient_type")
        ]
