"""Turning a reader.Snapshot into the flat dict / text used by both the CLI
and the MQTT bridge, so there's one place that defines what fields exist."""

from __future__ import annotations

from datetime import datetime, timezone

from . import reader


def snapshot_to_dict(snap: reader.Snapshot) -> dict:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pack_voltage_v": round(snap.pack_voltage_v, 3),
        "current_a": snap.instrument.current_a,
        "power_w": round(snap.pack_voltage_v * snap.instrument.current_a, 1),
        "soc_pct": snap.instrument.soc_pct,
        "cell_count": snap.label.cell_count,
        "cell_voltages_mv": snap.cell_voltages_mv,
        "min_cell_mv": snap.min_cell_mv,
        "max_cell_mv": snap.max_cell_mv,
        "cell_delta_mv": snap.cell_delta_mv,
        "balancing_cells": snap.instrument.balancing_cells,
        "mos_temp_c": snap.instrument.mos_temp_c,
        "probe_temps_c": snap.temps.values_c,
        "active_protections": snap.instrument.active_protections,
        "cycle_count": snap.basic.cycle_count,
        "health_pct": snap.basic.health_pct,
        "nominal_capacity_ah": snap.label.nominal_capacity_ah,
        "remaining_capacity_ah": round(
            snap.label.full_capacity_ah * snap.instrument.soc_pct / 100.0, 2
        ),
    }


def format_text(snap: reader.Snapshot) -> str:
    lines = []
    lines.append(f"Pack:      {snap.pack_voltage_v:6.3f} V   {snap.instrument.current_a:+7.3f} A   "
                  f"{snap.pack_voltage_v * snap.instrument.current_a:+7.1f} W   SOC {snap.instrument.soc_pct}%")
    lines.append(f"Cells:     {snap.label.cell_count} cells, "
                  f"min {snap.min_cell_mv} mV / max {snap.max_cell_mv} mV / delta {snap.cell_delta_mv} mV")
    cell_str = "  ".join(f"{i+1}:{mv/1000:.3f}" for i, mv in enumerate(snap.cell_voltages_mv))
    lines.append(f"  {cell_str}")
    if snap.instrument.balancing_cells:
        lines.append(f"  balancing: {snap.instrument.balancing_cells}")
    temps = ", ".join(
        f"T{i+1}={t:.1f}C" if t is not None else f"T{i+1}=--"
        for i, t in enumerate(snap.temps.values_c)
    )
    mos = f"{snap.instrument.mos_temp_c:.1f}C" if snap.instrument.mos_temp_c is not None else "--"
    lines.append(f"Temps:     {temps}   MOS={mos}")
    lines.append(f"Health:    cycles={snap.basic.cycle_count}  health={snap.basic.health_pct}%  "
                  f"capacity={snap.basic.capacity_ah}Ah")
    if snap.instrument.active_protections:
        lines.append(f"PROTECTION ACTIVE: {', '.join(snap.instrument.active_protections)}")
    else:
        lines.append("Protection: normal")
    return "\n".join(lines)
