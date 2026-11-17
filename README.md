# TP-Link Tapo P110M to Sigenergy (mySigen) Bridge

A lightweight `asyncio` Python bridge designed to run on a Raspberry Pi (including older models like the **Raspberry Pi Model B**). It polls TP-Link Tapo P110M smart plugs locally via `python-kasa` and emulates native **Shelly Plus 1PM** (Gen2+) devices over mDNS and HTTP JSON-RPC so the Sigenergy Gateway discovers and monitors each appliance inside the **mySigen** app.

---

## Prerequisites

* **Hardware:** Raspberry Pi connected via Ethernet to the same local network/VLAN as your Sigenergy Gateway and Tapo plugs.
* **OS:** Raspberry Pi OS Lite (Debian Bookworm or newer).
* **Tapo App Setting:** Open the Tapo app and enable **Third-Party Compatibility** (`Me` $\rightarrow$ `Third-Party Services` $\rightarrow$ `Third-Party Compatibility`).
* **Static IPs / DHCP Reservations:** Reserve static IP addresses on your router for each Tapo P110M plug so they do not change after reboots.

---

## 1. Configure Virtual IP Aliases

The Sigenergy Gateway expects one unique IP address on port `80` per Shelly device. Assign a secondary virtual IP alias to your Pi's Ethernet interface (`eth0`) for each Tapo plug you want to bridge.

> **Note:** The interface is usually `eth0`, but some systems use a predictable name instead (e.g. `enxb827eb36cba3` on a Pi Model B). Check yours with `ip -br addr` and substitute it wherever `eth0` appears below.

Assuming your Pi's primary IP is `192.168.1.200/24`:

```bash
# Check your active Ethernet connection name (usually "Wired connection 1")
nmcli connection show

# Add one virtual IP per Tapo plug (e.g., .201 and .202)
sudo nmcli connection modify "Wired connection 1" +ipv4.addresses "192.168.1.201/24,192.168.1.202/24"
sudo nmcli connection up "Wired connection 1"

# Verify the virtual IPs are active on eth0
ip -4 addr show eth0
```

---

## 2. Install Dependencies

Set up the project directory and Python virtual environment:

```bash
sudo mkdir -p /opt/sigen-tapo-bridge
sudo chown $USER:$USER /opt/sigen-tapo-bridge
cd /opt/sigen-tapo-bridge

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install python-kasa zeroconf aiohttp
```

Place your `bridge.py` script inside `/opt/sigen-tapo-bridge/` and make it executable:

```bash
chmod +x /opt/sigen-tapo-bridge/bridge.py
```

---

## 3. Create `config.json`

Copy `config.example.json` to `/opt/sigen-tapo-bridge/config.json` and fill in your TP-Link account credentials (used by `python-kasa` for local session authentication) and your plug mappings. `config.json` is gitignored so your credentials are never committed:

```bash
cp config.example.json /opt/sigen-tapo-bridge/config.json
```

Example contents:

```json
{
  "tapo_username": "your-tplink-email@example.com",
  "tapo_password": "your-tplink-password",
  "poll_interval_seconds": 3,
  "plugs": [
    {
      "name": "Kitchen Fridge",
      "tapo_ip": "192.168.1.51",
      "virtual_shelly_ip": "192.168.1.201",
      "virtual_mac": "A8032A110001"
    },
    {
      "name": "Office Desk",
      "tapo_ip": "192.168.1.52",
      "virtual_shelly_ip": "192.168.1.202",
      "virtual_mac": "A8032A110002"
    }
  ]
}
```

> **Note:** Each entry must have a unique `virtual_shelly_ip` (matching an alias bound to `eth0`) and a unique 12-character uppercase hex `virtual_mac` (no colons).

---

## 4. Set Up the Systemd Service

Create `/etc/systemd/system/sigen-tapo-bridge.service` so the bridge starts automatically on boot and binds to privileged port `80`:

```ini
[Unit]
Description=TP-Link Tapo P110M to Sigenergy Shelly Gen2 Bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/sigen-tapo-bridge
ExecStart=/opt/sigen-tapo-bridge/venv/bin/python /opt/sigen-tapo-bridge/bridge.py
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
```

Enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sigen-tapo-bridge.service
```

---

## 5. Verify & Pair in mySigen

1. **Verify Service Logs:**
   ```bash
   sudo journalctl -u sigen-tapo-bridge.service -f
   ```
   Confirm each plug connects without authentication or timeout errors.

2. **Test the Local Shelly RPC Endpoint:**
   ```bash
   curl http://192.168.1.201/rpc/Shelly.GetStatus
   ```
   Verify that `"apower"` reflects the live wattage of your Tapo plug.

3. **Pair in the mySigen App:**
   * Open **mySigen** $\rightarrow$ **Devices** / **Smart Load** $\rightarrow$ **Add Device**.
   * Scan your local network; each virtual IP will appear as a **Shelly Plus 1PM**.
   * Select **Metering Only** for passive tracking or **Metering & Control** to allow Sigenergy to switch the plug during load-shedding events.

---

## Adding More Plugs Later

1. Assign a static DHCP lease for the new Tapo plug on your router.
2. Add another virtual IP alias to `eth0` via `nmcli`.
3. Add a new object to the `"plugs"` array in `/opt/sigen-tapo-bridge/config.json` with a unique `virtual_shelly_ip` and `virtual_mac`.
4. Restart the service:
   ```bash
   sudo systemctl restart sigen-tapo-bridge.service
   ```