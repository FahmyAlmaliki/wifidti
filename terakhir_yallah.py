from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from math import sqrt

import requests


# =========================================================
# BASIC UTIL
# =========================================================

def _run(cmd: list[str], *, timeout: int = 30) -> str:
    return subprocess.check_output(
        cmd,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        text=True
    )


def _has_cmd(name: str) -> bool:
    try:
        _run(["bash", "-lc", f"command -v {name}"], timeout=5)
        return True
    except Exception:
        return False


def _nmcli(*args: str, timeout: int = 30) -> str:
    return _run(["nmcli", *args], timeout=timeout)


# =========================================================
# WIFI
# =========================================================

@dataclass
class WifiProfile:
    label: str
    con_name: str
    expected_ssid: str


def connected_wifi_ssid(iface: str) -> str | None:
    try:
        out = _run(["iw", "dev", iface, "link"], timeout=10)

        for line in out.splitlines():
            line = line.strip()

            if line.startswith("SSID:"):
                return line.split("SSID:", 1)[1].strip()

            if "Not connected" in line:
                return None

    except Exception:
        pass

    try:
        out = _run(["iwgetid", "-r"], timeout=10).strip()
        return out or None
    except Exception:
        return None


def connect_wifi(
    wifi: WifiProfile,
    *,
    iface: str,
    timeout_s: int = 45,
) -> tuple[bool, str | None]:

    try:
        _nmcli("dev", "disconnect", iface, timeout=15)
    except Exception:
        pass

    try:
        out = _nmcli(
            "con",
            "up",
            wifi.con_name,
            "ifname",
            iface,
            timeout=timeout_s
        )

        print(out.strip())

    except subprocess.CalledProcessError as e:
        text = (getattr(e, "output", "") or "").strip()
        return False, text

    except Exception as e:
        return False, str(e)

    start = time.time()

    while (time.time() - start) < timeout_s:
        ssid = connected_wifi_ssid(iface)

        if ssid == wifi.expected_ssid:
            return True, None

        time.sleep(1.5)

    return False, "timeout waiting SSID"


# =========================================================
# PING
# =========================================================

def ping_stats_ms(
    host: str = "8.8.8.8",
    *,
    count: int = 4,
    timeout_s: int = 20,
) -> dict | None:

    try:
        out = _run(
            ["ping", "-c", str(count), "-n", host],
            timeout=timeout_s
        )
    except Exception:
        return None

    packet_loss = None

    for line in out.splitlines():
        if "packet loss" in line:
            m = re.search(
                r"([0-9]+(?:\.[0-9]+)?)%\s+packet loss",
                line
            )

            if m:
                try:
                    packet_loss = float(m.group(1))
                except Exception:
                    pass

    for line in out.splitlines():
        if ("min/avg" in line) and ("=" in line):
            try:
                stats = line.split("=")[1].strip().split()[0]
                _min_s, avg_s, _max_s, dev_s = stats.split("/")

                payload = {
                    "ping_ms": float(avg_s),
                    "jitter_ms": float(dev_s),
                }

                if packet_loss is not None:
                    payload["packet_loss"] = packet_loss

                return payload

            except Exception:
                pass

    samples: list[float] = []

    for line in out.splitlines():
        if " time=" not in line:
            continue

        try:
            val = float(
                line.split(" time=")[1].split()[0]
            )

            samples.append(val)

        except Exception:
            pass

    if not samples:
        return None

    avg = sum(samples) / len(samples)

    var = sum((x - avg) ** 2 for x in samples) / len(samples)

    payload = {
        "ping_ms": avg,
        "jitter_ms": sqrt(var),
    }

    if packet_loss is not None:
        payload["packet_loss"] = packet_loss

    return payload


# =========================================================
# SPEEDTEST
# =========================================================

def run_speedtest_cli(
    *,
    timeout_s: int = 120,
) -> tuple[dict | None, str | None]:

    cmds = [
        ["speedtest-cli", "--json", "--secure"],
        ["speedtest", "--json", "--secure"],
    ]

    last_err = None

    for cmd in cmds:

        try:
            out = _run(cmd, timeout=timeout_s)

        except FileNotFoundError:
            last_err = f"{cmd[0]} not found"
            continue

        except subprocess.CalledProcessError as e:
            text = (getattr(e, "output", "") or "").strip()
            last_err = text or f"{cmd[0]} failed"
            continue

        except Exception as e:
            last_err = str(e)
            continue

        try:
            start = out.find("{")
            end = out.rfind("}")

            data = json.loads(out[start:end + 1])

            return (
                {
                    "download_mbps": float(data["download"]) / 1e6,
                    "upload_mbps": float(data["upload"]) / 1e6,
                    "ping_ms": float(data["ping"]),
                },
                None,
            )

        except Exception as e:
            last_err = str(e)

    return None, last_err


