"""A patient leaving an organization (POST /api/v1/patients/{id}/leave_organization)."""

from rest_framework.test import APIClient

from core.models import Observation, Organization, Patient, PatientOrganization, StudyPatient, StudyPatientScopeConsent

from .utils import Code, add_observations, add_patient_to_study, add_patients, create_study


def _as(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def _leave(client, patient, organization_id):
    return client.post(f"/api/v1/patients/{patient.id}/leave_organization?organization_id={organization_id}")


def test_a_patient_leaves_one_organization_and_keeps_everything_else(api_client, user, organization, patient, hr_study):
    other_org = Organization.objects.create(name="Other Org", type="other")
    other_study = create_study("other", organization=other_org, codes=[Code.HeartRate])
    add_patient_to_study(patient, other_study)
    add_observations(patient, Code.HeartRate, 3)

    r = _leave(_as(patient.jhe_user), patient, organization.id)

    assert r.status_code == 204, r.text
    assert list(patient.organizations.all()) == [other_org]
    assert [sp.study for sp in StudyPatient.objects.filter(patient=patient)] == [other_study]
    assert [c.study_patient.study for c in StudyPatientScopeConsent.objects.filter(study_patient__patient=patient)] == [
        other_study
    ]
    assert Observation.objects.filter(subject_patient=patient).count() == 3
    # the organization's practitioners no longer reach the patient
    assert not Patient.practitioner_authorized(user.id, patient.id)
    assert api_client.get(f"/api/v1/patients/{patient.id}/consents").status_code == 403


def test_leaving_the_last_organization_keeps_the_patient_and_their_readings(organization, patient, hr_study):
    add_observations(patient, Code.HeartRate, 2)

    assert _leave(_as(patient.jhe_user), patient, organization.id).status_code == 204

    assert Patient.objects.filter(pk=patient.pk).exists()
    assert Observation.objects.filter(subject_patient=patient).count() == 2
    assert not PatientOrganization.objects.filter(patient=patient).exists()


def test_only_the_patient_can_leave(api_client, organization, patient):
    [someone_else] = add_patients(1, organization=organization)

    assert _leave(api_client, patient, organization.id).status_code == 403
    assert _leave(_as(someone_else.jhe_user), patient, organization.id).status_code == 403
    assert PatientOrganization.objects.filter(patient=patient, organization=organization).exists()


def test_leaving_needs_an_organization_and_can_be_repeated(organization, patient):
    client = _as(patient.jhe_user)
    assert client.post(f"/api/v1/patients/{patient.id}/leave_organization").status_code == 400
    assert _leave(client, patient, organization.id).status_code == 204
    assert _leave(client, patient, organization.id).status_code == 204
