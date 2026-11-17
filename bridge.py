#!/usr/bin/env python3
import asyncio
import json
import logging
import os
import re
import signal
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from aiohttp import web
from kasa import (
    AuthenticationError,
    Device,
    DeviceConfig,
    Discover,
    Module,
    UnsupportedDeviceError,
)
from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("sigen-tapo-bridge")

BASE_DIR = Path(__file__).parent
PROCESS_START = time.monotonic()

# Identity of the emulated device. Used by both Shelly.GetDeviceInfo and the mDNS TXT records.
SHELLY_MODEL = "SNSW-001P16EU"
SHELLY_APP = "Plus1PM"
SHELLY_VER = "1.4.4"
SHELLY_FW_ID = "20241011-114455/1.4.4-g6d2a586"

MAX_BACKOFF_SECONDS = 60.0
BIND_RETRY_SECONDS = 30.0
STATE_SAVE_INTERVAL_SECONDS = 300.0

# Shelly Gen2 RPC error codes (https://shelly-api-docs.shelly.cloud/gen2/General/CommonErrors/)
ERR_INVALID_ARGUMENT = -103
ERR_NOT_FOUND = -105
ERR_UNAVAILABLE = -114
ERR_NO_HANDLER = 404


class RpcError(Exception):
    def __init__(self, code: int, message: str, http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


@dataclass
class PlugState:
    name: str
    tapo_ip: str
    virtual_shelly_ip: str
    virtual_mac: str
    is_on: bool = False
    apower: float = 0.0          # Active power in Watts
    voltage: float = 230.0       # Voltage in Volts (NZ nominal 230V)
    current: float = 0.0         # Current in Amps
    aenergy_total_wh: float = 0.0  # Cumulative energy in Watt-hours (integrated by the bridge)
    by_minute_mwh: List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])  # Newest first
    online: bool = False
    device: Any = field(default=None, repr=False)

    @property
    def shelly_id(self) -> str:
        return f"shellyplus1pm-{self.virtual_mac.lower()}"


class EnergyMeter:
    """Integrates power samples into a cumulative Wh total plus Shelly-style per-minute buckets.

    python-kasa returns None for consumption_total on Tapo plugs, so the bridge keeps its own
    counter. Gaps longer than max_gap_seconds (plug offline, bridge stalled) are not integrated.
    """

    def __init__(self, state: PlugState, max_gap_seconds: float):
        self.state = state
        self.max_gap_seconds = max_gap_seconds
        self._last_sample: Optional[tuple[float, float]] = None  # (monotonic time, watts)
        self._minute_start = 0
        self._minute_mwh = 0.0

    def reset_sample(self) -> None:
        self._last_sample = None

    def add_sample(self, watts: float) -> None:
        now = time.monotonic()
        last, self._last_sample = self._last_sample, (now, watts)
        self.roll_minutes()
        if last is None:
            return
        dt = now - last[0]
        if dt <= 0 or dt > self.max_gap_seconds:
            return
        wh = (last[1] + watts) / 2.0 * dt / 3600.0
        self.state.aenergy_total_wh += wh
        self._minute_mwh += wh * 1000.0

    def roll_minutes(self) -> None:
        minute = int(time.time()) // 60 * 60
        if minute == self._minute_start:
            return
        if self._minute_start:
            elapsed = (minute - self._minute_start) // 60
            completed = ([round(self._minute_mwh, 3)] + [0.0] * (elapsed - 1))[-3:]
            for value in completed:
                self.state.by_minute_mwh = [value] + self.state.by_minute_mwh[:2]
        self._minute_start = minute
        self._minute_mwh = 0.0


