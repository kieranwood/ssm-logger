"""Pre-trigger capture: automatically save the seconds around a fault.

Both the desktop logger and the browser version keep about an hour of live data in memory, so a
capture is a slice of that history. When a trigger fires at time T, the capture waits until
T + after_s and then saves T - before_s .. T + after_s as an ordinary recording, with a mark at T
and the reason in the file header. Recording doesn't need to be running.

Triggers:
  - rules on logged channels, e.g. total fuel trim above +25% for 2 s. A rule fires once when
    its condition has held for hold_s, then not again until the condition has cleared.
  - a new stored or pending fault code (the caller polls the ECU and calls codes()).
  - a manual "capture now".

Also: threshold alerts while logging (Alerter), which use the same rules.

Used by logger.py (desktop) and web/webapp.py (browser).
"""

from __future__ import annotations

import csv
import io
from dataclasses import asdict, dataclass, field

RUNNING_RPM = 400


@dataclass
class Rule:
    channel: str
    op: str                 # ">" or "<"
    value: float
    hold_s: float = 2.0
    enabled: bool = True

    def label(self) -> str:
        hold = f" for {self.hold_s:g} s" if self.hold_s else ""
        return f"{self.channel} {self.op} {self.value:g}{hold}"

    def test(self, v: float) -> bool:
        if v != v:          # NaN
            return False
        return v > self.value if self.op == ">" else v < self.value


def default_rules() -> list[Rule]:
    return [
        Rule("Total Fuel Trim", ">", 25, 2),
        Rule("Total Fuel Trim", "<", -25, 2),
        Rule("Coolant Temperature", ">", 105, 2, enabled=False),
        Rule("Battery Voltage", "<", 12.5, 5, enabled=False),
    ]


@dataclass
class Settings:
    rules: list[Rule] = field(default_factory=default_rules)
    on_new_code: bool = True      # trigger when a new stored or pending code appears
    before_s: float = 30.0
    after_s: float = 30.0
    running_only: bool = True     # ignore channel rules unless the engine is running

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Settings":
        d = dict(d)
        d["rules"] = [Rule(**r) for r in d.get("rules", [])]
        return cls(**d)


class RuleWatcher:
    """Evaluates rules sample by sample. A rule *fires* once when its condition has held for
    hold_s, and is *active* from then until the condition clears."""

    def __init__(self, rules: list[Rule], channel_names: list[str], running_only: bool = True):
        self.rules = rules
        self.col = {n: i for i, n in enumerate(channel_names)}
        self.rpm_col = self.col.get("Engine Speed")
        self.running_only = running_only
        self.since: dict[int, float] = {}     # rule index -> when its condition started holding
        self.latched: set[int] = set()        # rules that fired and haven't cleared yet

    def missing_channels(self) -> list[str]:
        return sorted({r.channel for r in self.rules if r.enabled and r.channel not in self.col})

    def update(self, t: float, row: list[float]) -> list[Rule]:
        """Feed one sample; returns the rules that fired on it."""
        running = (self.rpm_col is None or not self.running_only
                   or (row[self.rpm_col] or 0) > RUNNING_RPM)
        fired = []
        for i, rule in enumerate(self.rules):
            c = self.col.get(rule.channel)
            if not rule.enabled or c is None:
                continue
            v = row[c]
            if not running or v is None or not rule.test(v):
                self.since.pop(i, None)
                self.latched.discard(i)
                continue
            start = self.since.setdefault(i, t)
            if i not in self.latched and t - start >= rule.hold_s:
                self.latched.add(i)
                fired.append(rule)
        return fired

    def active(self) -> list[Rule]:
        return [self.rules[i] for i in sorted(self.latched)]


