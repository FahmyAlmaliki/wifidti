from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from math import sqrt

import requests


def _run(cmd: list[str], timeout: int = 20) -> str:
    return subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=timeout, text=True)


def _env_float(name: str) -> float | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        return float(raw)
    except Exception:
        return None


def _env_int(name: str) -> int | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except Exception:
        return None


def scan_wifi() -> list[dict]:
    # Prefer `iw dev wlan0 scan` because it can provide dBm.
    try:
        out = _run(["iw", "dev", "wlan0", "scan"], timeout=25)
        networks: list[dict] = []
        ssid = None
        rssi = None
        freq = None
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("BSS "):
                if ssid:
                    networks.append(_finalize_wifi(ssid, rssi, freq))
                ssid, rssi, freq = None, None, None
            elif line.startswith("signal:"):
                # e.g. signal: -47.00 dBm
                try:
                    rssi = float(line.split()[1])
                except Exception:
                    pass
            elif line.startswith("freq:"):
                try:
                    freq = int(line.split()[1])
                except Exception:
                    pass
            elif line.startswith("SSID:"):
                ssid = line.split("SSID:", 1)[1].strip()

        if ssid:
            networks.append(_finalize_wifi(ssid, rssi, freq))

        return [n for n in networks if n.get("ssid")]
    except Exception:
        pass

    # Fallback: nmcli (signal percent + channel)
    try:
        out = _run(["nmcli", "-t", "-f", "SSID,SIGNAL,CHAN", "dev", "wifi"], timeout=25)
        networks: list[dict] = []
        for row in out.splitlines():
            parts = row.split(":")
            if len(parts) < 3:
                continue
            ssid, signal, chan = parts[0], parts[1], parts[2]
            if not ssid:
                continue
            item = {"ssid": ssid}
            try:
                item["signal_percent"] = float(signal)
            except Exception:
                pass
            try:
                item["channel"] = int(chan)
            except Exception:
                pass
            networks.append(item)
        return networks
    except Exception:
        return []


def _finalize_wifi(ssid: str | None, rssi_dbm: float | None, freq_mhz: int | None) -> dict:
    item: dict = {"ssid": ssid or ""}
    if rssi_dbm is not None:
        item["rssi_dbm"] = rssi_dbm
    if freq_mhz is not None:
        item["channel"] = _freq_to_channel(freq_mhz)
    return item


def _freq_to_channel(freq_mhz: int) -> int | None:
    # 2.4 GHz
    if 2412 <= freq_mhz <= 2472:
        return 1 + (freq_mhz - 2412) // 5
    if freq_mhz == 2484:
        return 14
    # 5 GHz (common)
    if 5180 <= freq_mhz <= 5825:
        return (freq_mhz - 5000) // 5
    return None


def ping_stats_ms(host: str = "8.8.8.8", *, count: int = 4, timeout_s: int = 20) -> dict | None:
    try:
        out = _run(["ping", "-c", str(count), "-n", host], timeout=timeout_s)
    except Exception:
        return None

    # Common formats:
    # - rtt min/avg/max/mdev = 7.123/8.456/9.001/0.321 ms
    # - round-trip min/avg/max/stddev = 7.123/8.456/9.001/0.321 ms
    for line in out.splitlines():
        if ("min/avg" in line) and ("=" in line):
            try:
                stats = line.split("=", 1)[1].strip().split()[0]
                _min_s, avg_s, _max_s, dev_s = stats.split("/")
                return {"ping_ms": float(avg_s), "jitter_ms": float(dev_s)}
            except Exception:
                pass

    # Fallback: compute avg + stddev from per-packet times.
    samples: list[float] = []
    for line in out.splitlines():
        # e.g. "64 bytes from 8.8.8.8: icmp_seq=1 ttl=116 time=8.42 ms"
        if " time=" not in line:
            continue
        try:
            tail = line.split(" time=", 1)[1]
            value = float(tail.split()[0])
            samples.append(value)
        except Exception:
            continue

    if not samples:
        return None

    avg = sum(samples) / len(samples)
    var = sum((x - avg) ** 2 for x in samples) / len(samples)
    return {"ping_ms": float(avg), "jitter_ms": float(sqrt(var))}


def run_speedtest_cli(*, timeout_s: int = 120) -> dict | None:
    try:
        out = _run(["speedtest-cli", "--json", "--secure"], timeout=timeout_s)
    except Exception:
        return None

    # Some environments prepend warnings; try to extract JSON object.
    text = out.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    try:
        data = json.loads(text[start : end + 1])
        download_bps = float(data.get("download"))
        upload_bps = float(data.get("upload"))
        ping_ms = float(data.get("ping"))
        return {
            "download_mbps": download_bps / 1e6,
            "upload_mbps": upload_bps / 1e6,
            "ping_ms": ping_ms,
        }
    except Exception:
        return None


def location_from_env() -> dict | None:
    lat = _env_float("LOCATION_LAT")
    lon = _env_float("LOCATION_LON")
    if lat is None or lon is None:
        return None
    loc: dict = {"lat": lat, "lon": lon}
    acc = _env_float("LOCATION_ACCURACY_M")
    if acc is not None:
        loc["accuracy_m"] = acc
    label = os.getenv("LOCATION_LABEL")
    if label:
        loc["label"] = label.strip()
    return loc


