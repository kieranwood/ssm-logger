"""Standard SSM parameter definitions, loaded from RomRaider's log_defs.xml.

The file is not part of this repo (RomRaider is GPL). Get it with:
    .venv/bin/python fetch_defs.py

Only the "SSM base" set is used: fixed addresses every Subaru ECU of this era
answers on. Whether a particular ECU supports a parameter is given by a bit in
its init reply: RomRaider's byte="N" bit="M" means init[N + 7] & (1 << (M - 1)),
where init is the reply data after the 0xEA response byte.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

# Searched in order; SSM_LOG_DEFS (environment variable) overrides all of them.
DOWNLOADED_DEFS = Path(__file__).resolve().parent / "definitions" / "log_defs.xml"   # fetch_defs.py
ROMRAIDER_CHECKOUT_DEFS = Path.home() / "source/RomRaider/definitions/log_defs.xml"


class DefinitionsMissing(FileNotFoundError):
    pass


def find_defs() -> Path:
    if env := os.environ.get("SSM_LOG_DEFS"):
        return Path(env).expanduser()
    for path in (DOWNLOADED_DEFS, ROMRAIDER_CHECKOUT_DEFS):
        if path.is_file():
            return path
    raise DefinitionsMissing(
        "RomRaider's log_defs.xml was not found. Download it with:\n"
        "    .venv/bin/python fetch_defs.py\n"
        "or point the SSM_LOG_DEFS environment variable at your own copy.")

_SAFE_EXPR = re.compile(r"^[\d\s.+\-*/()x]+$")

# log_defs.xml has a mangled degree sign ("B") and a few wrong units.
_UNIT_FIXES = {"B C": "°C", "B BTDC": "° BTDC", "B ": "°", "volts": "V"}
_UNIT_OVERRIDES = {"Engine Load": "%"}


@dataclass(frozen=True)
class Parameter:
    name: str
    address: int
    length: int          # bytes
    signed: bool
    expr: str            # Python expression in x
    units: str
    decimals: int
    support_byte: int
    support_bit: int
    desc: str
    switch: bool = False  # on/off flag: one bit of the byte at `address`

    @property
    def switch_bit(self) -> int:
        # For switches RomRaider uses the same 1-based bit for support and data.
        return self.support_bit - 1

    def convert(self, raw: int) -> float:
        return eval(self.expr, {"__builtins__": {}}, {"x": raw})  # validated in load()

    def addresses(self) -> list[int]:
        return [self.address + i for i in range(self.length)]

    def value(self, raw_bytes: bytes) -> float:
        if self.switch:
            return float((raw_bytes[0] >> self.switch_bit) & 1)
        return self.convert(int.from_bytes(raw_bytes, "big", signed=self.signed))

    def supported_by(self, init: bytes) -> bool:
        idx = self.support_byte + 7
        return idx < len(init) and bool(init[idx] & (1 << (self.support_bit - 1)))

    @property
    def label(self) -> str:
        return f"{self.name} ({self.units})" if self.units else self.name


def load(path: Path | str | None = None) -> list[Parameter]:
    path = path or find_defs()
    root = ET.parse(path).getroot()
    base = root.find(".//logprotocol[@type='SSM']/ecu[@type='ssmbase']")
    if base is None:
        raise ValueError(f"No SSM base parameter set in {path}")
    params = []
    for el in base.findall("parameter"):
        a = el.attrib
        storage = a.get("storagetype", "uint8")
        m = re.fullmatch(r"(u?)int(8|16|32)", storage)
        expr = a.get("expr", "[value]").replace("[value]", "x")
        if not m or not _SAFE_EXPR.match(expr) or "byte" not in a or "bit" not in a:
            continue   # skip anything we can't evaluate safely (none in the base set today)
        params.append(Parameter(
            name=a["id"],
            address=int(a["offset"].lstrip("#"), 16),
            length=int(m.group(2)) // 8,
            signed=m.group(1) == "",
            expr=expr,
            units=_UNIT_OVERRIDES.get(a["id"], _UNIT_FIXES.get(a.get("metric", ""), a.get("metric", ""))),
            decimals=int(a.get("decimals", "2")),
            support_byte=int(a["byte"]),
            support_bit=int(a["bit"]),
            desc=a.get("desc", "").replace("|", " "),
            switch=a.get("type") == "bool",
        ))
    return params


def request_addresses(params: list[Parameter]) -> list[int]:
    """Unique addresses to read for these parameters (switches share bytes)."""
    seen: dict[int, None] = {}
    for p in params:
        for a in p.addresses():
            seen.setdefault(a)
    return list(seen)


def decode(params: list[Parameter], addresses: list[int], data: bytes) -> list[float]:
    """Convert a read-address reply (after the 0xE8 byte) to values, in order."""
    by_addr = dict(zip(addresses, data))
    return [p.value(bytes(by_addr[a] for a in p.addresses())) for p in params]
