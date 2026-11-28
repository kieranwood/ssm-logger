"""Check the .raw files test_drive_log made: the firmware read the same addresses defs.py would,
and ssmraw.py turns them into CSVs the logger loads with the fake ECU's values.

    python3 test_ssmraw.py DIR      (DIR: where test_drive_log wrote its files)
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import defs  # noqa: E402
import recording  # noqa: E402
import ssmraw  # noqa: E402
from sources import DEMO_INIT  # noqa: E402  (the fake ECU's init reply)

d = Path(sys.argv[1])
params = [p for p in defs.load() if p.supported_by(DEMO_INIT)]
want = defs.request_addresses(params)
out = Path(tempfile.mkdtemp())
for name, samples, truncated, gap in (("drive_0001.raw", 800, False, False),
                                      ("drive_0003.raw", 600, False, True),
                                      ("drive_0009.raw", 799, True, False)):
    drive = ssmraw.parse((d / name).read_bytes())
    assert drive.init == DEMO_INIT, name
    assert drive.addresses == want, f"{name}: firmware read different addresses than defs.py"
    assert len(drive.samples) == samples and drive.truncated == truncated, (name, len(drive.samples), drive.truncated)
    t = [s[0] for s in drive.samples]
    assert all(b > a for a, b in zip(t, t[1:])), name
    biggest = max(b - a for a, b in zip(t, t[1:]))
    assert (biggest > 3) == gap, (name, biggest)
    rec = recording.load_csv(ssmraw.to_csv(drive, out, name[:-4]))
    v = {c.name: rec.y[i] for i, c in enumerate(rec.channels)}
    assert rec.info == "ECU 7152345007", rec.info
    assert len(rec.t) == samples
    assert (v["Engine Speed"] == 1500).all() and (v["Coolant Temperature"] == 90).all()
    assert abs(v["Battery Voltage"] - 14.32).max() < 0.01
    print(f"ok: {name}: {len(rec.channels)} channels, {samples} rows, {t[-1]:.1f} s, largest gap {biggest:.1f} s")
print(f"ok: the firmware's {len(want)} addresses match defs.request_addresses exactly")
