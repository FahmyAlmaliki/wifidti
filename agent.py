from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone

import requests


def _run(cmd: list[str], timeout: int = 20) -> str:
    return subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=timeout, text=True)


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


def ping_latency_ms(host: str = "8.8.8.8") -> float | None:
    try:
        out = _run(["ping", "-c", "4", "-n", host], timeout=20)
        # rtt min/avg/max/mdev = 7.123/8.456/9.001/0.321 ms
        for line in out.splitlines():
            if "min/avg" in line and "=" in line:
                stats = line.split("=")[1].strip().split()[0]
                min_s, avg_s, max_s, mdev_s = stats.split("/")
                return float(avg_s)
    except Exception:
        return None
    return None


def run_speedtest() -> dict | None:
    try:
        import speedtest

        st = speedtest.Speedtest()
        st.get_best_server()
        down_bps = st.download()
        up_bps = st.upload()
        ping_ms = float(st.results.ping)

        return {
            "download_mbps": down_bps / 1e6,
            "upload_mbps": up_bps / 1e6,
            "ping_ms": ping_ms,
        }
    except Exception:
        return None


def main() -> None:
    api_base = os.getenv("API_BASE", "http://10.39.30.150:8000").rstrip("/")
    device_id = os.getenv("DEVICE_ID", "pi-01")
    interval_s = int(os.getenv("INTERVAL_SECONDS", "60"))

    connect_timeout_s = float(os.getenv("HTTP_CONNECT_TIMEOUT", "5"))
    read_timeout_s = float(os.getenv("HTTP_READ_TIMEOUT", "15"))

    # Preflight: helps distinguish "backend not reachable" vs "ingest is slow".
    try:
        r = requests.get(f"{api_base}/api/health", timeout=(min(connect_timeout_s, 5.0), min(read_timeout_s, 5.0)))
        r.raise_for_status()
    except Exception as e:
        print(f"[startup] backend health check failed ({api_base}/api/health): {e}")

    while True:
        ts = datetime.now(timezone.utc).isoformat()
        wifi = scan_wifi()

        speed = None
        st = run_speedtest()
        if st:
            # Jitter not provided by speedtest-cli; keep 0 and use ping jitter in web.
            speed = {
                "download_mbps": float(st["download_mbps"]),
                "upload_mbps": float(st["upload_mbps"]),
                "ping_ms": float(st["ping_ms"]),
                "jitter_ms": 0.0,
            }
        else:
            lat = ping_latency_ms()
            if lat is not None:
                speed = {"download_mbps": 0.0, "upload_mbps": 0.0, "ping_ms": float(lat), "jitter_ms": 0.0}

        payload = {"device_id": device_id, "ts": ts, "wifi": wifi}
        if speed is not None:
            payload["speed"] = speed

        try:
            r = requests.post(
                f"{api_base}/api/ingest/pi",
                json=payload,
                timeout=(connect_timeout_s, read_timeout_s),
            )
            r.raise_for_status()
            print(f"[{ts}] sent ok: wifi={len(wifi)} speed={'yes' if speed else 'no'}")
        except Exception as e:
            print(
                f"[{ts}] send failed (api_base={api_base} connect_timeout={connect_timeout_s}s read_timeout={read_timeout_s}s): {e}"
            )

        time.sleep(interval_s)


if __name__ == "__main__":
    main()
