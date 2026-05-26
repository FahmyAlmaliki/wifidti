from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from getpass import getpass
from math import sqrt

import requests


def _run(cmd: list[str], *, timeout: int = 30) -> str:
    return subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=timeout, text=True)


def _has_cmd(name: str) -> bool:
    try:
        _run(["bash", "-lc", f"command -v {name}"], timeout=5)
        return True
    except Exception:
        return False


def connected_wifi_ssid(iface: str = "wlan0") -> str | None:
    try:
        out = _run(["iw", "dev", iface, "link"], timeout=8)
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("SSID:"):
                ssid = line.split("SSID:", 1)[1].strip()
                return ssid or None
            if "Not connected" in line:
                return None
    except Exception:
        pass

    try:
        out = _run(["iwgetid", "-r"], timeout=8).strip()
        return out or None
    except Exception:
        pass

    return None


def ping_stats_ms(host: str = "8.8.8.8", *, count: int = 4, timeout_s: int = 20) -> dict | None:
    try:
        out = _run(["ping", "-c", str(count), "-n", host], timeout=timeout_s)
    except Exception:
        return None

    packet_loss = None
    for line in out.splitlines():
        if "packet loss" not in line:
            continue
        m = re.search(r"([0-9]+(?:\.[0-9]+)?)%\s+packet loss", line)
        if m:
            try:
                packet_loss = float(m.group(1))
                break
            except Exception:
                pass

    for line in out.splitlines():
        if ("min/avg" in line) and ("=" in line):
            try:
                stats = line.split("=", 1)[1].strip().split()[0]
                _min_s, avg_s, _max_s, dev_s = stats.split("/")
                payload = {"ping_ms": float(avg_s), "jitter_ms": float(dev_s)}
                if packet_loss is not None:
                    payload["packet_loss"] = float(packet_loss)
                return payload
            except Exception:
                pass

    samples: list[float] = []
    for line in out.splitlines():
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

    if packet_loss is None and count > 0:
        try:
            packet_loss = max(0.0, min(100.0, (count - len(samples)) * 100.0 / float(count)))
        except Exception:
            packet_loss = None

    payload = {"ping_ms": float(avg), "jitter_ms": float(sqrt(var))}
    if packet_loss is not None:
        payload["packet_loss"] = float(packet_loss)
    return payload


def run_speedtest_cli(*, timeout_s: int = 120) -> tuple[dict | None, str | None]:
    cmds = [
        ["speedtest-cli", "--json", "--secure"],
        ["speedtest", "--json", "--secure"],
    ]

    last_err: str | None = None
    for cmd in cmds:
        try:
            out = _run(cmd, timeout=timeout_s)
        except FileNotFoundError:
            last_err = f"{cmd[0]} not found"
            continue
        except subprocess.CalledProcessError as e:
            text = (getattr(e, "output", "") or "").strip()
            last_err = f"{cmd[0]} exit {e.returncode}: {text[-400:]}" if text else f"{cmd[0]} exit {e.returncode}"
            continue
        except Exception as e:
            last_err = f"{cmd[0]} failed: {e}"
            continue

        text = out.strip()
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            last_err = f"{cmd[0]} returned non-JSON output"
            continue

        try:
            data = json.loads(text[start : end + 1])
            download_bps = float(data.get("download"))
            upload_bps = float(data.get("upload"))
            ping_ms = float(data.get("ping"))
            return (
                {
                    "download_mbps": download_bps / 1e6,
                    "upload_mbps": upload_bps / 1e6,
                    "ping_ms": ping_ms,
                },
                None,
            )
        except Exception as e:
            last_err = f"{cmd[0]} JSON parse failed: {e}"
            continue

    return None, last_err


def run_speedtest_lib() -> tuple[dict | None, str | None]:
    try:
        import speedtest  # type: ignore

        st = speedtest.Speedtest()
        st.get_best_server()
        down_bps = st.download()
        up_bps = st.upload()
        ping_ms = float(st.results.ping)
        return (
            {
                "download_mbps": down_bps / 1e6,
                "upload_mbps": up_bps / 1e6,
                "ping_ms": ping_ms,
            },
            None,
        )
    except Exception as e:
        return None, str(e)


@dataclass
class EnterpriseWifi:
    label: str
    ssid: str
    identity: str
    password: str
    anonymous_identity: str | None = None
    eap: str = "peap"
    phase2: str = "mschapv2"
    ca_cert: str | None = None
    domain_suffix: str | None = None

    @property
    def con_name(self) -> str:
        safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", self.label.strip())
        return f"autotest-{safe}" if safe else "autotest"


