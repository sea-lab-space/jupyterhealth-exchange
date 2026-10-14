"""How a reading's source becomes the name of its DataSource."""

import pytest

from core.services.data_sources import source_label


@pytest.mark.parametrize(
    "fields, label",
    [
        # Open Wearables, as reported for a real iPhone + Apple Watch owner
        (
            {
                "provider": "apple",
                "source_name": "Yuang\u2019s Apple\u00a0Watch",
                "device_model": "Watch6,17",
            },
            "Apple Watch",
        ),
        (
            {
                "provider": "apple",
                "source_name": "YUANG",
                "device_model": "iPhone16,2",
                "device_type": "phone",
            },
            "iPhone",
        ),
        # a vendor's app relaying through a platform is named after both, whatever else is connected
        (
            {"provider": "apple", "source_name": "Google Health", "device_model": "iPhone16,2"},
            "Google Health via Apple Health",
        ),
        (
            {"provider": "apple", "source_name": "Connect", "device_model": "iPhone16,2"},
            "Garmin via Apple Health",
        ),
        (
            {
                "provider": "apple",
                "source_name": "Connect",
                "device_model": "Forerunner 965",
                "manufacturer": "Garmin",
            },
            "Garmin via Apple Health",
        ),
        (
            {"provider": "apple", "source_name": "Health Mate", "manufacturer": "Withings"},
            "Withings via Apple Health",
        ),
        (
            {"provider": "health_connect", "source_name": "Samsung Health"},
            "Samsung Health via Health Connect",
        ),
        # the platform's own app is not relayed
        ({"provider": "samsung", "source_name": "Samsung Health"}, "Samsung Health"),
        # Apple's writer for a sensor paired to the phone
        (
            {"provider": "apple", "source_name": "Bluetooth Device", "device_model": "Watch6,17"},
            "Bluetooth device",
        ),
        (
            {
                "provider": "apple",
                "source_name": "HRVEmotionPrototype",
                "device_model": "Watch6,17",
            },
            "Apple Watch",
        ),
        (
            {"provider": "suunto", "source_name": "suunto", "device_model": "Suunto Vertical"},
            "Suunto",
        ),
        # the Google Health API merges every source behind the account and names none of them
        ({"provider": "google_health", "source_name": "google_health_api"}, "Google Health"),
        ({"provider": "garmin", "source_name": "garmin"}, "Garmin"),
        ({"provider": "health_connect"}, "Health Connect"),
        # a sample whose data-source metadata could not be fetched
        ({"source_name": "garmin"}, "Garmin"),
        ({"source_name": "Connect"}, "Garmin"),
        # Apple Health export: HKDevice manufacturer and model
        (
            {
                "manufacturer": "Apple Inc.",
                "device_model": "Watch",
                "source_name": "Jan\u2019s Apple Watch",
            },
            "Apple Watch",
        ),
        ({"manufacturer": "Apple Inc.", "device_model": "iPhone", "source_name": "JAN"}, "iPhone"),
        (
            {"manufacturer": "Garmin", "device_model": "Forerunner 965", "source_name": "Connect"},
            "Garmin",
        ),
        ({"manufacturer": "Withings", "source_name": "Health Mate"}, "Withings"),
        # the maker decides when the writing app is not one we know, even inside Apple Health
        (
            {
                "provider": "apple",
                "manufacturer": "Garmin",
                "source_name": "Connect IQ",
                "device_model": "fenix 7",
            },
            "Garmin via Apple Health",
        ),
        ({"source_name": "MyFitnessPal"}, "MyFitnessPal"),
        # never mistake another maker's watch for an Apple Watch
        (
            {"provider": "samsung", "source_name": "Galaxy Watch7", "device_model": "SM-R960"},
            "Galaxy Watch7",
        ),
        # a person's name never becomes a label
        ({"source_name": "Jan\u2019s Scale"}, "Scale"),
        ({"provider": "apple"}, "Apple Health"),
        ({}, "Unknown source"),
    ],
)
def test_source_label(fields, label):
    assert source_label(**fields) == label
