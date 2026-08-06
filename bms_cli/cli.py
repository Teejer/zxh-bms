from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone

from . import protocol as p
from . import reader
from .format import format_text, snapshot_to_dict
from .transport import BmsConnectionError, connect, scan
from .mqtt_bridge import add_mqtt_bridge_parser


async def cmd_scan(args: argparse.Namespace) -> None:
    print(f"Scanning for {args.timeout}s ...", file=sys.stderr)
    devices = await scan(timeout=args.timeout, retries=args.retries, retry_delay=args.retry_delay)
    if not devices:
        print("No BLE devices found.")
        return
    for d in devices:
        print(f"{d.address}  {d.name or '(unknown)'}")


def _connect_kwargs(args: argparse.Namespace) -> dict:
    return dict(
        timeout=args.connect_timeout,
        debug=args.debug,
        retries=args.connect_retries,
        retry_delay=args.retry_delay,
    )


async def _connect_and_get_label(args: argparse.Namespace):
    client, transport = await connect(args.device, **_connect_kwargs(args))
    try:
        if args.address is None:
            args.address = await reader.read_package_number(transport)
            if args.debug:
                print(f"[debug] discovered pack address = {args.address}", flush=True)
        label = await reader.read_label(transport, args.address)
        return client, transport, label
    except Exception:
        try:
            await transport.stop()
        except Exception:
            pass
        try:
            await client.disconnect()
            await asyncio.sleep(0.5)
        except Exception:
            pass
        raise


async def cmd_info(args: argparse.Namespace) -> None:
    client, transport, label = await _connect_and_get_label(args)
    try:
        date, fw = await reader.read_manufacturer(transport, args.address)
        out = {
            "cell_count": label.cell_count,
            "temp_probe_count": label.temp_probe_count,
            "nominal_voltage_v": label.nominal_voltage_v,
            "nominal_capacity_ah": label.nominal_capacity_ah,
            "full_capacity_ah": label.full_capacity_ah,
            "manufacture_date": date,
            "firmware_version": fw,
        }
        print(json.dumps(out, indent=2) if args.json else "\n".join(f"{k}: {v}" for k, v in out.items()))
    finally:
        try:
            await transport.stop()
        except Exception:
            pass
        try:
            await client.disconnect()
            await asyncio.sleep(0.5)
        except Exception:
            pass


async def cmd_status(args: argparse.Namespace) -> None:
    client, transport, label = await _connect_and_get_label(args)
    try:
        snap = await reader.read_snapshot(
            transport,
            args.address,
            label,
            include_protect_counters=args.protect_counters,
            include_manufacturer=False,
        )
        if args.json:
            print(json.dumps(snapshot_to_dict(snap), indent=2))
        else:
            print(format_text(snap))
    finally:
        try:
            await transport.stop()
        except Exception:
            pass
        try:
            await client.disconnect()
            await asyncio.sleep(0.5)
        except Exception:
            pass


async def cmd_watch(args: argparse.Namespace) -> None:
    """Connect fresh for each poll and disconnect right after -- holding one
    BLE connection open across the whole watch session is what these boards
    tend to drop after a few intervals."""
    while True:
        try:
            client, transport, label = await _connect_and_get_label(args)
            try:
                snap = await reader.read_snapshot(transport, args.address, label)
            finally:
                try:
                    await transport.stop()
                except Exception:
                    pass
                try:
                    await client.disconnect()
                    await asyncio.sleep(0.5)
                except Exception:
                    pass
        except (BmsConnectionError, OSError, asyncio.TimeoutError) as e:
            print(f"warning: read failed ({e}); will retry next interval", file=sys.stderr)
        else:
            if args.json:
                print(json.dumps(snapshot_to_dict(snap)))
                sys.stdout.flush()
            else:
                print(f"--- {datetime.now().strftime('%H:%M:%S')} ---")
                print(format_text(snap))
        await asyncio.sleep(args.interval)


async def cmd_pack_number(args: argparse.Namespace) -> None:
    client, transport = await connect(args.device, **_connect_kwargs(args))
    try:
        addr = await reader.read_package_number(transport)
        print(addr)
    finally:
        try:
            await transport.stop()
        except Exception:
            pass
        try:
            await client.disconnect()
            await asyncio.sleep(0.5)
        except Exception:
            pass


async def cmd_raw(args: argparse.Namespace) -> None:
    address = args.address if args.address is not None else 0
    client, transport = await connect(args.device, **_connect_kwargs(args))
    try:
        request = p.build_request(address, args.function, args.register, args.count)
        print(f"-> {request.hex()}")
        resp = await transport.transact(request, address, args.function, retries=args.retries)
        print(f"<- {resp.hex()}")
        print(f"data: {resp[4:-2].hex()}")
    finally:
        try:
            await transport.stop()
        except Exception:
            pass
        try:
            await client.disconnect()
            await asyncio.sleep(0.5)
        except Exception:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bms-cli", description="Linux CLI for BLE LiFePO4 BMS boards")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_connect_opts(sp: argparse.ArgumentParser, multi: bool = False):
        if multi:
            sp.add_argument("device", nargs="+", metavar="device",
                             help="One or more BLE MAC addresses (space separated)")
        else:
            sp.add_argument("device", help="BLE MAC address (or platform-specific identifier)")
        sp.add_argument("--connect-timeout", type=float, default=10.0,
                         help="Seconds to wait for a single connect attempt (default 10)")
        sp.add_argument("--connect-retries", type=int, default=3,
                         help="Retry the whole connect+discover sequence this many times on failure (default 3)")
        sp.add_argument("--retry-delay", type=float, default=2.0,
                         help="Seconds to wait between connect retries (default 2)")
        sp.add_argument("--debug", action="store_true",
                         help="Print discovered GATT services, every write, and every notification received")

    def add_common(sp: argparse.ArgumentParser):
        add_connect_opts(sp)
        sp.add_argument("--address", type=int, default=None,
                         help="BMS bus address / battery pack number. If omitted, it's "
                              "auto-discovered from the device (recommended -- most boards "
                              "ignore commands sent to the wrong address).")

    sp = sub.add_parser("scan", help="Scan for nearby BLE devices")
    sp.add_argument("--timeout", type=float, default=10.0, help="Seconds to scan for (default 10)")
    sp.add_argument("--retries", type=int, default=3,
                     help="Retry the whole scan this many times if BlueZ reports the adapter "
                          "busy/in-progress (default 3)")
    sp.add_argument("--retry-delay", type=float, default=2.0, help="Seconds between scan retries (default 2)")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("pack-number", help="Discover the BMS's real bus address (diagnostic)")
    add_connect_opts(sp)
    sp.set_defaults(func=cmd_pack_number)

    sp = sub.add_parser("info", help="Read identification / manufacturer info once")
    add_common(sp)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("status", help="Read one full status snapshot")
    add_common(sp)
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--protect-counters", action="store_true", help="Also read lifetime protection-event counters")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("watch", help="Poll status in a loop")
    add_common(sp)
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--interval", type=float, default=2.0, help="Seconds between reads (default 2)")
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("raw", help="Send a raw read request (function/register/count) and dump the response")
    add_common(sp)
    sp.add_argument("--function", type=lambda x: int(x, 0), required=True, help="e.g. 3 or 4")
    sp.add_argument("--register", type=lambda x: int(x, 0), required=True)
    sp.add_argument("--count", type=lambda x: int(x, 0), required=True)
    sp.add_argument("--retries", type=int, default=3)
    sp.set_defaults(func=cmd_raw)

    add_mqtt_bridge_parser(sub, add_connect_opts)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        asyncio.run(args.func(args))
    except BmsConnectionError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
