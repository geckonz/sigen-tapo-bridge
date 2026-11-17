#!/usr/bin/env python3
import asyncio
import json
import logging
import signal
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from aiohttp import web
from kasa import Discover, Module
from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("sigen-tapo-bridge")


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
    aenergy_total_wh: float = 0.0  # Cumulative energy in Watt-hours
    online: bool = False
    device: Any = field(default=None, repr=False)

    @property
    def shelly_id(self) -> str:
        return f"shellyplus1pm-{self.virtual_mac.lower()}"


class TapoShellyNode:
    def __init__(self, state: PlugState, username: str, password: str, poll_interval: float):
        self.state = state
        self.username = username
        self.password = password
        self.poll_interval = poll_interval
        self.runner: Optional[web.AppRunner] = None
        self.aiozc: Optional[AsyncZeroconf] = None
        self.services: list[ServiceInfo] = []
        self._stop_event = asyncio.Event()

    async def _connect_tapo(self) -> bool:
        try:
            dev = await Discover.discover_single(
                self.state.tapo_ip,
                username=self.username,
                password=self.password,
            )
            await dev.update()
            self.state.device = dev
            self.state.online = True
            log.info("[%s] Connected to Tapo plug at %s (%s)", self.state.name, self.state.tapo_ip, dev.model)
            return True
        except Exception as exc:
            log.warning("[%s] Failed connecting to %s: %s", self.state.name, self.state.tapo_ip, exc)
            self.state.device = None
            self.state.online = False
            return False

    async def poll_loop(self) -> None:
        while not self._stop_event.is_set():
            if self.state.device is None:
                await self._connect_tapo()

            if self.state.device is not None:
                try:
                    await self.state.device.update()
                    dev = self.state.device
                    self.state.is_on = bool(dev.is_on)

                    energy = dev.modules.get(Module.Energy)
                    if energy is not None:
                        if energy.current_consumption is not None:
                            self.state.apower = round(float(energy.current_consumption), 2)
                        if energy.voltage is not None:
                            self.state.voltage = round(float(energy.voltage), 1)
                        if energy.current is not None:
                            self.state.current = round(float(energy.current), 3)
                        if energy.consumption_total is not None:
                            # python-kasa reports consumption_total in kWh; Shelly expects Wh
                            self.state.aenergy_total_wh = round(float(energy.consumption_total) * 1000.0, 2)
                    self.state.online = True
                except Exception as exc:
                    log.warning("[%s] Poll error (%s); will reconnect", self.state.name, exc)
                    self.state.device = None
                    self.state.online = False
                    self.state.apower = 0.0
                    self.state.current = 0.0

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass

    # --- Shelly Gen2+ JSON-RPC Payload Builders ---

    def _device_info(self) -> Dict[str, Any]:
        return {
            "name": self.state.name,
            "id": self.state.shelly_id,
            "mac": self.state.virtual_mac.upper(),
            "slot": 0,
            "model": "SNSW-001P16EU",
            "gen": 2,
            "fw_id": "20241011-114455/1.4.4-g6d2a586",
            "ver": "1.4.4",
            "app": "Plus1PM",
            "auth_en": False,
            "auth_domain": None,
        }

    def _switch_status(self) -> Dict[str, Any]:
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
                "total": self.state.aenergy_total_wh,
                "by_minute": [0.0, 0.0, 0.0],
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
                "uptime": 86400,
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
            return self._switch_status()
        if method == "Switch.Set":
            target_on = bool(params.get("on", self.state.is_on))
            was_on = self.state.is_on
            if self.state.device is not None:
                try:
                    if target_on:
                        await self.state.device.turn_on()
                    else:
                        await self.state.device.turn_off()
                    self.state.is_on = target_on
                    log.info("[%s] Switched %s via mySigen", self.state.name, "ON" if target_on else "OFF")
                except Exception as exc:
                    log.error("[%s] Failed Switch.Set(%s): %s", self.state.name, target_on, exc)
            return {"was_on": was_on}
        if method == "Sys.SetConfig":
            new_name = params.get("config", {}).get("device", {}).get("name")
            if new_name:
                self.state.name = new_name
            return {"restart_required": False}
        if method == "Sys.GetStatus":
            return self._full_status()["sys"]
        if method == "Wifi.GetStatus":
            return self._full_status()["wifi"]

        return {}

    # --- HTTP Endpoints ---

    async def handle_shelly_info(self, request: web.Request) -> web.Response:
        return web.json_response(self._device_info())

    async def handle_rpc_path(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        params: Dict[str, Any] = dict(request.query)
        if "on" in params:
            params["on"] = str(params["on"]).lower() in ("true", "1", "yes")
        if request.can_read_body:
            try:
                body = await request.json()
                if isinstance(body, dict):
                    params.update(body.get("params", body))
            except Exception:
                pass
        result = await self._dispatch_rpc(method, params)
        return web.json_response(result)

    async def handle_rpc_post(self, request: web.Request) -> web.Response:
        data = await request.json()
        rpc_id = data.get("id", 1)
        method = data.get("method", "")
        params = data.get("params", {}) or {}
        result = await self._dispatch_rpc(method, params)
        return web.json_response({"id": rpc_id, "src": self.state.shelly_id, "result": result})

    # --- Lifecycle (HTTP + mDNS) ---

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/shelly", self.handle_shelly_info)
        app.router.add_route("*", "/rpc/{method}", self.handle_rpc_path)
        app.router.add_post("/rpc", self.handle_rpc_post)

        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, host=self.state.virtual_shelly_ip, port=80)
        await site.start()

        ip_bytes = socket.inet_aton(self.state.virtual_shelly_ip)
        instance_name = self.state.shelly_id
        server_fqdn = f"{instance_name}.local."
        txt_records = {
            "id": instance_name,
            "mac": self.state.virtual_mac.upper(),
            "model": "SNSW-001P16EU",
            "gen": "2",
            "app": "Plus1PM",
            "ver": "1.4.4",
        }

        self.aiozc = AsyncZeroconf(interfaces=[self.state.virtual_shelly_ip], ip_version=IPVersion.V4Only)
        self.services = [
            ServiceInfo(
                "_shelly._tcp.local.",
                f"{instance_name}._shelly._tcp.local.",
                addresses=[ip_bytes],
                port=80,
                properties=txt_records,
                server=server_fqdn,
            ),
            ServiceInfo(
                "_http._tcp.local.",
                f"{instance_name}._http._tcp.local.",
                addresses=[ip_bytes],
                port=80,
                properties=txt_records,
                server=server_fqdn,
            ),
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

    async def stop(self) -> None:
        self._stop_event.set()
        if self.aiozc:
            for svc in self.services:
                await self.aiozc.async_unregister_service(svc)
            await self.aiozc.async_close()
        if self.runner:
            await self.runner.cleanup()


async def main() -> None:
    cfg_path = Path(__file__).parent / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    username = cfg["tapo_username"]
    password = cfg["tapo_password"]
    poll_interval = float(cfg.get("poll_interval_seconds", 3.0))

    nodes: list[TapoShellyNode] = []
    for entry in cfg["plugs"]:
        state = PlugState(
            name=entry["name"],
            tapo_ip=entry["tapo_ip"],
            virtual_shelly_ip=entry["virtual_shelly_ip"],
            virtual_mac=entry["virtual_mac"],
        )
        node = TapoShellyNode(state, username, password, poll_interval)
        await node.start()
        nodes.append(node)

    tasks = [asyncio.create_task(n.poll_loop()) for n in nodes]

    stop_future = asyncio.get_running_loop().create_future()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: not stop_future.done() and stop_future.set_result(True))

    await stop_future
    log.info("Shutting down Sigen-Tapo bridge...")
    for n in nodes:
        await n.stop()
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())