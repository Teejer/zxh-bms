"""High level "read everything" helpers built on transport + protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from . import protocol as p
from .transport import BmsTransport


@dataclass
class Snapshot:
    label: p.LabelInfo
    instrument: p.Instrument
    cell_voltages_mv: List[int]
    basic: p.BasicInfo
    temps: p.TempProbes
    protect_counters: Optional[p.ProtectCounters] = None
    manufacture_date: Optional[str] = None
    firmware_version: Optional[str] = None

    @property
    def pack_voltage_v(self) -> float:
        return sum(self.cell_voltages_mv) / 1000.0

    @property
    def min_cell_mv(self) -> int:
        return min(self.cell_voltages_mv)

    @property
    def max_cell_mv(self) -> int:
        return max(self.cell_voltages_mv)

    @property
    def cell_delta_mv(self) -> int:
        return self.max_cell_mv - self.min_cell_mv


async def read_package_number(transport: BmsTransport) -> int:
    resp = await transport.transact(p.req_package_number(), 0, p.FUNC_READ_PARAM)
    return p.decode_package_number(resp[4:-2])


async def read_label(transport: BmsTransport, address: int) -> p.LabelInfo:
    resp = await transport.transact(p.req_label_info(address), address, p.FUNC_READ_PARAM)
    return p.decode_label_info(resp[4:-2])


async def read_instrument(transport: BmsTransport, address: int) -> p.Instrument:
    resp = await transport.transact(p.req_instrument(address), address, p.FUNC_READ_STATUS)
    return p.decode_instrument(resp[4:-2])


async def read_cells(transport: BmsTransport, address: int, cell_count: int) -> List[int]:
    voltages: List[int] = []
    full_pages, remainder = divmod(cell_count, 7)
    for page in range(full_pages):
        resp = await transport.transact(
            p.req_cells_page(address, page, 7), address, p.FUNC_READ_STATUS
        )
        voltages += p.decode_cells_page(resp[4:-2], 7)
    if remainder:
        resp = await transport.transact(
            p.req_cells_page(address, full_pages, remainder), address, p.FUNC_READ_STATUS
        )
        voltages += p.decode_cells_page(resp[4:-2], remainder)
    return voltages


async def read_basic(transport: BmsTransport, address: int) -> p.BasicInfo:
    resp = await transport.transact(p.req_basic_info(address), address, p.FUNC_READ_STATUS)
    return p.decode_basic_info(resp[4:-2])


async def read_temps(transport: BmsTransport, address: int) -> p.TempProbes:
    resp = await transport.transact(p.req_temp_probes(address), address, p.FUNC_READ_STATUS)
    return p.decode_temp_probes(resp[4:-2])


async def read_protect_counters(transport: BmsTransport, address: int) -> p.ProtectCounters:
    r1 = await transport.transact(
        p.req_protect_counters_1(address), address, p.FUNC_READ_STATUS
    )
    r2 = await transport.transact(
        p.req_protect_counters_2(address), address, p.FUNC_READ_STATUS
    )
    return p.decode_protect_counters(r1[4:-2], r2[4:-2])


async def read_manufacturer(transport: BmsTransport, address: int) -> tuple[str, str]:
    resp = await transport.transact(p.req_manufacturer_1(address), address, p.FUNC_READ_PARAM)
    return p.decode_manufacturer_1(resp[4:-2])


async def read_snapshot(
    transport: BmsTransport,
    address: int,
    label: p.LabelInfo,
    include_protect_counters: bool = False,
    include_manufacturer: bool = False,
) -> Snapshot:
    instrument = await read_instrument(transport, address)
    cells = await read_cells(transport, address, label.cell_count)
    basic = await read_basic(transport, address)
    temps = await read_temps(transport, address)
    protect = await read_protect_counters(transport, address) if include_protect_counters else None
    date = fw = None
    if include_manufacturer:
        date, fw = await read_manufacturer(transport, address)
    return Snapshot(
        label=label,
        instrument=instrument,
        cell_voltages_mv=cells,
        basic=basic,
        temps=temps,
        protect_counters=protect,
        manufacture_date=date,
        firmware_version=fw,
    )