class StateStore:
    """Persists cumulative energy totals so the Shelly counter never goes backwards across restarts."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception as exc:
            log.error("Could not read state file %s (%s); energy totals start at 0", self.path, exc)
            return {}

    def save(self, states: List[PlugState]) -> None:
        data = {s.virtual_mac.upper(): {"aenergy_total_wh": round(s.aenergy_total_wh, 3)} for s in states}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception as exc:
            log.error("Could not write state file %s: %s", self.path, exc)


class TapoShellyNode:
    def __init__(
        self,
        state: PlugState,
        username: str,
        password: str,
        poll_interval: float,
        log_requests: bool = False,
    ):
        self.state = state
        self.username = username
        self.password = password
        self.poll_interval = poll_interval
        self.log_requests = log_requests
        self.meter = EnergyMeter(state, max_gap_seconds=poll_interval * 4 + 10)
        self.runner: Optional[web.AppRunner] = None
        self.aiozc: Optional[AsyncZeroconf] = None
        self.services: list[ServiceInfo] = []
        self._stop_event = asyncio.Event()
        self._device_config: Optional[DeviceConfig] = None  # Cached after first discovery
        self._failures = 0

    async def _sleep(self, seconds: float) -> bool:
        """Wait for the given time; return True if the node is stopping."""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        return self._stop_event.is_set()

    # --- Tapo side ---

    @staticmethod
    async def _safe_disconnect(dev: Device) -> None:
        try:
            await dev.disconnect()
        except Exception as exc:
            log.debug("Ignoring error while disconnecting: %s", exc)

    @staticmethod
    def _connect_hint(exc: Exception) -> str:
        if isinstance(exc, AuthenticationError):
            return (
                " - if these credentials work for other plugs, remove and re-add this plug in the "
                "Tapo app with Third-Party Compatibility already enabled"
            )
        if isinstance(exc, UnsupportedDeviceError) and "TPAP" in str(exc):
            return " - enable Third-Party Compatibility in the Tapo app (Me > Third-Party Services)"
        return ""

    async def _connect_tapo(self) -> bool:
        dev: Optional[Device] = None
        used_cache = self._device_config is not None
        try:
            if used_cache:
                dev = await Device.connect(config=self._device_config)
            else:
                dev = await Discover.discover_single(
                    self.state.tapo_ip,
                    username=self.username,
                    password=self.password,
                )
                if dev is None:
                    raise ConnectionError("no discovery response")
            await dev.update()
        except Exception as exc:
            if dev is not None:
                await self._safe_disconnect(dev)
            # A stale cached config (e.g. plug re-onboarded) is dropped so the next attempt rediscovers.
            self._device_config = None
            log.warning(
                "[%s] Failed connecting to %s%s: %s%s",
                self.state.name,
                self.state.tapo_ip,
                " (cached config)" if used_cache else "",
                exc,
                self._connect_hint(exc),
            )
            return False

        self._device_config = dev.config
        self.state.device = dev
        self.state.online = True
        log.info("[%s] Connected to Tapo plug at %s (%s)", self.state.name, self.state.tapo_ip, dev.model)
        return True

    async def _mark_offline(self) -> None:
        dev, self.state.device = self.state.device, None
        self.state.online = False
        self.state.apower = 0.0
        self.state.current = 0.0
        self.meter.reset_sample()
        if dev is not None:
            await self._safe_disconnect(dev)

    def _read_device(self, dev: Device) -> None:
        self.state.is_on = bool(dev.is_on)
        energy = dev.modules.get(Module.Energy)
        if energy is not None:
            if energy.current_consumption is not None:
                self.state.apower = round(float(energy.current_consumption), 2)
            if energy.voltage is not None:
                self.state.voltage = round(float(energy.voltage), 1)
            if energy.current is not None:
                self.state.current = round(float(energy.current), 3)
        self.meter.add_sample(self.state.apower)

    async def poll_loop(self) -> None:
        while not self._stop_event.is_set():
            if self.state.device is None:
                # _connect_tapo already runs update(), so read straight from the fresh device.
                if await self._connect_tapo():
                    self._read_device(self.state.device)
                    self._failures = 0
                else:
                    self._failures += 1
            else:
                try:
                    await self.state.device.update()
                    self._read_device(self.state.device)
                    self._failures = 0
                except Exception as exc:
                    if self._stop_event.is_set():
                        break
                    log.warning("[%s] Poll error (%s); will reconnect", self.state.name, exc)
                    await self._mark_offline()
                    self._failures += 1

            delay = self.poll_interval
            if self._failures:
                delay = min(self.poll_interval * 2 ** self._failures, MAX_BACKOFF_SECONDS)
            await self._sleep(delay)

    # --- Shelly Gen2+ JSON-RPC Payload Builders ---

    def _device_info(self) -> Dict[str, Any]:
        return {
            "name": self.state.name,
            "id": self.state.shelly_id,
            "mac": self.state.virtual_mac.upper(),
            "slot": 0,
            "model": SHELLY_MODEL,
            "gen": 2,
            "fw_id": SHELLY_FW_ID,
            "ver": SHELLY_VER,
            "app": SHELLY_APP,
            "auth_en": False,
            "auth_domain": None,
        }

    def _switch_status(self) -> Dict[str, Any]:
        self.meter.roll_minutes()
        return {
            "id": 0,
            "source": "HTTP_in",
            "output": self.state.is_on,
            "apower": self.state.apower,
            "voltage": self.state.voltage,
            "current": self.state.current,
            "pf": 1.0 if self.state.apower > 0 else 0.0,
            "freq": 50.0,
            "aenergy": {
                "total": round(self.state.aenergy_total_wh, 3),
                "by_minute": list(self.state.by_minute_mwh),
                "minute_ts": int(time.time()) // 60 * 60,
            },
            "temperature": {"tC": 38.5, "tF": 101.3},
        }

    def _full_status(self) -> Dict[str, Any]:
        return {
            "ble": {},
            "cloud": {"connected": False},
            "input:0": {"id": 0, "state": False},
            "switch:0": self._switch_status(),
            "sys": {
                "mac": self.state.virtual_mac.upper(),
                "restart_required": False,
                "time": time.strftime("%H:%M"),
                "unixtime": int(time.time()),
                "uptime": int(time.monotonic() - PROCESS_START),
                "ram_size": 262144,
                "ram_free": 140000,
                "fs_size": 458752,
                "fs_free": 180000,
                "cfg_rev": 1,
                "available_updates": {},
            },
            "wifi": {
                "sta_ip": self.state.virtual_shelly_ip,
                "status": "got ip",
                "ssid": "LocalLAN",
                "rssi": -50,
            },
        }

    @staticmethod
    def _check_switch_id(params: Dict[str, Any]) -> None:
        if "id" in params and str(params["id"]) != "0":
            raise RpcError(ERR_NOT_FOUND, f"Bad id={params['id']}", 404)

    async def _set_switch(self, target_on: bool) -> Dict[str, Any]:
        was_on = self.state.is_on
        dev = self.state.device
        if dev is None:
            raise RpcError(ERR_UNAVAILABLE, f"Tapo plug {self.state.tapo_ip} is unreachable", 503)
        try:
            if target_on:
                await dev.turn_on()
            else:
                await dev.turn_off()
        except Exception as exc:
            log.error("[%s] Failed switching %s: %s", self.state.name, "ON" if target_on else "OFF", exc)
            raise RpcError(ERR_UNAVAILABLE, f"Failed to switch Tapo plug: {exc}", 503) from exc
        self.state.is_on = target_on
        log.info("[%s] Switched %s via Shelly RPC", self.state.name, "ON" if target_on else "OFF")
        return {"was_on": was_on}

    async def _dispatch_rpc(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if method == "Shelly.GetDeviceInfo":
            return self._device_info()
        if method == "Shelly.GetStatus":
            return self._full_status()
        if method == "Shelly.GetConfig":
            return {
                "sys": {"device": {"name": self.state.name, "mac": self.state.virtual_mac.upper()}},
                "switch:0": {"id": 0, "name": self.state.name, "initial_state": "restore_last"},
            }
        if method == "Switch.GetStatus":
            self._check_switch_id(params)
            return self._switch_status()
        if method == "Switch.Set":
            self._check_switch_id(params)
            if "on" not in params:
                raise RpcError(ERR_INVALID_ARGUMENT, "Missing required 'on'")
            return await self._set_switch(_as_bool(params["on"]))
        if method == "Switch.Toggle":
            self._check_switch_id(params)
            return await self._set_switch(not self.state.is_on)
        if method == "Sys.SetConfig":
            new_name = params.get("config", {}).get("device", {}).get("name")
            if new_name:
                self.state.name = new_name
            return {"restart_required": False}
        if method == "Sys.GetStatus":
            return self._full_status()["sys"]
        if method == "Wifi.GetStatus":
            return self._full_status()["wifi"]

        raise RpcError(ERR_NO_HANDLER, f"No handler for {method}", 404)

    # --- HTTP Endpoints ---

    def _log_request(self, request: web.Request, method: str, params: Dict[str, Any]) -> None:
        if self.log_requests:
            log.info(
                "[%s] %s %s %s method=%s params=%s",
                self.state.name,
                request.remote,
                request.method,
                request.path,
                method,
                params,
            )

    async def handle_shelly_info(self, request: web.Request) -> web.Response:
        self._log_request(request, "/shelly", {})
        return web.json_response(self._device_info())

    async def handle_rpc_path(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        params: Dict[str, Any] = dict(request.query)
        if request.can_read_body:
            try:
                body = await request.json()
            except Exception:
                return web.json_response({"code": ERR_INVALID_ARGUMENT, "message": "Invalid JSON body"}, status=400)
            if isinstance(body, dict):
                params.update(body.get("params", body))
        self._log_request(request, method, params)
        try:
            result = await self._dispatch_rpc(method, params)
        except RpcError as err:
            return web.json_response({"code": err.code, "message": err.message}, status=err.http_status)
        return web.json_response(result)

    async def handle_rpc_post(self, request: web.Request) -> web.Response:
        frame: Dict[str, Any] = {"id": None, "src": self.state.shelly_id}
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("request frame must be a JSON object")
        except Exception as exc:
            frame["error"] = {"code": ERR_INVALID_ARGUMENT, "message": f"Invalid request frame: {exc}"}
            return web.json_response(frame, status=400)

        frame["id"] = data.get("id")
        if data.get("src"):
            frame["dst"] = data["src"]
        method = data.get("method", "")
        params = data.get("params") or {}
        self._log_request(request, method, params)
        try:
            if not isinstance(params, dict):
                raise RpcError(ERR_INVALID_ARGUMENT, "params must be an object")
            frame["result"] = await self._dispatch_rpc(method, params)
        except RpcError as err:
            frame["error"] = {"code": err.code, "message": err.message}
        return web.json_response(frame)

    # --- Lifecycle (HTTP + mDNS) ---

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/shelly", self.handle_shelly_info)
        app.router.add_route("*", "/rpc/{method}", self.handle_rpc_path)
        app.router.add_post("/rpc", self.handle_rpc_post)
        return app

    async def start(self) -> None:
        self.runner = web.AppRunner(self.build_app(), access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, host=self.state.virtual_shelly_ip, port=80)
        await site.start()

        ip_bytes = socket.inet_aton(self.state.virtual_shelly_ip)
        instance_name = self.state.shelly_id
        server_fqdn = f"{instance_name}.local."
        txt_records = {
            "id": instance_name,
            "mac": self.state.virtual_mac.upper(),
            "model": SHELLY_MODEL,
            "gen": "2",
            "app": SHELLY_APP,
            "ver": SHELLY_VER,
        }

        self.aiozc = AsyncZeroconf(interfaces=[self.state.virtual_shelly_ip], ip_version=IPVersion.V4Only)
        self.services = [
            ServiceInfo(
                service_type,
                f"{instance_name}.{service_type}",
                addresses=[ip_bytes],
                port=80,
                properties=txt_records,
                server=server_fqdn,
            )
            for service_type in ("_shelly._tcp.local.", "_http._tcp.local.")
        ]
        for svc in self.services:
            await self.aiozc.async_register_service(svc)

        log.info(
            "[%s] Emulating %s on %s:80 (Tapo target: %s)",
            self.state.name,
            instance_name,
            self.state.virtual_shelly_ip,
            self.state.tapo_ip,
        )

    async def _start_with_retry(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.start()
                return
            except Exception as exc:
                log.error(
                    "[%s] Could not start emulation on %s:80 (%s); retrying in %ds",
                    self.state.name,
                    self.state.virtual_shelly_ip,
                    exc,
                    BIND_RETRY_SECONDS,
                )
                await self._shutdown_server()
                await self._sleep(BIND_RETRY_SECONDS)

    async def run(self) -> None:
        """Serve the emulated Shelly and poll the Tapo plug independently of other nodes."""
        await asyncio.gather(self._start_with_retry(), self.poll_loop())

    async def _shutdown_server(self) -> None:
        if self.aiozc:
            for svc in self.services:
                try:
                    await self.aiozc.async_unregister_service(svc)
                except Exception as exc:
                    log.debug("Ignoring error unregistering %s: %s", svc.name, exc)
            await self.aiozc.async_close()
            self.aiozc = None
        self.services = []
        if self.runner:
            await self.runner.cleanup()
            self.runner = None

    async def stop(self) -> None:
        self._stop_event.set()
        await self._shutdown_server()
        if self.state.device is not None:
            await self._safe_disconnect(self.state.device)
            self.state.device = None


def load_config(path: Path) -> Dict[str, Any]:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    seen_ips: set[str] = set()
    seen_macs: set[str] = set()
    for entry in cfg["plugs"]:
        ip, mac = entry["virtual_shelly_ip"], entry["virtual_mac"].upper()
        if not re.fullmatch(r"[0-9A-F]{12}", mac):
            raise SystemExit(f"Plug '{entry['name']}': virtual_mac must be 12 hex characters without colons")
        if ip in seen_ips or mac in seen_macs:
            raise SystemExit(f"Plug '{entry['name']}': virtual_shelly_ip and virtual_mac must be unique")
        seen_ips.add(ip)
        seen_macs.add(mac)
    return cfg


async def main() -> None:
    cfg = load_config(BASE_DIR / "config.json")

    username = cfg["tapo_username"]
    password = cfg["tapo_password"]
    poll_interval = float(cfg.get("poll_interval_seconds", 3.0))
    log_requests = bool(cfg.get("log_requests", False))

    store = StateStore(BASE_DIR / cfg.get("state_file", "state.json"))
    saved = store.load()

    nodes: list[TapoShellyNode] = []
    for entry in cfg["plugs"]:
        state = PlugState(
            name=entry["name"],
            tapo_ip=entry["tapo_ip"],
            virtual_shelly_ip=entry["virtual_shelly_ip"],
            virtual_mac=entry["virtual_mac"],
        )
        state.aenergy_total_wh = float(saved.get(state.virtual_mac.upper(), {}).get("aenergy_total_wh", 0.0))
        nodes.append(TapoShellyNode(state, username, password, poll_interval, log_requests))

    states = [n.state for n in nodes]
    stop_event = asyncio.Event()

    async def save_periodically() -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=STATE_SAVE_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                store.save(states)

    tasks = [asyncio.create_task(n.run()) for n in nodes]
    tasks.append(asyncio.create_task(save_periodically()))

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()
    log.info("Shutting down Sigen-Tapo bridge...")
    for n in nodes:
        await n.stop()
    await asyncio.gather(*tasks, return_exceptions=True)
    store.save(states)


if __name__ == "__main__":
    asyncio.run(main())