def _nmcli(*args: str, timeout: int = 30) -> str:
    return _run(["nmcli", *args], timeout=timeout)


def ensure_nmcli_enterprise_connection(w: EnterpriseWifi, *, iface: str) -> None:
    existing = ""
    try:
        existing = _nmcli("-t", "-f", "NAME", "con", "show", timeout=20)
    except Exception:
        existing = ""

    if w.con_name not in {line.strip() for line in existing.splitlines() if line.strip()}:
        _nmcli(
            "con",
            "add",
            "type",
            "wifi",
            "ifname",
            iface,
            "con-name",
            w.con_name,
            "ssid",
            w.ssid,
            timeout=30,
        )

    # Basic WiFi + enterprise.
    _nmcli("con", "modify", w.con_name, "connection.autoconnect", "no")
    _nmcli("con", "modify", w.con_name, "wifi-sec.key-mgmt", "wpa-eap")

    # 802.1x settings. Most common for eduroam/enterprise: PEAP + MSCHAPv2.
    _nmcli("con", "modify", w.con_name, "802-1x.eap", w.eap)
    _nmcli("con", "modify", w.con_name, "802-1x.identity", w.identity)
    _nmcli("con", "modify", w.con_name, "802-1x.password", w.password)

    if w.anonymous_identity is not None:
        _nmcli("con", "modify", w.con_name, "802-1x.anonymous-identity", w.anonymous_identity)

    if w.phase2:
        # nmcli expects e.g. "mschapv2" for PEAP.
        _nmcli("con", "modify", w.con_name, "802-1x.phase2-auth", w.phase2)

    if w.domain_suffix:
        _nmcli("con", "modify", w.con_name, "802-1x.domain-suffix-match", w.domain_suffix)

    if w.ca_cert:
        _nmcli("con", "modify", w.con_name, "802-1x.ca-cert", w.ca_cert)
    else:
        # Use system CA store (works for many deployments; for strict eduroam you may still want ca_cert).
        _nmcli("con", "modify", w.con_name, "802-1x.system-ca-certs", "yes")


def connect_wifi(w: EnterpriseWifi, *, iface: str, connect_timeout_s: int = 45) -> tuple[bool, str | None]:
    # Disconnect first to reduce sticky behavior.
    try:
        _nmcli("dev", "disconnect", iface, timeout=15)
    except Exception:
        pass

    try:
        _nmcli("con", "up", w.con_name, "ifname", iface, timeout=connect_timeout_s)
    except subprocess.CalledProcessError as e:
        text = (getattr(e, "output", "") or "").strip()
        return False, text[-500:] if text else f"nmcli exited {e.returncode}"
    except Exception as e:
        return False, str(e)

    start = time.time()
    last_state = None
    while time.time() - start < connect_timeout_s:
        ssid = connected_wifi_ssid(iface)
        if ssid and ssid == w.ssid:
            return True, None
        try:
            dev = _nmcli("-t", "-f", "GENERAL.STATE", "dev", "show", iface, timeout=10)
            last_state = dev.strip()
        except Exception:
            pass
        time.sleep(1.5)

    return False, f"timeout waiting for SSID {w.ssid} (state={last_state})"


def send_result(
    *,
    api_base: str,
    device_id: str,
    source: str,
    test: str,
    speed: dict,
    wifi_ssid: str | None,
    timeout: tuple[float, float],
) -> None:
    payload: dict = {
        "device_id": device_id,
        "source": source,
        "test": test,
        "ts": datetime.now(timezone.utc).isoformat(),
        "speed": speed,
        "wifi": [],
    }
    if wifi_ssid:
        payload["wifi_ssid"] = wifi_ssid
        payload["speed"]["wifi_ssid"] = wifi_ssid

    r = requests.post(f"{api_base}/api/ingest/pi", json=payload, timeout=timeout)
    r.raise_for_status()