def location_from_gpsd(*, timeout_s: int = 6) -> dict | None:
    # Requires gpsd + gpspipe. Returns the first TPV fix that includes lat/lon.
    try:
        out = _run(["gpspipe", "-w", "-n", "12"], timeout=timeout_s)
    except Exception:
        return None

    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("class") != "TPV":
            continue
        lat = obj.get("lat")
        lon = obj.get("lon")
        if lat is None or lon is None:
            continue

        loc: dict = {"lat": float(lat), "lon": float(lon)}

        # gpsd uses meters for epx/epy/eph.
        for key in ("eph", "epx", "epy"):
            if obj.get(key) is None:
                continue
            try:
                loc["accuracy_m"] = float(obj[key])
                break
            except Exception:
                pass

        return loc

    return None


def get_location(*, gpsd_enabled: bool = True) -> dict | None:
    return location_from_env() or (location_from_gpsd() if gpsd_enabled else None)


def main() -> None:
    api_base = os.getenv("API_BASE", "http://localhost:8000").rstrip("/")
    device_id = os.getenv("DEVICE_ID", "pi-01")
    interval_s = int(os.getenv("INTERVAL_SECONDS", "60"))

    connect_timeout_s = float(os.getenv("HTTP_CONNECT_TIMEOUT", "5"))
    read_timeout_s = float(os.getenv("HTTP_READ_TIMEOUT", "15"))

    ping_host = os.getenv("PING_HOST", "8.8.8.8")
    ping_count = _env_int("PING_COUNT") or 4
    ping_timeout_s = _env_int("PING_TIMEOUT_SECONDS") or 20

    speedtest_enabled = (os.getenv("SPEEDTEST_ENABLED", "1").strip() not in {"0", "false", "no"})
    speedtest_interval_s = _env_int("SPEEDTEST_INTERVAL_SECONDS") or 600
    speedtest_timeout_s = _env_int("SPEEDTEST_TIMEOUT_SECONDS") or 120

    gpsd_enabled = (os.getenv("GPSD_ENABLED", "1").strip() not in {"0", "false", "no"})
    location_interval_s = _env_int("LOCATION_INTERVAL_SECONDS") or 300

    last_speedtest_at = 0.0
    last_location_at = 0.0
    cached_location: dict | None = None

    # Preflight: helps distinguish "backend not reachable" vs "ingest is slow".
    try:
        r = requests.get(f"{api_base}/api/health", timeout=(min(connect_timeout_s, 5.0), min(read_timeout_s, 5.0)))
        r.raise_for_status()
    except Exception as e:
        print(f"[startup] backend health check failed ({api_base}/api/health): {e}")

    while True:
        ts = datetime.now(timezone.utc).isoformat()
        wifi = scan_wifi()

        # Location: cache it so we don't hammer gpsd every loop.
        now = time.time()
        if (cached_location is None) or (now - last_location_at >= location_interval_s):
            cached_location = get_location(gpsd_enabled=gpsd_enabled)
            last_location_at = now

        # Always try to capture ping (avg + jitter).
        ping = ping_stats_ms(ping_host, count=ping_count, timeout_s=ping_timeout_s)

        speedtest = None
        if speedtest_enabled and (now - last_speedtest_at >= speedtest_interval_s):
            speedtest = run_speedtest_cli(timeout_s=speedtest_timeout_s)
            last_speedtest_at = now

        # backend_js expects speed to always contain throughput fields.
        speed: dict | None = None
        if ping is not None:
            speed = {
                "download_mbps": 0.0,
                "upload_mbps": 0.0,
                "ping_ms": float(ping["ping_ms"]),
                "jitter_ms": float(ping["jitter_ms"]),
            }
        elif speedtest is not None:
            # If ICMP ping is blocked, at least store speedtest ping.
            speed = {
                "download_mbps": 0.0,
                "upload_mbps": 0.0,
                "ping_ms": float(speedtest["ping_ms"]),
                "jitter_ms": 0.0,
            }

        if speedtest is not None and speed is not None:
            speed["download_mbps"] = float(speedtest["download_mbps"])
            speed["upload_mbps"] = float(speedtest["upload_mbps"])

        payload: dict = {"device_id": device_id, "ts": ts, "wifi": wifi}
        if speed is not None:
            payload["speed"] = speed
        if cached_location is not None:
            payload["location"] = cached_location

        try:
            r = requests.post(
                f"{api_base}/api/ingest/pi",
                json=payload,
                timeout=(connect_timeout_s, read_timeout_s),
            )
            r.raise_for_status()
            kind = "none"
            if speed is not None:
                kind = "throughput" if (speed.get("download_mbps", 0.0) > 0 or speed.get("upload_mbps", 0.0) > 0) else "ping"
            print(
                f"[{ts}] sent ok: wifi={len(wifi)} speed={kind} location={'yes' if cached_location else 'no'}"
            )
        except Exception as e:
            print(
                f"[{ts}] send failed (api_base={api_base} connect_timeout={connect_timeout_s}s read_timeout={read_timeout_s}s): {e}"
            )

        time.sleep(interval_s)


if __name__ == "__main__":
    main()