# =========================================================
# SEND RESULT
# =========================================================

def send_result(
    *,
    api_base: str,
    device_id: str,
    source: str,
    test: str,
    wifi_ssid: str | None,
    speed: dict,
):

    payload = {
        "device_id": device_id,
        "source": source,
        "test": test,
        "wifi_ssid": wifi_ssid,
        "ts": datetime.now(timezone.utc).isoformat(),
        "speed": speed,
        "wifi": [],
    }

    r = requests.post(
        f"{api_base}/api/ingest/pi",
        json=payload,
        timeout=(5, 20),
    )

    r.raise_for_status()


# =========================================================
# MAIN
# =========================================================

def main():

    p = argparse.ArgumentParser()

    p.add_argument(
        "--iface",
        default="wlan0",
    )

    p.add_argument(
        "--speedtest",
        action="store_true",
    )

    p.add_argument(
        "--api-base",
        default=os.getenv("API_BASE", "http://localhost:8000"),
    )

    p.add_argument(
        "--device-id",
        default=os.getenv("DEVICE_ID", "pi-01"),
    )

    p.add_argument(
        "--source",
        default="pi",
    )

    p.add_argument(
        "--test",
        default=os.getenv("TEST", "raspi"),
        help="Nilai tag 'test' untuk net_perf (default: raspi)",
    )

    p.add_argument(
        "--cycles",
        type=int,
        default=0,
    )

    p.add_argument(
        "--pause-seconds",
        type=float,
        default=5,
    )

    p.add_argument(
        "--no-send",
        action="store_true",
    )

    args = p.parse_args()

    args.api_base = str(args.api_base or "").rstrip("/")

    if not _has_cmd("nmcli"):
        raise SystemExit("nmcli tidak ada")

    if not _has_cmd("iw"):
        raise SystemExit("iw tidak ada")

    # =========================================
    # INPUT MANUAL
    # =========================================

    wifi_a = WifiProfile(
        label="wifi_a",
        con_name=input("Connection A: ").strip(),
        expected_ssid=input("SSID A: ").strip(),
    )

    wifi_b = WifiProfile(
        label="wifi_b",
        con_name=input("Connection B: ").strip(),
        expected_ssid=input("SSID B: ").strip(),
    )

    wifis = [wifi_a, wifi_b]

    cycle = 0

    while True:

        cycle += 1

        for wifi in wifis:

            print("\n================================================")
            print(f"SWITCHING -> {wifi.con_name}")
            print("================================================")

            ok, err = connect_wifi(
                wifi,
                iface=args.iface,
            )

            if not ok:
                print(f"CONNECT FAILED: {err}")
                continue

            time.sleep(args.pause_seconds)

            ssid = connected_wifi_ssid(args.iface)

            print(f"CONNECTED SSID: {ssid}")

            ping = ping_stats_ms()

            speed = {}

            if ping:
                speed.update(ping)

            if args.speedtest:

                st, st_err = run_speedtest_cli()

                if st:
                    speed.update(st)

                else:
                    print(f"SPEEDTEST FAILED: {st_err}")

            if not speed:
                print("NO DATA")
                continue

            speed["wifi_ssid"] = ssid

            print("\nRESULT")
            print("--------------------------------")

            print(f"Download : {speed.get('download_mbps', '-')}")
            print(f"Upload   : {speed.get('upload_mbps', '-')}")
            print(f"Ping     : {speed.get('ping_ms', '-')}")
            print(f"Jitter   : {speed.get('jitter_ms', '-')}")
            print(f"Loss     : {speed.get('packet_loss', '-')}")

            if args.no_send:
                continue

            try:
                send_result(
                    api_base=args.api_base,
                    device_id=args.device_id,
                    source=args.source,
                    test=args.test,
                    wifi_ssid=ssid,
                    speed=speed,
                )

                print("SEND OK")

            except Exception as e:
                print(f"SEND FAILED: {e}")

        if args.cycles and cycle >= args.cycles:
            break


if __name__ == "__main__":
    main()
