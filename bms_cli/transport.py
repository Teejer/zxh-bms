"""BLE transport: connect, pick the right service/characteristics, and run
write+wait-for-notify transactions with retry -- mirroring the app's
$BluetoothHelper service auto-detection and readCommandSend retry loop.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Optional
import queue

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakDBusError

from .protocol import find_response

log = logging.getLogger("bms_cli.transport")

# (service uuid substring, notify char uuid, write char uuid) -- checked in
# this order, same as the app's connect flow.
KNOWN_PROFILES = [
    ("00002760", "00002760-08C2-11E1-9073-0E8AC72E0002", "00002760-08C2-11E1-9073-0E8AC72E0001"),
    ("6E400001", "6E400003-B5A3-F393-E0A9-E50E24DCCA9E", "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"),
    ("0003CDD0", "0003CDD1-0000-1000-8000-00805F9B0131", "0003CDD2-0000-1000-8000-00805F9B0131"),
]


class BmsConnectionError(RuntimeError):
    pass


class BmsTransport:
    def __init__(
        self,
        client: BleakClient,
        notify_uuid: str,
        write_uuid: str,
        write_with_response: bool = False,
        debug: bool = False,
    ):
        self.client = client
        self.notify_uuid = notify_uuid
        self.write_uuid = write_uuid
        self.write_with_response = write_with_response
        self.debug = debug
        # Thread-safe queue: _on_notify runs in BlueZ thread, not asyncio
        self._notify_q: "queue.Queue[bytes]" = queue.Queue()
        self._lock = asyncio.Lock()

    def _on_notify(self, _handle, data: bytearray) -> None:
        self._notify_q.put(bytes(data))
        if self.debug:
            print(f"[debug] notify <- {data.hex()}", flush=True)

    async def start(self) -> None:
        await self.client.start_notify(self.notify_uuid, self._on_notify)

    async def stop(self) -> None:
        try:
            await self.client.stop_notify(self.notify_uuid)
        except Exception:
            pass

    async def close(self) -> None:
        """Stop notifications and clean up."""
        await self.stop()

    async def transact(
        self,
        request: bytes,
        address: int,
        function: int,
        attempt_timeout: float = 1.5,
        retries: int = 3,
    ) -> bytes:
        """Write `request`, wait for a matching, CRC-valid response. Retries
        the write up to `retries` times, `attempt_timeout` seconds each."""
        async with self._lock:
            # Drain notification queue for this transaction
            while not self._notify_q.empty():
                self._notify_q.get_nowait()
            resp_buf: bytearray = bytearray()
            for attempt in range(retries):
                if self.debug:
                    print(f"[debug] write -> {request.hex()} "
                          f"(char={self.write_uuid}, response={self.write_with_response})", flush=True)
                await self.client.write_gatt_char(
                    self.write_uuid, request, response=self.write_with_response
                )
                loop = asyncio.get_event_loop()
                deadline = loop.time() + attempt_timeout
                # Drain notification queue for this transaction
                while loop.time() < deadline:
                    while not self._notify_q.empty():
                        data = self._notify_q.get_nowait()
                        resp_buf.extend(data)
                    resp = find_response(bytes(resp_buf), address, function)
                    if resp is not None:
                        return resp
                    await asyncio.sleep(0.04)
                log.debug("attempt %d/%d timed out, retrying", attempt + 1, retries)
            raise BmsConnectionError(
                f"no valid response for func={function:#x} after {retries} attempts "
                f"(address={address}); raw bytes seen from device: "
                f"{bytes(resp_buf).hex() or '(none -- nothing was received at all)'}"
            )


def _char_by_uuid(services, uuid: str):
    for svc in services:
        for ch in svc.characteristics:
            if ch.uuid.lower() == uuid.lower():
                return ch
    return None


def _write_mode(ch) -> bool:
    """True if the characteristic requires write-with-response (i.e. it does
    NOT advertise write-without-response)."""
    if ch is None:
        return True
    props = set(ch.properties)
    if "write-without-response" in props:
        return False
    return True


async def find_profile_for_client(client: BleakClient):
    services = client.services
    if services is None:
        services = await client.get_services()
    for prefix, notify_uuid, write_uuid in KNOWN_PROFILES:
        for svc in services:
            if prefix.lower() in svc.uuid.lower():
                write_ch = _char_by_uuid(services, write_uuid)
                return notify_uuid, write_uuid, _write_mode(write_ch)
    # Fallback: find one notify-capable and one write-capable characteristic.
    notify_uuid = write_uuid = None
    write_ch = None
    for svc in services:
        for ch in svc.characteristics:
            props = set(ch.properties)
            if notify_uuid is None and ("notify" in props or "indicate" in props):
                notify_uuid = ch.uuid
            if write_uuid is None and ("write" in props or "write-without-response" in props):
                write_uuid = ch.uuid
                write_ch = ch
    if notify_uuid and write_uuid:
        return notify_uuid, write_uuid, _write_mode(write_ch)
    raise BmsConnectionError("could not find a notify+write characteristic pair on this device")


def describe_services(services) -> str:
    lines = []
    for svc in services:
        lines.append(f"service {svc.uuid}")
        for ch in svc.characteristics:
            lines.append(f"  char {ch.uuid}  props={list(ch.properties)}")
    return "\n".join(lines)


async def scan(timeout: float = 10.0, retries: int = 3, retry_delay: float = 2.0) -> list[BLEDevice]:
    """Retries the whole scan on BleakDBusError. BlueZ's discovery state is a
    single resource shared by the whole adapter, so 'org.bluez.Error.InProgress'
    (another discovery session mid-flight -- a stuck previous scan, another BLE
    client on the same adapter, an mqtt-bridge instance also connecting right
    now, etc.) shows up occasionally and is usually transient."""
    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            return await BleakScanner.discover(timeout=timeout)
        except BleakDBusError as e:
            last_exc = e
            if attempt < retries:
                print(
                    f"scan attempt {attempt}/{retries} failed ({e}); "
                    f"retrying in {retry_delay:.0f}s...",
                    file=sys.stderr,
                )
                await asyncio.sleep(retry_delay)
    raise BmsConnectionError(f"scan failed after {retries} attempts: {last_exc}")


async def connect(
    address_or_name: str,
    timeout: float = 10.0,
    debug: bool = False,
    retries: int = 3,
    retry_delay: float = 2.0,
) -> tuple[BleakClient, BmsTransport]:
    """Connect and pick a profile, retrying the whole sequence (fresh
    BleakClient each time) on any failure.

    After each connect we sleep briefly -- bleak/BlueZ sometimes fails to
    restart its D-Bus event loop after rapid reconnects.  A 1s pause stabilizes
    notify callbacks and prevents the silent-stall seen after ~7 cycles.
    """
    last_exc: Optional[Exception] = None
    transport: Optional[BmsTransport] = None
    for attempt in range(1, retries + 1):
        client = BleakClient(address_or_name, timeout=timeout)
        try:
            await client.connect()
            if debug:
                services = client.services or await client.get_services()
                print(
                    f"[debug] discovered services/characteristics:\n{describe_services(services)}",
                    flush=True,
                )
            notify_uuid, write_uuid, write_with_response = await find_profile_for_client(client)
            if debug:
                print(
                    f"[debug] using notify={notify_uuid} write={write_uuid} "
                    f"write_with_response={write_with_response}",
                    flush=True,
                )
            transport = BmsTransport(client, notify_uuid, write_uuid, write_with_response, debug=debug)
            await transport.start()
            # Stabilize BlueZ event loop
            await asyncio.sleep(1.0)
            return client, transport
        except Exception as e:
            last_exc = e
            # Stop notify before disconnect to prevent stale callbacks
            try:
                if transport is not None:
                    await transport.stop()
            except Exception:
                pass
            try:
                await client.disconnect()
            except Exception:
                pass
            if attempt < retries:
                print(
                    f"connect attempt {attempt}/{retries} failed ({e}); "
                    f"retrying in {retry_delay:.0f}s...",
                    file=sys.stderr,
                )
                await asyncio.sleep(retry_delay)
    raise BmsConnectionError(f"failed to connect after {retries} attempts: {last_exc}")