def _load_config(path: str) -> list[EnterpriseWifi]:
    with open(path, "r", encoding="utf-8") as f:
        obj = json.load(f)
    nets = obj.get("networks") or obj.get("wifi") or obj.get("wifis")
    if not isinstance(nets, list) or len(nets) != 2:
        raise SystemExit("config harus berisi 'networks' list dengan tepat 2 item")

    out: list[EnterpriseWifi] = []
    for item in nets:
        if not isinstance(item, dict):
            raise SystemExit("config networks item harus object")

        label = str(item.get("label") or item.get("name") or item.get("ssid") or "").strip()
        ssid = str(item.get("ssid") or "").strip()
        identity = str(item.get("identity") or item.get("username") or "").strip()

        password = None
        if item.get("password"):
            password = str(item.get("password"))
        elif item.get("password_env"):
            password = os.getenv(str(item.get("password_env")) or "")
        if not password:
            raise SystemExit(f"password belum ada untuk network '{label or ssid}'")

        out.append(
            EnterpriseWifi(
                label=label or ssid,
                ssid=ssid,
                identity=identity,
                password=password,
                anonymous_identity=(item.get("anonymous_identity") if item.get("anonymous_identity") is not None else None),
                eap=str(item.get("eap") or "peap"),
                phase2=str(item.get("phase2") or item.get("phase2_auth") or "mschapv2"),
                ca_cert=(str(item.get("ca_cert")) if item.get("ca_cert") else None),
                domain_suffix=(str(item.get("domain_suffix")) if item.get("domain_suffix") else None),
            )
        )

    return out


