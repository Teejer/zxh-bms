"""Wire protocol for this family of Modbus-like BLE LiFePO4 BMS boards.

Reverse engineered from the decompiled Android app (uni-app bundle,
common/libs/Command/DataPacker.js and pages/controlPanel/index/index.vue).

Frame shapes
------------
Request  (7 bytes):  [addr, func, reg_hi, reg_lo, count, crc_hi, crc_lo]
Response (6+len):    [addr, func, len_hi, len_lo, data x len, crc_hi, crc_lo]

`func` is a real Modbus-ish function code (3 = read parameter/config
registers, 4 = read status/realtime registers) but the length field is a
16-bit word (not the classic single-byte Modbus byte count), and the CRC is
CRC16/XMODEM rather than the classic Modbus CRC16. `count` in the request is
an opaque single byte the firmware expects per command -- it is not always
"number of 16-bit registers", so each command below hard-codes the exact
value the app sends.

Everything here is read-only (function code 4, plus function code 3 for the
one identification read). The app also has write paths (function 6/16) for
changing protection parameters and pushing firmware -- those are NOT
implemented here since a mistake there can misconfigure or brick a BMS.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .crc import crc16_bytes

FUNC_READ_PARAM = 0x03  # "read parameter register" -- config/calibration
FUNC_READ_STATUS = 0x04  # "read status register" -- realtime + history


def build_request(address: int, function: int, register: int, count: int) -> bytes:
    frame = bytearray()
    frame.append(address & 0xFF)
    frame.append(function & 0xFF)
    frame.append((register >> 8) & 0xFF)
    frame.append(register & 0xFF)
    frame.append(count & 0xFF)
    frame += crc16_bytes(bytes(frame))
    return bytes(frame)


def check_crc(frame: bytes) -> bool:
    if len(frame) < 2:
        return False
    body, crc = frame[:-2], frame[-2:]
    return crc16_bytes(body) == crc


def find_response(buf: bytes, address: int, function: int) -> Optional[bytes]:
    """Scan a byte buffer for the first complete, CRC-valid response frame
    matching (address, function). Mirrors readCommandSend's scan loop."""
    i = 0
    n = len(buf)
    while i < n:
        if n - i > 6 and buf[i] == address and buf[i + 1] == function:
            length = (buf[i + 2] << 8) | buf[i + 3]
            total = length + 6
            if n - i >= total:
                candidate = buf[i : i + total]
                if check_crc(candidate):
                    return candidate
        i += 1
    return None


def u16(b: bytes) -> int:
    return (b[0] << 8) | b[1]


def i16(b: bytes) -> int:
    v = u16(b)
    return v - 0x10000 if v & 0x8000 else v


def bcd_byte(b: int) -> int:
    """Byte's hex digits read back as decimal, e.g. 0x25 -> 25."""
    return int(f"{b:02x}")


TEMP_OFFSET = 2732  # raw units are (degC * 10) + 2732
TEMP_INVALID_C = -40.0


def decode_temp(raw: int) -> Optional[float]:
    c = (raw - TEMP_OFFSET) / 10.0
    if c < TEMP_INVALID_C:
        return None
    return c


PROTECTION_BITS = {
    0: "cell_voltage_diff_large",
    1: "voltage_detect_line_open",
    2: "mos_high_temp",
    3: "protect_board_locked",
    4: "chip_failure",
    5: "short_circuit",
    6: "discharge_overcurrent",
    7: "charge_overcurrent",
    8: "discharge_low_temp",
    9: "discharge_high_temp",
    10: "charge_low_temp",
    11: "charge_high_temp",
    12: "reserved_12",
    13: "reserved_13",
    14: "cell_undervoltage",
    15: "cell_overvoltage",
}


def decode_protection_bits(protection_state: int) -> List[str]:
    active = []
    for bit, name in PROTECTION_BITS.items():
        if protection_state & (1 << bit):
            active.append(name)
    return active


# ---------------------------------------------------------------------------
# Command builders -- (address, register, count) exactly as sent by the app.
# ---------------------------------------------------------------------------


def req_package_number() -> bytes:
    """Discover the BMS's real bus address. Sent to broadcast address 0 --
    this is the one command these boards answer at address 0; the response's
    single data byte is the real address to use for every other command."""
    return build_request(0, FUNC_READ_PARAM, 4008, 1)


def decode_package_number(data: bytes) -> int:
    return data[0]


def req_label_info(address: int) -> bytes:
    return build_request(address, FUNC_READ_PARAM, 4000, 5)


def req_instrument(address: int) -> bytes:
    return build_request(address, FUNC_READ_STATUS, 3000, 5)


def req_cells_page(address: int, page: int, count: int) -> bytes:
    return build_request(address, FUNC_READ_STATUS, 3012 + 14 * page, count)


def req_basic_info(address: int) -> bytes:
    return build_request(address, FUNC_READ_STATUS, 3076, 7)


def req_temp_probes(address: int) -> bytes:
    return build_request(address, FUNC_READ_STATUS, 3087, 4)


def req_protect_counters_1(address: int) -> bytes:
    return build_request(address, FUNC_READ_STATUS, 3095, 7)


def req_protect_counters_2(address: int) -> bytes:
    return build_request(address, FUNC_READ_STATUS, 3109, 7)


