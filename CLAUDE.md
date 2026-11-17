# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Single-file asyncio bridge (`bridge.py`) that polls TP-Link Tapo P110M smart plugs locally via `python-kasa` and makes each one appear on the LAN as a **Shelly Plus 1PM (Gen2)** device, so a Sigenergy Gateway discovers it and the mySigen app can meter and switch it. It is deployed on a Raspberry Pi 1 Model B (armv6l, ~430 MB RAM), where python-kasa's KLAP encryption is CPU-expensive, so keep it lightweight. Full deployment steps are in `README.md`.

## Commands

There is no build system, test suite, or linter configured. Dependencies: `pip install python-kasa zeroconf aiohttp` (into a venv). The deployed versions are python-kasa 0.12.0, aiohttp 3.14.5, zeroconf 0.154.0 on Python 3.13.

- Run: `python bridge.py` (reads `config.json` from the script's own directory). It must run as root, or with `CAP_NET_BIND_SERVICE`, because it binds port 80. Every `virtual_shelly_ip` must be assigned to a local interface; a node whose IP isn't available yet logs an error and retries every 30 s without affecting the other nodes.
- Exercise the emulated device: `curl http://<virtual_shelly_ip>/rpc/Shelly.GetStatus`, `curl http://<ip>/shelly`, `curl "http://<ip>/rpc/Switch.Set?id=0&on=true"`, or POST JSON-RPC `{"id":1,"src":"me","method":"Switch.GetStatus","params":{"id":0}}` to `/rpc`.
- Set `"wifi_ssid"` to the real Wi-Fi network name; it is reported in `Wifi.GetStatus`, which mySigen calls when adding a device.
- Set `"log_requests": true` in `config.json` to log every incoming HTTP request (source IP, path, method, params). Use it to see what curl or the Sigenergy Gateway actually calls.
- Deployed service: `sigen-tapo-bridge.service` expects the code at `/opt/sigen-tapo-bridge` with a venv at `/opt/sigen-tapo-bridge/venv`. Logs: `journalctl -u sigen-tapo-bridge.service -f`.

## Architecture

- **One `TapoShellyNode` per plug entry** in `config.json`. `node.run()` runs independently per plug and owns:
  - an aiohttp server bound to that plug's own `virtual_shelly_ip:80`. The Sigenergy Gateway expects one IP per Shelly device, which is why the host needs one IP alias per plug.
  - an `AsyncZeroconf` instance restricted to that IP, advertising `_shelly._tcp` and `_http._tcp` under the instance name `shellyplus1pm-<mac>`.
  - a `poll_loop` that refreshes `PlugState` from the Tapo device every `poll_interval_seconds`.
- **`PlugState` is the cache between the two sides.** Shelly RPC reads are answered from this cache and never touch the Tapo device. Only `Switch.Set` and `Switch.Toggle` reach the plug directly.
- **Connection handling:** the first connect uses `Discover.discover_single`. Its `DeviceConfig` is then cached, and reconnects use `Device.connect(config=...)`, which skips UDP discovery. A failed cached connect clears the cache so the next attempt rediscovers. On a poll failure the node disconnects the old device (otherwise aiohttp sessions leak), zeroes power and current, and retries with exponential backoff capped at 60 s.
- **`_dispatch_rpc` handles every Shelly method.** Both transports (`/rpc/{method}` with query string or body params, and JSON-RPC POST to `/rpc`) feed into it. Failures raise `RpcError`. On `/rpc/{method}` this becomes `{"code","message"}` with an HTTP error status; on POST `/rpc` it becomes the frame's `error` member. Unknown methods return code 404 "No handler for ...", an unreachable plug returns -114, and a bad switch id returns -105. To support a new Shelly method, add it here.
- **Energy is integrated by the bridge.** python-kasa's `consumption_total` is always `None` for Tapo plugs, so `EnergyMeter` integrates `apower` over time (trapezoidal, skipping gaps longer than `4 × poll + 10 s`) into `aenergy.total` (Wh) and the Shelly `by_minute` buckets (mWh, newest first). `StateStore` saves totals to `state.json` (gitignored) every 5 minutes and on shutdown, keyed by `virtual_mac`, so the counter never goes backwards.
- **Spoofed static identity:** the `SHELLY_*` constants are used by both `_device_info` and the mDNS TXT records. Temperature, RAM and wifi fields are hardcoded; `uptime` is the real process uptime. Voltage defaults to 230 V and frequency is 50 Hz (NZ mains).

## Config constraints

`config.json` holds the TP-Link account credentials, which python-kasa uses for local auth. `load_config` rejects duplicate `virtual_shelly_ip` or `virtual_mac` values and MACs that aren't 12 hex characters. `config.json` is gitignored; the repo only tracks `config.example.json` (placeholder values), which is copied to `config.json` for real use.

**Tapo onboarding:** "Third-Party Compatibility" must be enabled in the Tapo app *before* a plug is added. A plug added before that advertises `TPAP` encryption (python-kasa: "Unsupported device"), or still fails KLAP auth after the setting is toggled. The fix is to remove and re-add the plug in the app, not to change code.