def main() -> None:
    p = argparse.ArgumentParser(description="Test 2 enterprise WiFi bergantian lalu kirim hasil ke backend (opsional).")
    p.add_argument("--config", help="Path JSON config (punya 2 network)")

    p.add_argument("--ssid-a", help="SSID WiFi A")
    p.add_argument("--identity-a", help="Identity/username WiFi A (mis. user@domain)")
    p.add_argument("--label-a", default="wifi_a", help="Label test untuk WiFi A (tag 'test')")
    p.add_argument("--anonymous-a", default=None, help="Anonymous identity (optional)")
    p.add_argument("--eap-a", default="peap", help="EAP method (default: peap)")
    p.add_argument("--phase2-a", default="mschapv2", help="Phase2 auth (default: mschapv2)")
    p.add_argument("--ca-cert-a", default=None, help="Path CA cert file (optional)")
    p.add_argument("--domain-suffix-a", default=None, help="domain-suffix-match (optional)")

    p.add_argument("--ssid-b", help="SSID WiFi B")
    p.add_argument("--identity-b", help="Identity/username WiFi B")
    p.add_argument("--label-b", default="wifi_b", help="Label test untuk WiFi B (tag 'test')")
    p.add_argument("--anonymous-b", default=None)
    p.add_argument("--eap-b", default="peap")
    p.add_argument("--phase2-b", default="mschapv2")
    p.add_argument("--ca-cert-b", default=None)
    p.add_argument("--domain-suffix-b", default=None)

    p.add_argument("--iface", default=os.getenv("WIFI_IFACE", "wlan0"), help="Interface wifi (default: wlan0)")
    p.add_argument("--cycles", type=int, default=0, help="Jumlah siklus (1 siklus = test A + test B). 0=jalan terus")
    p.add_argument("--pause-seconds", type=float, default=3.0, help="Jeda kecil setelah switch wifi")

    p.add_argument("--ping-host", default=os.getenv("PING_HOST", "8.8.8.8"))
    p.add_argument("--ping-count", type=int, default=int(os.getenv("PING_COUNT", "4")))
    p.add_argument("--ping-timeout", type=int, default=int(os.getenv("PING_TIMEOUT_SECONDS", "20")))

    p.add_argument("--speedtest", action="store_true", help="Jalankan speedtest juga")
    p.add_argument("--speedtest-timeout", type=int, default=int(os.getenv("SPEEDTEST_TIMEOUT_SECONDS", "120")))

    p.add_argument("--api-base", default=os.getenv("API_BASE", "http://localhost:8000").rstrip("/"))
    p.add_argument("--device-id", default=os.getenv("DEVICE_ID", "pi-01"))
    p.add_argument("--source", default=os.getenv("SOURCE", "pi"))
    p.add_argument("--no-send", action="store_true", help="Jangan kirim ke backend (hanya print)")

    p.add_argument("--connect-timeout", type=int, default=int(os.getenv("WIFI_CONNECT_TIMEOUT", "45")))
    p.add_argument("--http-connect-timeout", type=float, default=float(os.getenv("HTTP_CONNECT_TIMEOUT", "5")))
    p.add_argument("--http-read-timeout", type=float, default=float(os.getenv("HTTP_READ_TIMEOUT", "20")))

    args = p.parse_args()

    if not _has_cmd("nmcli"):
        raise SystemExit("butuh NetworkManager (nmcli). Install/enable dulu: sudo apt install network-manager")
    if not _has_cmd("iw"):
        raise SystemExit("butuh 'iw' untuk cek SSID. Install: sudo apt install iw")

    if args.config:
        wifis = _load_config(args.config)
    else:
        ssid_a = (args.ssid_a or os.getenv("SSID_A") or "").strip() or input("SSID A: ").strip()
        ssid_b = (args.ssid_b or os.getenv("SSID_B") or "").strip() or input("SSID B: ").strip()

        identity_a = (args.identity_a or os.getenv("IDENTITY_A") or "").strip() or input(f"Identity A ({ssid_a}): ").strip()
        identity_b = (args.identity_b or os.getenv("IDENTITY_B") or "").strip() or input(f"Identity B ({ssid_b}): ").strip()

        pw_a = os.getenv("PASSWORD_A") or getpass(f"Password A ({ssid_a}): ")
        pw_b = os.getenv("PASSWORD_B") or getpass(f"Password B ({ssid_b}): ")

        wifis = [
            EnterpriseWifi(
                label=(args.label_a or ssid_a),
                ssid=ssid_a,
                identity=identity_a,
                password=pw_a,
                anonymous_identity=args.anonymous_a,
                eap=args.eap_a,
                phase2=args.phase2_a,
                ca_cert=args.ca_cert_a,
                domain_suffix=args.domain_suffix_a,
            ),
            EnterpriseWifi(
                label=(args.label_b or ssid_b),
                ssid=ssid_b,
                identity=identity_b,
                password=pw_b,
                anonymous_identity=args.anonymous_b,
                eap=args.eap_b,
                phase2=args.phase2_b,
                ca_cert=args.ca_cert_b,
                domain_suffix=args.domain_suffix_b,
            ),
        ]

    # Prepare nmcli profiles.
    for w in wifis:
        if not w.ssid or not w.identity or not w.password:
            raise SystemExit(f"network tidak lengkap: label={w.label} ssid={w.ssid} identity={w.identity}")
        ensure_nmcli_enterprise_connection(w, iface=args.iface)

    timeout = (args.http_connect_timeout, args.http_read_timeout)
    cycle = 0
    while True:
        cycle += 1
        for w in wifis:
            print(f"\n[{datetime.now(timezone.utc).isoformat()}] switching -> {w.label} (SSID={w.ssid})")
            ok, err = connect_wifi(w, iface=args.iface, connect_timeout_s=args.connect_timeout)
            if not ok:
                print(f"  connect failed: {err}")
                time.sleep(max(1.0, args.pause_seconds))
                continue

            time.sleep(max(0.5, args.pause_seconds))
            wifi_ssid = connected_wifi_ssid(args.iface)
            print(f"  connected ssid={wifi_ssid or '-'}")

            ping = ping_stats_ms(args.ping_host, count=args.ping_count, timeout_s=args.ping_timeout)
            speed: dict | None = None
            if ping is not None:
                speed = {
                    "ping_ms": float(ping.get("ping_ms") or 0.0),
                    "jitter_ms": float(ping.get("jitter_ms") or 0.0),
                }
                if ping.get("packet_loss") is not None:
                    speed["packet_loss"] = float(ping["packet_loss"])

            if args.speedtest:
                st, st_err = run_speedtest_cli(timeout_s=args.speedtest_timeout)
                if st is None:
                    st, lib_err = run_speedtest_lib()
                    if st is None:
                        st_err = (st_err or "") + ("; " if st_err else "") + (f"lib: {lib_err}" if lib_err else "")

                if st is not None:
                    if speed is None:
                        speed = {"ping_ms": float(st["ping_ms"]), "jitter_ms": 0.0}
                    speed["download_mbps"] = float(st["download_mbps"])
                    speed["upload_mbps"] = float(st["upload_mbps"])
                elif st_err:
                    print(f"  speedtest failed: {st_err}")

            if not speed:
                print("  no metrics (ping/speedtest failed)")
                continue

            if wifi_ssid:
                speed["wifi_ssid"] = wifi_ssid

            print(
                "  result: "
                f"dl={speed.get('download_mbps','-')}Mbps "
                f"ul={speed.get('upload_mbps','-')}Mbps "
                f"ping={speed.get('ping_ms','-')}ms "
                f"jitter={speed.get('jitter_ms','-')}ms "
                f"loss={speed.get('packet_loss','-')}%"
            )

            if args.no_send:
                continue

            try:
                send_result(
                    api_base=args.api_base,
                    device_id=args.device_id,
                    source=args.source,
                    test=w.label,
                    speed=speed,
                    wifi_ssid=wifi_ssid,
                    timeout=timeout,
                )
                print("  sent ok")
            except Exception as e:
                print(f"  send failed: {e}")

        if args.cycles and cycle >= args.cycles:
            break


if __name__ == "__main__":
    main()
