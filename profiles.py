"""Engine profiles: the engine-specific numbers behind the analysis and guided tests.

The checks themselves (fuel trims, sensor response, charging...) apply to any Subaru with a front
wideband A/F sensor; what differs between engines is what counts as normal - idle airflow, the
airflow bands, and for turbo engines boost, knock and fuelling under boost. Pick a profile with
`get(key)`, or let `detect()` choose from the logged channels.

Used by analyse.py, guided.py, logger.py and web/webapp.py.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

AUTO = "auto"


@dataclass(frozen=True)
class Profile:
    key: str
    name: str
    engines: str                       # which engines it covers, for the menu and findings
    litres: float
    turbo: bool = False
    warm_c: float = 80.0               # coolant considered fully warm
    cold_c: float = 70.0               # below this the engine is still warming up
    running_c: tuple[float, float] = (85.0, 95.0)   # typical coolant once warm
    hot_c: float = 105.0               # overheating
    idle_maf: tuple[float, float] = (2.5, 4.0)      # typical warm idle airflow, g/s
    maf_bands: tuple[float, float, float] = (5.0, 15.0, 40.0)   # idle | light | cruise | load, g/s
    c3_rest: float | None = -50.0      # where A/F Correction #3 rests (None: not known)
    leak_rpm: tuple[float, float] = (2200.0, 2900.0)  # the "2500 rpm" band in the leak test
    # turbo only
    boost_psi: float = 5.0             # above this the engine is "in boost"
    max_boost_afr: float = 12.0        # leaner than this under boost is dangerous
    redline_rpm: float = 6000.0        # approximate, for the rev gauge's red zone only

    @property
    def label(self) -> str:
        return f"{self.name} ({self.engines})"


PROFILES = [
    Profile("na25", "Naturally aspirated 2.5 L", "FB25, EJ25 non-turbo", 2.5),
    Profile("na20", "Naturally aspirated 2.0 L", "FB20, FA20 (BRZ/86), EJ20 non-turbo", 2.0,
            idle_maf=(2.0, 3.5), maf_bands=(4.0, 12.0, 32.0), redline_rpm=6500.0),
    Profile("turbo", "Turbo", "EJ20/EJ25 turbo, FA20DIT, FA24DIT", 2.5, turbo=True,
            idle_maf=(3.0, 6.0), maf_bands=(5.0, 20.0, 60.0), c3_rest=None, redline_rpm=6500.0),
]
BY_KEY = {p.key: p for p in PROFILES}
DEFAULT = BY_KEY["na25"]

# Only turbo ECUs report a boost control solenoid, so this is a safe giveaway.
TURBO_CHANNELS = ("Primary Wastegate Duty Cycle", "Secondary Wastegate Duty Cycle")


def detect(series: dict[str, np.ndarray]) -> tuple[Profile, str]:
    """Best guess from the logged channels -> (profile, how it was decided)."""
    for name in TURBO_CHANNELS:
        if name in series and np.isfinite(series[name]).any():
            return BY_KEY["turbo"], f"the ECU reports {name.lower()}"
    mrp = series.get("Manifold Relative Pressure")
    if mrp is not None:
        mrp = np.asarray(mrp, float)
        if np.count_nonzero(mrp > 3.0) >= 10:          # an NA engine can't go above atmospheric
            return BY_KEY["turbo"], "the manifold went above atmospheric pressure (boost)"
    return DEFAULT, "no turbo channels or boost; pick 2.0 L under Engine profile if that's yours"


def resolve(choice: str | None, series: dict[str, np.ndarray]) -> tuple[Profile, str]:
    """choice is a profile key, or "auto"/None to detect. Returns (profile, note)."""
    if choice and choice != AUTO:
        if choice in BY_KEY:
            return BY_KEY[choice], "chosen under Engine profile"
    p, why = detect(series)
    return p, f"auto-detected: {why}"


def get(choice: str | None, series: dict[str, np.ndarray] | None = None) -> Profile:
    return resolve(choice, series or {})[0]


def choices() -> list[tuple[str, str]]:
    """(key, label) for menus, starting with Automatic."""
    return [(AUTO, "Automatic")] + [(p.key, p.label) for p in PROFILES]
