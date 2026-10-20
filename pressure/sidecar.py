"""Supervise the radio and the panel command file.

Stdout is one line, ``ready``, and then silence. Readings never go to the log.
"""

from __future__ import annotations

import argparse
import importlib
import asyncio
import fcntl
import json
import os
import signal
import sys
import time
from pathlib import Path

# pressure/run execs this file, so the script directory is on sys.path and the
# package root is not. Insert the repo root before the package imports.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pressure.commands import handle, write_response
from pressure.store import Store, _private_dir


def _dirs(args: argparse.Namespace) -> tuple[Path, Path]:
    home = Path.home()
    data = Path(args.data) if args.data else home / ".local" / "share" / "jmcanana.blood-pressure"
    state = Path(args.state) if args.state else home / ".local" / "state" / "jmcanana.blood-pressure"
    return data, state


async def _commands_clean(store: Store, state: Path, live: dict) -> None:
    """Handle the panel's command file. Claiming it with a rename first means a command written
    while the previous one is being handled is never deleted unread."""
    command_path = state / "command.json"
    claimed_path = state / "command.claimed"
    response_path = state / "response.json"
    while True:
        # Keep the directory 0700 even if something recreated it; a claim left by a crash is
        # handled before any new command.
        _private_dir(state)
        if not claimed_path.exists():
            try:
                os.replace(command_path, claimed_path)
            except FileNotFoundError:
                await asyncio.sleep(0.2)
                continue
        command: dict = {}
        try:
            command = json.loads(claimed_path.read_text(encoding="utf-8"))
            body = handle(store, command)
        except ValueError as exc:
            body = {"ok": False, "error": str(exc)[:80]}
        except Exception:
            body = {"ok": False, "error": "command failed"}
        claimed_path.unlink(missing_ok=True)
        write_response(response_path, str(command.get("id", "")), body)
        store.write_status(
            state=str(live.get("state", "idle")),
            beacon=str(live.get("beacon", "none")),
            link=bool(live.get("link", False)),
            error=str(live.get("error", "")),
        )


PARENT_POLL_SECONDS = 1.0
LOCK_RETRY_SECONDS = 1.0
LOCK_QUIET_SECONDS = 5.0
CLEANUP_SECONDS = 10.0
ALREADY_RUNNING = "a sidecar is already using these directories"


def _try_lock(path: Path) -> int | None:
    """Return a descriptor holding the sidecar lock, with our pid written in it for a waiter to name,
    or None while another sidecar holds it."""
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    os.ftruncate(descriptor, 0)
    os.write(descriptor, f"{os.getpid()}\n".encode())
    return descriptor


async def _single_instance(state: Path) -> int:
    """Wait until no other sidecar holds the lock: after a shell restart the old one may still be exiting."""
    _private_dir(state)
    lock_path = state / "sidecar.lock"
    quiet_until = time.monotonic() + LOCK_QUIET_SECONDS
    while (descriptor := _try_lock(lock_path)) is None:
        if quiet_until is not None and time.monotonic() >= quiet_until:
            holder = lock_path.read_text(encoding="utf-8").strip() or "unknown"
            print(f"waiting for sidecar {holder} to exit", file=sys.stderr, flush=True)
            quiet_until = None
        await asyncio.sleep(LOCK_RETRY_SECONDS)
    return descriptor


async def _stop_requested(parent_pid: int | None, stop: asyncio.Event) -> None:
    """Return on SIGTERM or SIGINT, or once the shell that started us has died: a dead shell does not
    kill its children."""
    while not stop.is_set() and (parent_pid is None or os.getppid() == parent_pid):
        await asyncio.sleep(PARENT_POLL_SECONDS)


def _abandon() -> None:
    """Exit without further cleanup; the kernel releases the lock."""
    os._exit(0)


async def _run_until_stopped(work, parent_pid: int | None) -> None:
    """Run work until it ends or a stop is requested, then cancel it. Cleanup that hangs (a BlueZ
    disconnect, say) is abandoned after CLEANUP_SECONDS so the lock is never held by a dead sidecar."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signal_number, stop.set)
    job = asyncio.ensure_future(work)
    watch = asyncio.ensure_future(_stop_requested(parent_pid, stop))
    done, pending = await asyncio.wait({job, watch}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    if pending:
        _finished, stuck = await asyncio.wait(pending, timeout=CLEANUP_SECONDS)
        if stuck:
            _abandon()
    for task in done:
        task.result()


async def supervise(data: Path, state: Path) -> None:
    lock = await _single_instance(state)
    try:
        store = Store(data, state)
        try:
            await serve(store, state)
        finally:
            store.write_status(state="idle", beacon="none", link=False, error="")
    finally:
        os.close(lock)


async def serve(store: Store, state: Path) -> None:
    live = {"state": "starting", "beacon": "none", "link": False, "error": ""}
    store.write_status(state="starting", beacon="none", link=False, error="")
    print("ready", flush=True)
    # A task group, unlike gather, waits for the radio to finish cancelling before the caller releases the lock.
    async with asyncio.TaskGroup() as group:
        group.create_task(_commands_clean(store, state, live))
        group.create_task(_radio(store, live))


BLEAK_MISSING = "bleak is not installed"


async def _radio(store: Store, live: dict) -> None:
    """Without bleak there is no radio, but the panel keeps working and shows the setup command."""
    try:
        importlib.import_module("bleak")
    except ImportError:
        live.update(state="setup", error=BLEAK_MISSING)
        store.write_status(state="setup", beacon="none", link=False, error=BLEAK_MISSING)
        return
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
        if arrived != 2 or result["clock_set"]:
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
    parser.add_argument("--parent-pid", type=int)
    args = parser.parse_args(argv)
    data, state = _dirs(args)
    if args.self_check or args.demo:
        _run_offline(args, data, state)
        return
    asyncio.run(_run_until_stopped(supervise(data, state), args.parent_pid))


def _run_offline(args: argparse.Namespace, data: Path, state: Path) -> None:
    """The self-check and demo write synthetic data, so they refuse directories a sidecar is serving."""
    _private_dir(state)
    lock = _try_lock(state / "sidecar.lock")
    if lock is None:
        raise SystemExit(ALREADY_RUNNING)
    try:
        if args.self_check:
            self_check(data, state)
            return
        from pressure.demo import seed

        seed(Store(data, state))
        print("DEMO_OK", flush=True)
    finally:
        os.close(lock)


if __name__ == "__main__":
    main()
