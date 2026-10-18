"""Supervise the radio and the panel command file.

Stdout is one line, ``ready``, and then silence. Readings never go to the log.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# pressure/run execs this file, so the script directory is on sys.path and the
# package root is not. Insert the repo root before the package imports.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pressure.commands import handle, write_response
from pressure.store import Store


def _dirs(args: argparse.Namespace) -> tuple[Path, Path]:
    home = Path.home()
    data = Path(args.data) if args.data else home / ".local" / "share" / "jmcanana.pressure"
    state = Path(args.state) if args.state else home / ".local" / "state" / "jmcanana.pressure"
    return data, state


async def _commands_clean(store: Store, state: Path, live: dict) -> None:
    command_path = state / "command.json"
    response_path = state / "response.json"
    seen = None
    while True:
        if command_path.exists():
            stamp = command_path.stat().st_mtime_ns
            if stamp != seen:
                seen = stamp
                command: dict = {}
                try:
                    command = json.loads(command_path.read_text(encoding="utf-8"))
                    body = handle(store, command)
                except ValueError as exc:
                    body = {"ok": False, "error": str(exc)[:80]}
                except Exception:
                    body = {"ok": False, "error": "command failed"}
                write_response(response_path, str(command.get("id", "")), body)
                if command_path.exists() and command_path.stat().st_mtime_ns == stamp:
                    command_path.unlink()
                store.write_status(
                    state=str(live.get("state", "idle")),
                    beacon=str(live.get("beacon", "none")),
                    link=bool(live.get("link", False)),
                    error=str(live.get("error", "")),
                )
        await asyncio.sleep(0.2)


async def serve(store: Store, state: Path) -> None:
    live = {"state": "starting", "beacon": "none", "link": False, "error": ""}
    store.write_status(state="starting", beacon="none", link=False, error="")
    print("ready", flush=True)
    try:
        await asyncio.gather(
            _commands_clean(store, state, live),
            _radio(store, live),
        )
    except ImportError:
        live["error"] = "bleak is not installed"
        store.write_status(state="error", beacon="none", link=False, error="bleak is not installed")
        raise SystemExit(2)


async def _radio(store: Store, live: dict) -> None:
    from pressure.radio import radio_loop

    await radio_loop(store, live)


def self_check(data: Path, state: Path) -> None:
    from pressure.collector import drain
    from pressure.fake import FakeLink, measurement

    store = Store(data, state)
    store.add_patient("North")
    address = "00:00:00:00:00:01"
    store.trust(address, "Synthetic")
    store.bind(address, 1, 1)
    payload = measurement(120, 78, (2024, 5, 1, 8, 0, 0), 64)
    vendor = measurement(130, 84, (2024, 5, 2, 8, 0, 0), 70, tail=bytes.fromhex("090000000004"))

    async def run() -> None:
        link = FakeLink(
            [
                ("00002a35-0000-1000-8000-00805f9b34fb", payload),
                ("11127003-b364-11e4-ab27-0800200c9a66", vendor),
            ]
        )
        result = await drain(link, store, address, quiet_seconds=0.15, first_wait=1.0)
        arrived = result["stored"] + result["repeated"]
        if arrived != 2 or result["clock_written"]:
            raise SystemExit("self-check drain mismatch")

    asyncio.run(run())
    store.write_status(state="idle", beacon="none", link=False, error="")
    document = json.loads(store.status_path.read_text(encoding="utf-8"))
    if "systolic" in document or document["records"] != 2:
        raise SystemExit("self-check status leaked or dropped a reading")
    shown = store.summary(1, "aha")
    if shown["hero"].get("systolic") != 130 or len(shown["trend"]["readings"]) != 2:
        raise SystemExit("self-check summary lost a reading")
    print("SELF_CHECK_OK", flush=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Local blood-pressure sync")
    parser.add_argument("--data")
    parser.add_argument("--state")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args(argv)
    data, state = _dirs(args)
    if args.self_check:
        self_check(data, state)
        return
    if args.demo:
        from pressure.demo import seed

        store = Store(data, state)
        seed(store)
        print("DEMO_OK", flush=True)
        return
    store = Store(data, state)
    try:
        asyncio.run(serve(store, state))
    except KeyboardInterrupt:
        store.write_status(state="idle", beacon="none", link=False, error="")


if __name__ == "__main__":
    main()
