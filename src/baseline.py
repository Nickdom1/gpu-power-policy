"""ROM-derived defaults and conservative comparisons with the pinned RM ABI."""

import hashlib
import json
import re
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import subprocess

import policy

FIELDS = {
    "powerCeilingWatts": "power_max_w",
    "powerDefaultWatts": "power_default_w",
    "currentCeilingMilliamps": "nvvdd_output_ocp_max_ma",
    "currentRatedMilliamps": "nvvdd_output_ocp_rated_ma",
}
SEEDS = ("powerDefaultWatts", "currentRatedMilliamps")
CEILINGS = ("powerCeilingWatts", "currentCeilingMilliamps")
FAMILIES = {"ampere", "ada", "turing"}


def editor(command, *args):
    try:
        result = subprocess.run([command, *map(str, args)], capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise ValueError("nvbios command not found; rerun the stock install (pinned editor required)") from exc
    try:
        report = json.loads(result.stdout)
    except ValueError as exc:
        raise ValueError("nvbios produced no JSON: " + result.stderr.strip()) from exc
    if result.returncode or not report.get("ok"):
        raise ValueError(f"nvbios {args[0]} failed: {report.get('error', result.stderr)}")
    return report["result"]


def whole(value, name):
    if (
        isinstance(value, bool)
        or not isinstance(value, (float, int))
        or not 0 < value <= 4294967
        or int(value) != value
    ):
        raise ValueError(f"{name} must be a positive whole W/mA value; refusing to round {value!r}")
    return int(value)


def read_rom(path, command="nvbios"):
    report = editor(command, "inspect", path)
    family = report["architecture"]["family"]
    if family not in FAMILIES:
        raise ValueError(f"unsupported VBIOS family {family!r}; only Turing, Ampere and Ada are handled")
    fields_report = editor(command, "fields", path)
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if report["sha256"] != digest or fields_report["sha256"] != digest:
        raise ValueError("ROM changed during inspection; retry with an unchanged file")
    fields = {field["key"]: field for field in fields_report["fields"]}
    values, editable = {}, {}
    for key, name in FIELDS.items():
        field = fields.get(name)
        if field is None and key == "currentRatedMilliamps":
            # Ada exposes the rated value as read-only raw bounds on the current ceiling.
            current = fields.get(FIELDS["currentCeilingMilliamps"], {})
            bounds = current.get("raw_bounds", {})
            if bounds.get("units") == "mA" and "rated" in bounds:
                field = {"current": bounds["rated"], "editable": False}
        if field is None:
            raise ValueError(f"ROM has no recognized {name}; refusing to guess a boot default or ceiling")
        values[key] = whole(field["current"], name)
        editable[key] = field.get("editable") is True
    indices = {}
    for name, key in (("power", "power_default_w"), ("current", "nvvdd_output_ocp_max_ma")):
        rows = fields[key].get("policy_indices", [])
        if len(rows) != 1 or type(rows[0]) is not int or not 0 <= rows[0] < 32:
            raise ValueError(f"ROM {name} policy index is ambiguous; refusing this layout")
        indices[name] = rows[0]
    if any(values[seed] > values[ceiling] for seed, ceiling in zip(SEEDS, CEILINGS, strict=True)):
        raise ValueError("ROM rated/default values exceed its ceilings")
    return {
        "family": family,
        "pci_device": report["pci_device"].lower(),
        "sha256": digest,
        "values": values,
        "policies": indices,
        "editable": editable,
        "notebook": fields["power_default_w"].get("notebook_tgp") is True,
        "version": (
            match[0].decode()
            if (match := re.search(rb"[0-9A-Fa-f]{2}(?:\.[0-9A-Fa-f]{2}){4}", Path(path).read_bytes()))
            else None
        ),
    }


def validate(rom):
    if not isinstance(rom, dict) or not isinstance(rom.get("family"), str) or rom["family"] not in FAMILIES:
        raise ValueError("invalid ROM baseline family")
    for name, keys in (("values", FIELDS), ("policies", ("power", "current"))):
        if not isinstance(rom.get(name), dict) or any(key not in rom[name] for key in keys):
            raise ValueError(f"ROM baseline missing required {name}")
    for key, pattern in (("pci_device", r"[0-9a-f]{4}:[0-9a-f]{4}"), ("sha256", r"[0-9a-f]{64}")):
        if not isinstance(rom.get(key), str) or not re.fullmatch(pattern, rom[key]):
            raise ValueError(f"ROM baseline missing or invalid {key}")
    for key, value in rom["values"].items():
        if key in FIELDS:
            whole(value, key)
    if any(type(value) is not int or not 0 <= value < 32 for value in rom["policies"].values()):
        raise ValueError("invalid ROM baseline policy indices")
    if any(rom["values"][seed] > rom["values"][ceiling] for seed, ceiling in zip(SEEDS, CEILINGS, strict=True)):
        raise ValueError("ROM baseline defaults exceed its ceilings")
    return rom


def amount(value, stock, *, step=1):
    """Percentages refer to ROM maximum; round to whole W or whole A, half up."""
    if isinstance(value, str) and re.fullmatch(r"\+\d+(?:\.\d+)?%", value):
        number = Decimal(stock) * (1 + Decimal(value[1:-1]) / 100)
        return int((number / step).quantize(Decimal(1), rounding=ROUND_HALF_UP)) * step
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        value = int(value)
    return whole(value, "value")


def propose(rom, config):
    stock = rom["values"]
    values = dict(stock)
    for key in CEILINGS:
        requested = config.get(key)
        value = (
            stock[key]
            if requested is None
            else amount(requested, stock[key], step=1000 if key.startswith("current") else 1)
        )
        kind, unit = ("current", "mA") if key.startswith("current") else ("power", "W")
        if value < stock[key]:
            raise ValueError(
                f"{kind} {value} {unit} is below ROM stock maximum {stock[key]} {unit}. "
                f"Ceilings cannot go below stock; use sudo gpu-power {kind} {value} at runtime."
            )
        if rom["family"] == "ada" and kind == "current" and requested is not None:
            raise ValueError(
                "Ada: the current ceiling is read-only in the pinned editor because firmware "
                "enforcement is unproven; this release has no current control on Ada. "
                "Use --power-ceiling only."
            )
        if value > stock[key]:
            if rom["family"] == "ada" and rom.get("notebook"):
                raise ValueError(
                    "Ada laptop: the editor cannot relocate this image under the 512 KiB GSP limit; "
                    "raising is not available. Stock-range controls still work."
                )
            if not rom["editable"][key]:
                raise ValueError(f"{key} is read-only in the pinned editor for this ROM")
            if value * 10 > stock[key] * 11 and config.get("beyondTested") is not True:
                percent = (value / stock[key] - 1) * 100
                raise ValueError(
                    f"{kind} +{percent:.1f}% is past the +10% cap. "
                    "Nothing past the cap has run on any board. To proceed anyway add --beyond-tested."
                )
        values[key] = value
    return values


def compare(rom, configured, info):
    validate(rom)
    rows = {row["index"]: row for row in policy.table(policy.INFO, info)}
    live = {}
    for name, cls, seed, ceiling, scale in (
        ("power", 0, SEEDS[0], CEILINGS[0], 1000),
        ("current", 15, SEEDS[1], CEILINGS[1], 1),
    ):
        row = rows.get(rom["policies"][name])
        if row is None or row["cls"] != cls:
            raise ValueError(f"live {name} policy is absent or has an unsupported class; refusing this layout")
        low, rated, high, _ = row["words"]
        if not 0 < low <= rated <= high:
            raise ValueError(f"live {name} policy bounds are inconsistent")
        live[seed], live[ceiling] = rated / scale, high / scale
    expected = {key: configured.get(key, rom["values"][key]) for key in FIELDS}
    return {
        "configured": expected,
        "rom": rom["values"],
        "live": live,
        "match": live == expected,
        "seeds_match": all(live[k] == rom["values"][k] == expected[k] for k in SEEDS),
    }


def require_match(result):
    if not result["match"] or not result["seeds_match"]:
        differences = [
            f"{key}: ROM {result['rom'][key]}, configured {result['configured'][key]}, live {result['live'][key]}"
            for key in FIELDS
            if result["live"][key] != result["configured"][key]
            or (key in SEEDS and result["live"][key] != result["rom"][key])
        ]
        raise ValueError("ROM/live baseline mismatch; boot the stock install (vbios=null). " + "; ".join(differences))
