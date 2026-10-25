"""Verify FurMark placement on the laptop panel."""

import json
from run import Stop
from witness import display_config


def panel_geometry(witness):
    window = witness["window"]
    g = window["geometry"]
    panels = [d for d in witness["displays"] if d["name"].startswith("eDP")]
    if not window["fullscreen"] or len(panels) != 1:
        raise Stop("window not fullscreen or no unique laptop-panel geometry")
    p = panels[0]
    if p["rotation"] != 1:
        raise Stop("rotated panel mapping not supported by this diagnostic")
    width = p["size"]["width"] / p["scale"]
    height = p["size"]["height"] / p["scale"]
    # This is a consistency check in logical desktop coordinates, not a Vulkan extent measurement.
    if any(
        abs(a - b) > 2
        for a, b in [
            (g["x"], p["pos"]["x"]),
            (g["y"], p["pos"]["y"]),
            (g["width"], width),
            (g["height"], height),
        ]
    ):
        raise Stop(
            "FurMark geometry does not match the laptop panel logical bounds; return artifacts before retrying"
        )
    return g


def validate_witness(telemetry):
    root = telemetry / "witness"
    if not json.loads((root / "result.json").read_text()).get("complete"):
        raise Stop("desktop witness incomplete")
    samples = [
        json.loads((root / ("geometry-%ds" % d) / "result.json").read_text())
        for d in (30, 90)
    ]
    geometry = [panel_geometry(s) for s in samples]
    configs = [
        display_config(
            json.loads(
                (telemetry / "snapshots" / ("kscreen-" + s + ".json")).read_text()
            )
        )
        for s in ("start", "end")
    ]
    if (
        configs[0] != configs[1]
        or any(s["displays"] != configs[0] for s in samples)
        or geometry[0] != geometry[1]
    ):
        raise Stop("window geometry or active display modes changed during workload")
    return dict(geometry=geometry[0], displays=configs[0])
