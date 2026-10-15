"""Name the device or app a reading came from, as the name of its DataSource.

Ingestion paths know where a reading came from in different vocabularies: Open Wearables
reports a provider, a free-text source name and a hardware model; an Apple Health export
reports a source name and an HKDevice description. ``source_label`` turns any of those into one short, shared name
("Apple Watch", "Garmin", "Google Health"). The same device on the same path shares a label,
two devices never share one, and data an app relays through a phone platform names that path
("Garmin via Apple Health"), so it is never mistaken for, or added to, the same vendor's cloud
connection.

Labels are shared across people, so a label must never carry a person's name. HealthKit names
a person's own devices after them ("Jan's Apple Watch", or just "JAN" for an iPhone); those
are recognised by their Apple hardware and filed under the device class instead.
"""

import re

# Cloud connectors in Open Wearables. The provider *is* the source.
PROVIDER_LABELS = {
    "garmin": "Garmin",
    "oura": "Oura",
    "polar": "Polar",
    "suunto": "Suunto",
    "whoop": "WHOOP",
    "fitbit": "Fitbit",
    "google_health": "Google Health",
    "ultrahuman": "Ultrahuman",
    "strava": "Strava",
}

# Platforms that relay other apps' data. A known app arriving through one is named "<vendor> via
# <platform>"; with nothing more specific known, the platform itself is the label.
PLATFORM_LABELS = {
    "apple": "Apple Health",
    "health_connect": "Health Connect",
    "samsung": "Samsung Health",
}

# Apps and makers that write into Apple Health or Health Connect, keyed by normalised name.
# Each maps to its vendor, so the Garmin Connect app and a Garmin watch's maker both give
# "Garmin", which becomes "Garmin via Apple Health" when it arrives through a platform.
KNOWN_SOURCES = {
    "connect": "Garmin",
    "garmin connect": "Garmin",
    "garmin": "Garmin",
    "oura": "Oura",
    "health mate": "Withings",
    "withings": "Withings",
    "fitbit": "Fitbit",
    "google health": "Google Health",
    "google fit": "Google Fit",
    "polar flow": "Polar",
    "polar": "Polar",
    "whoop": "WHOOP",
    "suunto": "Suunto",
    "coros": "COROS",
    "zepp": "Zepp",
    "zepp life": "Zepp",
    "samsung health": "Samsung Health",
    "strava": "Strava",
    "ultrahuman": "Ultrahuman",
    "eight sleep": "Eight Sleep",
    "dexcom": "Dexcom",
    "dexcom g6": "Dexcom",
    "dexcom g7": "Dexcom",
    "librelink": "FreeStyle Libre",
    "freestyle libre": "FreeStyle Libre",
    "omron connect": "Omron",
    "qardio": "Qardio",
    "bluetooth device": "Bluetooth device",
}

# Apple hardware and model ids start with the device class ("Watch6,17", "iPhone16,2",
# "Watch" in an HKDevice); a device *name* has to say "Apple Watch" to count, so a
# "Galaxy Watch" is never mistaken for one.
APPLE_MODEL_PREFIXES = (
    ("watch", "Apple Watch"),
    ("iphone", "iPhone"),
    ("ipad", "iPad"),
    ("airpods", "AirPods"),
)
APPLE_NAME_WORDS = (
    ("apple watch", "Apple Watch"),
    ("iphone", "iPhone"),
    ("ipad", "iPad"),
    ("airpods", "AirPods"),
)
OW_DEVICE_TYPES = {"watch": "Apple Watch", "phone": "iPhone"}

MAX_LABEL_LENGTH = 60
# Apple's own writer for a sensor paired to the phone, not an app relaying a vendor's data.
BLUETOOTH_DEVICE = "Bluetooth device"


def _normalise(value):
    """Lower-case, typographic apostrophes and no-break spaces flattened, runs of space collapsed."""
    if not value:
        return ""
    value = str(value).replace("\u2019", "'").replace("\u00a0", " ")
    return re.sub(r"\s+", " ", value).strip().lower()


def _apple_model(model):
    model = _normalise(model)
    return next((label for prefix, label in APPLE_MODEL_PREFIXES if model.startswith(prefix)), None)


def _apple_name(name):
    name = _normalise(name)
    return next((label for word, label in APPLE_NAME_WORDS if word in name), None)


def _without_owner(name):
    """ "Jan's Scale" -> "Scale": the device name without the person it is named after."""
    text = re.sub(r"\s+", " ", str(name).replace("\u2019", "'").replace("\u00a0", " ")).strip()
    return re.sub(r"^.+?'s\s+", "", text).strip()


def _via(vendor, provider):
    """A vendor's data relayed through a platform is named after both."""
    platform = PLATFORM_LABELS.get(provider)
    if platform and vendor not in (BLUETOOTH_DEVICE, platform):
        return f"{vendor} via {platform}"
    return vendor


def source_label(*, provider=None, source_name=None, device_model=None, device_type=None, manufacturer=None):
    """The shared name for where a reading came from. Never empty."""
    provider = _normalise(provider)
    if provider in PROVIDER_LABELS:
        return PROVIDER_LABELS[provider]

    known = KNOWN_SOURCES.get(_normalise(source_name))
    if known:
        return _via(known, provider)

    maker = _normalise(manufacturer)
    if maker in KNOWN_SOURCES:
        # A device written into Apple Health by its maker's app, e.g. a Garmin watch.
        return _via(KNOWN_SOURCES[maker], provider)
    if provider == "apple" or maker.startswith("apple"):
        device = (
            _apple_model(device_model)
            or _apple_name(source_name)
            or (OW_DEVICE_TYPES.get(_normalise(device_type)) if provider == "apple" else None)
        )
        if device:
            return device

    if source_name:
        device = _apple_name(source_name)
        if device:
            return device
        name = _without_owner(source_name)
        if name:
            return name[:MAX_LABEL_LENGTH]

    return PLATFORM_LABELS.get(provider, "Unknown source")


def provider_of(label):
    """The provider a label's readings come in through: a cloud account, or a phone platform.

    Labels are per path, so a live connection and an export of it (an Apple Health export, a
    Garmin account export) share their labels and map to the same provider. A label naming no
    cloud account and no platform is Apple's hardware ("Apple Watch"), Apple's "Bluetooth
    device", or an app that named itself; those arrive through Apple Health.
    """
    for provider, name in PROVIDER_LABELS.items():
        if label == name:
            return provider
    for provider, name in PLATFORM_LABELS.items():
        if label == name or label.endswith(f" via {name}"):
            return provider
    return "apple"
