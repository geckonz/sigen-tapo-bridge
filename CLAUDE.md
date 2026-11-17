# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Single-file asyncio bridge (`bridge.py`) that polls TP-Link Tapo P110M smart plugs locally via `python-kasa` and makes each one appear on the LAN as a **Shelly Plus 1PM (Gen2)** device, so a Sigenergy Gateway discovers it and the mySigen app can meter and switch it. It runs on a Raspberry Pi (as old as a Pi 2 Model B), so keep it lightweight. Full deployment steps are in `sigen_tapo_bridge_setup_guide.md`.

## Commands

There is no build system, test suite, or linter configured. Dependencies: `pip install python-kasa zeroconf aiohttp` (into a venv).

- Run: `python bridge.py` (reads `config.json` from the script's own directory). It must run as root, or with `CAP_NET_BIND_SERVICE`, because it binds port 80, and every `virtual_shelly_ip` in the config must already be assigned to a local interface or the bind fails.
- Exercise the emulated device: `curl http://<virtual_shelly_ip>/rpc/Shelly.GetStatus`, `curl http://<ip>/shelly`, `curl "http://<ip>/rpc/Switch.Set?on=true"`, or POST JSON-RPC `{"id":1,"method":"Switch.GetStatus"}` to `/rpc`.
- Deployed service: `sigen-tapo-bridge.service` expects the code at `/opt/sigen-tapo-bridge` with a venv at `/opt/sigen-tapo-bridge/venv`. Logs: `journalctl -u sigen-tapo-bridge.service -f`.

## Architecture

- **One `TapoShellyNode` per plug entry** in `config.json`. Each node owns:
  - an aiohttp server bound to that plug's own `virtual_shelly_ip:80`. The Sigenergy Gateway expects one IP per Shelly device, which is why the host needs one IP alias per plug.
  - an `AsyncZeroconf` instance restricted to that IP, advertising `_shelly._tcp` and `_http._tcp` under the instance name `shellyplus1pm-<mac>`.
  - a `poll_loop` task that refreshes `PlugState` from the Tapo device every `poll_interval_seconds`.
- **`PlugState` is the cache between the two sides.** Shelly RPC reads are answered from this cache and never touch the Tapo device. The only call that reaches the Tapo device directly is `Switch.Set`. If a poll fails, the node drops the device handle, marks itself offline, zeroes power and current, and reconnects on the next loop.
- **`_dispatch_rpc` handles every Shelly method.** Both transports (`/rpc/{method}` with query string or body params, and JSON-RPC POST to `/rpc`) feed into it. Methods it does not recognise return `{}`. To support a new Shelly method, add it here.
- **Spoofed static identity:** the model (`SNSW-001P16EU`), firmware version, temperature, uptime, RAM and wifi fields are hardcoded so the device looks like a real Plus 1PM. The model, app and version strings appear in both `_device_info` and the mDNS TXT records, so update them together.
- **Unit conversion:** python-kasa's `consumption_total` is in kWh, while Shelly's `aenergy.total` is in Wh (multiplied by 1000). Voltage defaults to 230 V and frequency is 50 Hz (NZ mains).

## Config constraints

`config.json` holds the TP-Link account credentials, which python-kasa uses for local auth (the "Third-Party Compatibility" setting must be enabled in the Tapo app). Each plug entry needs a unique `virtual_shelly_ip` and a unique 12-character uppercase hex `virtual_mac` with no colons. `config.json` is gitignored; the repo only tracks `config.example.json` (placeholder values), which is copied to `config.json` for real use.
