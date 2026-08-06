"""Long-running bridge: reconnect to the BMS over BLE on every poll interval,
read one snapshot, disconnect, and publish it to MQTT with Home Assistant
MQTT Discovery so entities show up automatically -- no HA-side YAML required.

A fresh BLE connection per poll is slower than holding one open, but these
boards tend to drop a long-lived connection after a few intervals, so
reconnecting each time is more reliable in practice.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import sys
from typing import Optional

from . import reader
from .format import snapshot_to_dict
from .transport import BmsConnectionError, connect

log = logging.getLogger("bms_cli.mqtt_bridge")


def _slug(mac: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", mac.lower())


def _device_block(mac: str, label) -> dict:
    return {
        "identifiers": [f"bms_{_slug(mac)}"],
        "name": f"LiFePO4 BMS {mac}",
        "manufacturer": "Generic BLE BMS",
        "model": f"{label.cell_count}S / {label.nominal_voltage_v}V / {label.nominal_capacity_ah}Ah",
    }


# (object_id, name, json key/template, unit, device_class, state_class, entity_category)
_SIMPLE_SENSORS = [
    ("pack_voltage", "Pack Voltage", "{{ value_json.pack_voltage_v }}", "V", "voltage", "measurement", None),
    ("current", "Current", "{{ value_json.current_a }}", "A", "current", "measurement", None),
    ("power", "Power", "{{ value_json.power_w }}", "W", "power", "measurement", None),
    ("soc", "State of Charge", "{{ value_json.soc_pct }}", "%", "battery", "measurement", None),
    ("mos_temp", "MOSFET Temperature", "{{ value_json.mos_temp_c }}", "°C", "temperature", "measurement", None),
    ("min_cell", "Min Cell Voltage", "{{ value_json.min_cell_mv }}", "mV", "voltage", "measurement", "diagnostic"),
    ("max_cell", "Max Cell Voltage", "{{ value_json.max_cell_mv }}", "mV", "voltage", "measurement", "diagnostic"),
    ("cell_delta", "Cell Voltage Delta", "{{ value_json.cell_delta_mv }}", "mV", "voltage", "measurement", "diagnostic"),
    ("cycle_count", "Cycle Count", "{{ value_json.cycle_count }}", None, None, "total_increasing", "diagnostic"),
    ("health", "Health", "{{ value_json.health_pct }}", "%", None, "measurement", "diagnostic"),
    ("remaining_capacity", "Remaining Capacity", "{{ value_json.remaining_capacity_ah }}", "Ah", None, "measurement", None),
    ("balancing_cells", "Balancing Cells", "{{ value_json.balancing_cells }}", None, None, None, "diagnostic"),
    ("last_updated", "Last Updated", "{{ value_json.timestamp }}", None, "timestamp", None, "diagnostic"),
    ("device_name", "BLE Device Name", "{{ value_json.device_name }}", None, None, None, "diagnostic"),
]


def build_discovery(mac: str, label, prefix: str, base_topic: str) -> list[tuple[str, dict]]:
    """Return [(config_topic, payload_dict), ...] for every entity."""
    device = _device_block(mac, label)
    state_topic = f"{base_topic}/state"
    availability_topic = f"{base_topic}/availability"
    node_id = _slug(mac)
    entries: list[tuple[str, dict]] = []

    def sensor(component, object_id, payload):
        topic = f"{prefix}/{component}/{node_id}/{object_id}/config"
        payload = {
            "state_topic": state_topic,
            "availability_topic": availability_topic,
            "unique_id": f"bms_{node_id}_{object_id}",
            "device": device,
            **payload,
        }
        entries.append((topic, payload))

    for object_id, name, template, unit, device_class, state_class, category in _SIMPLE_SENSORS:
        payload = {"name": name, "value_template": template}
        if unit:
            payload["unit_of_measurement"] = unit
        if device_class:
            payload["device_class"] = device_class
        if state_class:
            payload["state_class"] = state_class
        if category:
            payload["entity_category"] = category
        sensor("sensor", object_id, payload)

    for i in range(label.cell_count):
        sensor(
            "sensor",
            f"cell_{i + 1}",
            {
                "name": f"Cell {i + 1} Voltage",
                "value_template": f"{{{{ value_json.cell_voltages_mv[{i}] }}}}",
                "unit_of_measurement": "mV",
                "device_class": "voltage",
                "state_class": "measurement",
                "entity_category": "diagnostic",
            },
        )

    for i in range(4):
        sensor(
            "sensor",
            f"probe_temp_{i + 1}",
            {
                "name": f"Temp Probe {i + 1}",
                "value_template": f"{{{{ value_json.probe_temps_c[{i}] }}}}",
                "unit_of_measurement": "°C",
                "device_class": "temperature",
                "state_class": "measurement",
            },
        )

    sensor(
        "binary_sensor",
        "protection",
        {
            "name": "Protection Active",
            "device_class": "problem",
            "value_template": "{{ 'ON' if (value_json.active_protections | length > 0) else 'OFF' }}",
            "payload_on": "ON",
            "payload_off": "OFF",
        },
    )
    sensor(
        "sensor",
        "active_protections",
        {
            "name": "Active Protections",
            "value_template": "{{ value_json.active_protections | join(', ') if value_json.active_protections else 'none' }}",
            "entity_category": "diagnostic",
        },
    )

    return entries


class MqttPublisher:
    """Thin wrapper around paho-mqtt so the rest of this module doesn't
    depend on its API shape directly."""

    def __init__(self, host, port, username, password, availability_topic, tls=False):
        import paho.mqtt.client as mqtt

        self._mqtt = mqtt
        self.client = mqtt.Client()
        if username:
            self.client.username_pw_set(username, password)
        if tls:
            self.client.tls_set()
        self.client.will_set(availability_topic, payload="offline", qos=1, retain=True)
        self.client.connect(host, port, keepalive=30)
        self.client.loop_start()

    def publish(self, topic: str, payload, retain: bool = False, qos: int = 0):
        if not isinstance(payload, (str, bytes)):
            payload = json.dumps(payload)
        self.client.publish(topic, payload, qos=qos, retain=retain)

    def close(self, availability_topic: str):
        try:
            self.client.publish(availability_topic, "offline", qos=1, retain=True)
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass


async def run_bridge(args: argparse.Namespace) -> None:
    """Entry point for the mqtt-bridge subcommand. `args.device` is always a
    list (argparse nargs='+'), even for a single device."""
    devices: list[str] = args.device
    if len(devices) > 1 and args.address is not None:
        raise BmsConnectionError(
            "--address cannot be used with multiple devices -- each device's bus "
            "address is auto-discovered independently; omit --address for multi-device mode"
        )

    if len(devices) == 1:
        await _run_single_bridge(devices[0], args.address, args, connect_lock=None)
        return

    # BlueZ's discovery/inquiry state is a single resource shared by the whole
    # adapter -- letting every device's connect/disconnect (and
    # reconnect-after-drop) attempt hit it at the same instant is a good way
    # to get org.bluez.Error.InProgress, or to have one device's disconnect
    # collide with another's connect and make it flaky. Serialize each
    # device's whole connect->read->disconnect cycle across devices in this
    # process, with a cooldown pause held after disconnecting before the
    # next device is allowed to connect.
    connect_lock = asyncio.Lock()
    log.info("starting bridge for %d devices: %s", len(devices), ", ".join(devices))
    results = await asyncio.gather(
        *(_run_single_bridge(device, None, args, connect_lock) for device in devices),
        return_exceptions=True,
    )
    for device, result in zip(devices, results):
        if isinstance(result, Exception):
            log.error("[%s] bridge task exited with an error: %s", device, result)


async def _run_single_bridge(
    device: str, address_override, args: argparse.Namespace, connect_lock: Optional[asyncio.Lock]
) -> None:
    base_topic = f"{args.topic_prefix}/{_slug(device)}"
    availability_topic = f"{base_topic}/availability"
    state_topic = f"{base_topic}/state"

    mqtt_password = args.mqtt_password
    if args.mqtt_password_env and not mqtt_password:
        mqtt_password = os.environ.get(args.mqtt_password_env)

    # Each device gets its own MQTT connection (own TCP socket + client id) so
    # its last-will correctly marks only *that* device's availability topic
    # offline if the process dies uncleanly -- paho only supports one LWT per
    # connection, so a shared connection couldn't do this per-device.
    mqtt = MqttPublisher(
        args.mqtt_host, args.mqtt_port, args.mqtt_username, mqtt_password,
        availability_topic, tls=args.mqtt_tls,
    )

    discovery_published = False
    address = address_override
    device_name: Optional[str] = None

    async def connect_and_read():
        """Connect fresh, read one snapshot, then disconnect -- reconnecting
        every poll instead of holding one BLE connection open for the whole
        bridge lifetime, since that's what these boards drop after a few
        intervals.

        In multi-device mode the whole cycle runs under connect_lock, with a
        cooldown pause held after disconnecting before the lock is released
        -- back-to-back connect/disconnect traffic across devices on the same
        BLE adapter is what tends to make reads flaky."""
        nonlocal discovery_published, address, device_name
        lock_ctx = connect_lock if connect_lock is not None else contextlib.nullcontext()
        async with lock_ctx:
            log.info("[%s] connecting ...", device)
            client, transport = await connect(
                device,
                timeout=args.connect_timeout,
                debug=args.debug,
                retries=args.connect_retries,
                retry_delay=args.retry_delay,
            )
            try:
                device_name = client.name or device_name
                if address is None:
                    address = await reader.read_package_number(transport)
                label = await reader.read_label(transport, address)
                if not discovery_published:
                    for topic, payload in build_discovery(device, label, args.discovery_prefix, base_topic):
                        mqtt.publish(topic, payload, retain=True)
                    discovery_published = True
                    log.info("[%s] published Home Assistant discovery configs (%d cells, %d temp probes)",
                              device, label.cell_count, label.temp_probe_count)
                snap = await reader.read_snapshot(transport, address, label)
            finally:
                try:
                    await transport.stop()
                except Exception:
                    pass
                try:
                    await client.disconnect()
                except Exception:
                    pass
                await asyncio.sleep(0.5)
                if connect_lock is not None:
                    await asyncio.sleep(args.connect_pause)
        return snap

    try:
        while True:
            try:
                snap = await connect_and_read()
                mqtt.publish(availability_topic, "online", retain=True)
                payload = snapshot_to_dict(snap)
                payload["device_name"] = device_name
                mqtt.publish(state_topic, payload)
            except (BmsConnectionError, OSError, asyncio.TimeoutError) as e:
                log.warning("[%s] read failed (%s); will retry next interval, leaving last "
                            "known values in place", device, e)
            await asyncio.sleep(args.interval)
    finally:
        mqtt.close(availability_topic)


def add_mqtt_bridge_parser(sub, add_connect_opts) -> None:
    sp = sub.add_parser(
        "mqtt-bridge",
        help="Run a persistent daemon that publishes readings to MQTT with Home Assistant auto-discovery",
    )
    add_connect_opts(sp, multi=True)
    sp.add_argument("--address", type=int, default=None,
                     help="BMS bus address; auto-discovered if omitted. Only valid with a single device.")
    sp.add_argument("--interval", type=float, default=5.0, help="Seconds between reads (default 5)")
    sp.add_argument("--connect-pause", type=float, default=5.0,
                     help="With multiple devices, seconds to wait after disconnecting from one "
                          "device before connecting to the next (default 5). Ignored with a "
                          "single device.")
    sp.add_argument("--mqtt-host", required=True)
    sp.add_argument("--mqtt-port", type=int, default=1883)
    sp.add_argument("--mqtt-username", default=None)
    sp.add_argument("--mqtt-password", default=None,
                     help="Prefer --mqtt-password-env to avoid putting the password on the command line")
    sp.add_argument("--mqtt-password-env", default=None,
                     help="Name of an environment variable to read the MQTT password from")
    sp.add_argument("--mqtt-tls", action="store_true")
    sp.add_argument("--discovery-prefix", default="homeassistant",
                     help="Home Assistant MQTT discovery prefix (default homeassistant)")
    sp.add_argument("--topic-prefix", default="bms", help="Base MQTT topic prefix (default bms)")

    async def _run(args):
        logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                             format="%(asctime)s %(levelname)s %(message)s")
        await run_bridge(args)

    sp.set_defaults(func=_run)