class Capturer:
    def __init__(self, settings: Settings, channel_names: list[str]):
        self.settings = settings
        self.watch = RuleWatcher(settings.rules, channel_names, settings.running_only)
        self.pending: tuple[float, str] | None = None
        self.known_codes: set[str] | None = None
        self.saved = 0

    def missing_channels(self) -> list[str]:
        return self.watch.missing_channels()

    def update(self, t: float, row: list[float]) -> None:
        for rule in self.watch.update(t, row):
            self.trigger(t, rule.label())

    def codes(self, t: float, codes: set[str]) -> None:
        """Report the stored + pending codes read from the ECU."""
        if self.known_codes is None:
            self.known_codes = set(codes)      # codes already there when logging started
            return
        new = codes - self.known_codes
        self.known_codes |= codes
        if new and self.settings.on_new_code:
            self.trigger(t, "new fault code " + ", ".join(sorted(new)))

    def trigger(self, t: float, reason: str) -> bool:
        """Start a capture at t unless one is already waiting to be saved."""
        if self.pending is not None:
            return False
        self.pending = (t, reason)
        return True

    def due(self, t_now: float) -> tuple[float, str] | None:
        """The (trigger time, reason) of a capture whose 'after' window has passed, once."""
        if self.pending and t_now >= self.pending[0] + self.settings.after_s:
            p, self.pending = self.pending, None
            self.saved += 1
            return p
        return None

    def status(self, t_now: float) -> str:
        if self.pending:
            left = self.pending[0] + self.settings.after_s - t_now
            return f"capturing ({self.pending[1]}) - saving in {max(0, left):.0f} s"
        return "armed" + (f", {self.saved} saved" if self.saved else "")


# ---------------------------------------------------------------- alerts
def default_alert_rules() -> list[Rule]:
    return [
        Rule("Coolant Temperature", ">", 105, 3),
        Rule("Battery Voltage", "<", 12.5, 5),
        Rule("Total Fuel Trim", ">", 30, 5, enabled=False),
        Rule("Engine Speed", ">", 6000, 0, enabled=False),
    ]


@dataclass
class AlertSettings:
    rules: list[Rule] = field(default_factory=default_alert_rules)
    sound: bool = True
    running_only: bool = True     # ignore rules unless the engine is running

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AlertSettings":
        d = dict(d)
        d["rules"] = [Rule(**r) for r in d.get("rules", [])]
        return cls(**d)


class Alerter:
    """Threshold alerts while logging: fired() for a sound, active() for the banner."""

    def __init__(self, settings: AlertSettings, channel_names: list[str]):
        self.settings = settings
        self.watch = RuleWatcher(settings.rules, channel_names, settings.running_only)

    def update(self, t: float, row: list[float]) -> list[str]:
        return [r.label() for r in self.watch.update(t, row)]

    def active(self) -> list[str]:
        return [r.label() for r in self.watch.active()]

    def missing_channels(self) -> list[str]:
        return self.watch.missing_channels()


def _g4(v: float) -> str:
    return "nan" if v is None else f"{v:.4g}"


def capture_csv(labels: list[str], ecu_id: str, t, columns, t_trigger: float, reason: str,
                settings: Settings, when: str) -> str:
    """CSV text (the logger's format) for before_s..after_s around t_trigger.

    t is the history's time array and columns a sequence of per-channel arrays (same length).
    """
    lo, hi = t_trigger - settings.before_s, t_trigger + settings.after_s
    idx = [i for i, tv in enumerate(t) if lo <= tv <= hi]
    out = io.StringIO()
    out.write(f"# ECU {ecu_id}\n")
    out.write(f"# Capture: {reason} at {when} ({settings.before_s:g} s before, {settings.after_s:g} s after)\n")
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["time_s", "mark"] + labels)
    if not idx:
        return out.getvalue()
    t0 = t[idx[0]]
    mark_i = next((i for i in idx if t[i] >= t_trigger), idx[-1])
    for i in idx:
        w.writerow([f"{t[i] - t0:.3f}", 1 if i == mark_i else ""] + [_g4(col[i]) for col in columns])
    return out.getvalue()


POLL_CODES_S = 10.0       # how often the apps check for new codes while armed


def poll_codes(request) -> set[str]:
    """Stored + pending codes (skips permanent codes: some ECUs never answer that request,
    which would stall logging for the timeout on every poll)."""
    import dtc
    return {c for r in dtc.read_all(request, modes=(0x03, 0x07)) for c in r.codes}
