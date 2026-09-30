from __future__ import annotations

import html
import json
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


_HERE = os.path.dirname(os.path.abspath(__file__))
_STATE_DIR = Path(_HERE).resolve().parent / "xdmovies_state"
_STATE_PATH = _STATE_DIR / "usage.json"
_EXCLUDED_INTERFACE_PREFIXES = ("docker", "veth", "br-", "virbr", "tun", "tap", "zt")


class UsageUnavailable(RuntimeError):
    pass


def _ensure_private_directory() -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(_STATE_DIR, 0o700)


def _write_json(payload: Dict[str, Any]) -> None:
    _ensure_private_directory()
    fd, tmp = tempfile.mkstemp(
        prefix=f".{_STATE_PATH.name}.",
        suffix=".tmp",
        dir=str(_STATE_DIR),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        os.replace(tmp, _STATE_PATH)
    finally:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass


def _load_baseline() -> Optional[Dict[str, Any]]:
    try:
        if not _STATE_PATH.exists():
            return None
        if os.name == "posix":
            os.chmod(_STATE_PATH, 0o600)
        data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _default_route_ip() -> str:
    target = os.environ.get("XDMOVIES_USAGE_ROUTE_TARGET", "1.1.1.1").strip() or "1.1.1.1"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(1.0)
        sock.connect((target, 80))
        return str(sock.getsockname()[0] or "")
    except OSError:
        return ""
    finally:
        sock.close()


def _interface_for_address(address: str, names: List[str]) -> str:
    if not address:
        return ""
    try:
        import psutil

        for name, entries in (psutil.net_if_addrs() or {}).items():
            if name not in names:
                continue
            for entry in entries or []:
                if int(getattr(entry, "family", 0) or 0) != socket.AF_INET:
                    continue
                if str(getattr(entry, "address", "") or "") == address:
                    return name
    except Exception:
        return ""
    return ""


def _candidate_interfaces(names: List[str]) -> List[str]:
    ignored = {"lo", "loopback"}
    return sorted(
        name
        for name in names
        if name.lower() not in ignored
        and not name.lower().startswith(_EXCLUDED_INTERFACE_PREFIXES)
    )


def _detect_interface(counters: Dict[str, Any]) -> str:
    configured = os.environ.get("XDMOVIES_USAGE_INTERFACE", "").strip()
    if configured:
        if configured not in counters:
            raise UsageUnavailable(f"configured interface {configured!r} was not found")
        return configured
    names = list(counters)
    by_route = _interface_for_address(_default_route_ip(), names)
    if by_route:
        return by_route
    candidates = _candidate_interfaces(names)
    if not candidates:
        raise UsageUnavailable("no public network interface was found")
    return max(
        candidates,
        key=lambda name: int(counters[name].bytes_recv) + int(counters[name].bytes_sent),
    )


def _read_interface_counters() -> Tuple[str, int, int]:
    try:
        import psutil
    except ImportError as exc:
        raise UsageUnavailable("psutil is not installed; run /restart after installing it") from exc
    try:
        counters = psutil.net_io_counters(pernic=True) or {}
    except Exception as exc:
        raise UsageUnavailable(f"network counters are unavailable: {exc}") from exc
    if not counters:
        raise UsageUnavailable("network counters are unavailable")
    interface = _detect_interface(counters)
    counter = counters[interface]
    return interface, int(counter.bytes_recv), int(counter.bytes_sent)


def _baseline_payload(interface: str, rx: int, tx: int, started_at: float) -> Dict[str, Any]:
    return {
        "interface": interface,
        "rx": int(rx),
        "tx": int(tx),
        "started_at": float(started_at),
    }


def reset_baseline(now: Optional[float] = None) -> Dict[str, Any]:
    interface, rx, tx = _read_interface_counters()
    started_at = time.time() if now is None else float(now)
    payload = _baseline_payload(interface, rx, tx, started_at)
    _write_json(payload)
    return payload


def _parse_month_row(row: Any) -> Optional[Tuple[Optional[int], int, int]]:
    if not isinstance(row, dict):
        return None
    date = row.get("date") if isinstance(row.get("date"), dict) else {}
    year = int(date.get("year", 0) or 0)
    month = int(date.get("month", 0) or 0)
    if year and month:
        return year, month, month
    month_id = int(row.get("id", 0) or 0)
    if month_id:
        return None, month_id, month_id
    return None


def parse_vnstat_month(payload: Any, interface: str, now: Optional[Any] = None) -> Tuple[int, int]:
    import datetime as _dt

    if isinstance(payload, (str, bytes, bytearray)):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise UsageUnavailable("vnstat returned invalid JSON")
    entry = None
    for candidate in payload.get("interfaces") or []:
        if isinstance(candidate, dict) and str(candidate.get("name") or "") == interface:
            entry = candidate
            break
    if entry is None:
        raise UsageUnavailable(f"vnstat has no data for {interface}")
    traffic = entry.get("traffic") if isinstance(entry.get("traffic"), dict) else {}
    month = traffic.get("month")
    if isinstance(month, dict) and "rx" in month and "tx" in month:
        return int(month.get("rx", 0) or 0), int(month.get("tx", 0) or 0)
    if isinstance(month, dict):
        month = list(month.values())
    if not isinstance(month, list):
        raise UsageUnavailable(f"vnstat has no current-month data for {interface}")
    current = now or _dt.datetime.now()
    for row in month:
        parsed = _parse_month_row(row)
        if parsed is None:
            continue
        year, _row_month, month_number = parsed
        if year is None:
            matches = month_number == current.month
        else:
            matches = year == current.year and month_number == current.month
        if matches and isinstance(row, dict):
            return int(row.get("rx", 0) or 0), int(row.get("tx", 0) or 0)
    raise UsageUnavailable(f"vnstat has no current-month data for {interface}")


def _vnstat_month(interface: str) -> Tuple[Optional[Tuple[int, int]], Optional[str]]:
    try:
        completed = subprocess.run(
            ["vnstat", "--iface", interface, "--json", "m"],
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
    except FileNotFoundError:
        return None, "vnstat is not installed on this VPS"
    except subprocess.TimeoutExpired:
        return None, "vnstat timed out"
    except OSError as exc:
        return None, f"vnstat could not run: {exc}"
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        return None, f"vnstat returned an error{suffix}"
    try:
        return parse_vnstat_month(completed.stdout, interface), None
    except UsageUnavailable as exc:
        return None, str(exc)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return None, f"vnstat data could not be read: {exc}"


def build_snapshot(now: Optional[float] = None) -> Dict[str, Any]:
    timestamp = time.time() if now is None else float(now)
    interface, rx, tx = _read_interface_counters()
    baseline = _load_baseline()
    rebaselined = False
    if (
        not baseline
        or str(baseline.get("interface") or "") != interface
        or int(baseline.get("rx", 0) or 0) > rx
        or int(baseline.get("tx", 0) or 0) > tx
    ):
        baseline = _baseline_payload(interface, rx, tx, timestamp)
        _write_json(baseline)
        rebaselined = True
    month, month_error = _vnstat_month(interface)
    since_rx = max(0, rx - int(baseline.get("rx", rx) or rx))
    since_tx = max(0, tx - int(baseline.get("tx", tx) or tx))
    return {
        "interface": interface,
        "month_rx": month[0] if month else None,
        "month_tx": month[1] if month else None,
        "month_error": month_error,
        "since_rx": since_rx,
        "since_tx": since_tx,
        "elapsed": max(0.0, timestamp - float(baseline.get("started_at", timestamp) or timestamp)),
        "rebaselined": rebaselined,
    }


def format_bytes(value: Any) -> str:
    amount = max(0.0, float(value or 0))
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    for unit in units:
        if amount < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(amount)} {unit}"
            return f"{amount:.2f} {unit}"
        amount /= 1024.0
    return f"{amount:.2f} PiB"


def format_duration(seconds: Any) -> str:
    total = max(0, int(seconds or 0))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours:02d}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def render_html(snapshot: Dict[str, Any]) -> str:
    interface = html.escape(str(snapshot.get("interface") or "unknown"), quote=True)
    lines = [
        "📊 <b>Bandwidth Usage</b>",
        "",
        f"🌐 <b>Overall — current month</b> (<code>{interface}</code>)",
    ]
    month_error = snapshot.get("month_error")
    if month_error:
        lines.append(f"⚠️ {html.escape(str(month_error))}")
    else:
        month_rx = int(snapshot.get("month_rx") or 0)
        month_tx = int(snapshot.get("month_tx") or 0)
        lines.extend(
            [
                f"⬇️ Received: {format_bytes(month_rx)}",
                f"⬆️ Sent: {format_bytes(month_tx)}",
                f"🔁 Total: {format_bytes(month_rx + month_tx)}",
            ]
        )
    since_rx = int(snapshot.get("since_rx") or 0)
    since_tx = int(snapshot.get("since_tx") or 0)
    lines.extend(
        [
            "",
            "🔄 <b>Since last bot restart</b>",
            f"⬇️ Received: {format_bytes(since_rx)}",
            f"⬆️ Sent: {format_bytes(since_tx)}",
            f"🔁 Total: {format_bytes(since_rx + since_tx)}",
            f"⏱️ Uptime: {format_duration(snapshot.get('elapsed'))}",
        ]
    )
    if snapshot.get("rebaselined"):
        lines.append("\nℹ️ Baseline was just created; usage starts counting now.")
    return "\n".join(lines)


def report_html() -> str:
    return render_html(build_snapshot())