def req_manufacturer_1(address: int) -> bytes:
    return build_request(address, FUNC_READ_PARAM, 4009, 3)


def req_manufacturer_serial_a(address: int) -> bytes:
    return build_request(address, FUNC_READ_PARAM, 4023, 1)


def req_manufacturer_serial_b(address: int) -> bytes:
    return build_request(address, FUNC_READ_PARAM, 4037, 1)


def req_manufacturer_name_a(address: int) -> bytes:
    return build_request(address, FUNC_READ_PARAM, 4051, 1)


def req_manufacturer_name_b(address: int) -> bytes:
    return build_request(address, FUNC_READ_PARAM, 4065, 1)


# ---------------------------------------------------------------------------
# Response decoders. Each takes the *data* portion (frame[4:-2]).
# ---------------------------------------------------------------------------


@dataclass
class LabelInfo:
    cell_count: int
    temp_probe_count: int
    nominal_voltage_v: float
    nominal_capacity_ah: float
    full_capacity_ah: float


def decode_label_info(data: bytes) -> LabelInfo:
    return LabelInfo(
        cell_count=data[0],
        temp_probe_count=data[1],
        nominal_voltage_v=u16(data[2:4]) / 10.0,
        nominal_capacity_ah=u16(data[4:6]) / 10.0,
        full_capacity_ah=u16(data[6:8]) / 10.0,
    )


@dataclass
class Instrument:
    current_a: float
    soc_pct: int
    mos_temp_c: Optional[float]
    equilibrium_state: int
    protection_state: int

    @property
    def active_protections(self) -> List[str]:
        return decode_protection_bits(self.protection_state)

    @property
    def balancing_cells(self) -> List[int]:
        return [i + 1 for i in range(32) if self.equilibrium_state & (1 << i)]


def decode_instrument(data: bytes) -> Instrument:
    # 3-byte signed big-endian current, sign-extended from data[0]'s high bit
    sign = 0xFF if data[0] > 124 else 0x00
    current_raw = int.from_bytes(bytes([sign, data[0], data[1], data[2]]), "big", signed=True)
    soc = data[3]
    mos_raw = u16(data[4:6])
    equilibrium = int.from_bytes(data[6:10], "big", signed=False)
    protection = u16(data[10:12])
    return Instrument(
        current_a=current_raw / 1000.0,
        soc_pct=soc,
        mos_temp_c=decode_temp(mos_raw),
        equilibrium_state=equilibrium,
        protection_state=protection,
    )


def decode_cells_page(data: bytes, count: int) -> List[int]:
    return [u16(data[2 * i : 2 * i + 2]) for i in range(count)]


@dataclass
class BasicInfo:
    capacity_ah: float
    cycle_count: int
    health_pct: int
    decay_count: int
    charge_count: int
    discharge_count: int
    aging_ok: bool


def decode_basic_info(data: bytes) -> BasicInfo:
    return BasicInfo(
        capacity_ah=u16(data[0:2]) / 10.0,
        cycle_count=u16(data[2:4]),
        health_pct=data[4],
        decay_count=data[5],
        charge_count=u16(data[6:8]),
        discharge_count=u16(data[8:10]),
        aging_ok=data[10] == 1,
    )


@dataclass
class TempProbes:
    values_c: List[Optional[float]]


def decode_temp_probes(data: bytes) -> TempProbes:
    raws = [u16(data[2 * i : 2 * i + 2]) for i in range(4)]
    return TempProbes(values_c=[decode_temp(r) for r in raws])


@dataclass
class ProtectCounters:
    short_circuit: int
    overload: int
    afe_error: int
    detect_line_broken: int
    charge_overcurrent: int
    charge_high_temp: int
    charge_low_temp: int
    discharge_overcurrent: int
    discharge_high_temp: int
    discharge_low_temp: int
    cell_undervoltage: int
    cell_overvoltage: int
    pack_undervoltage: int
    pack_overvoltage: int


def decode_protect_counters(data1: bytes, data2: bytes) -> ProtectCounters:
    a = [u16(data1[2 * i : 2 * i + 2]) for i in range(7)]
    b = [u16(data2[2 * i : 2 * i + 2]) for i in range(7)]
    return ProtectCounters(
        short_circuit=a[0],
        overload=a[1],
        afe_error=a[2],
        detect_line_broken=a[3],
        charge_overcurrent=a[4],
        charge_high_temp=a[5],
        charge_low_temp=a[6],
        discharge_overcurrent=b[0],
        discharge_high_temp=b[1],
        discharge_low_temp=b[2],
        cell_undervoltage=b[3],
        cell_overvoltage=b[4],
        pack_undervoltage=b[5],
        pack_overvoltage=b[6],
    )


@dataclass
class ManufacturerInfo:
    manufacture_date: str
    firmware_version: str
    serial_number: str
    manufacturer_name: str


def _ascii(b: bytes) -> str:
    return b.decode("ascii", errors="replace").strip(" \x00")


def decode_manufacturer_1(data: bytes) -> tuple[str, str]:
    # data[0] unused/version tag, data[1:4] BCD y/m/d, data[4:14] fw ascii
    y, m, d = bcd_byte(data[1]), bcd_byte(data[2]), bcd_byte(data[3])
    date = f"20{y:02d}-{m:02d}-{d:02d}"
    fw = _ascii(data[4:14])
    return date, fw
