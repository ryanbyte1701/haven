# Matter

[Matter](https://csa-iot.org/all-solutions/matter/) is the smart-home standard backed by Apple, Google, Amazon,
Samsung and most device makers. Matter devices are controlled **locally** on your network, and one device can be
controlled by several apps at once ("multi-admin"). So you can keep using Apple Home, Google Home, Alexa or
SmartThings and add Haven as one more controller.

Haven doesn't speak Matter itself. It connects to a **Matter server**: the open-source Matter controller that Home
Assistant uses ([matterjs-server](https://github.com/matter-js/matterjs-server) from the Open Home Foundation),
running as a second container next to Haven. Haven talks to it over its WebSocket API (`ws://<NAS>:5580/ws`).

> **Already using Home Assistant?** Matter devices that are in Home Assistant can come into Haven through
> **Settings → Home Assistant** instead, with nothing else to install. Use one or the other for the same device,
> not both, or you'll see it twice.

## What you need
- **IPv6 turned on** on the NAS's network port. Matter only uses IPv6 on your LAN (your internet connection can
  stay IPv4-only). On a Synology: **Control Panel → Network → Network Interface** → select your LAN port →
  **Edit → IPv6 → IPv6 setup: Auto** → OK.
- The NAS on the **same network** (subnet/VLAN) as your Matter devices, your phone and your Thread border routers.
- **Wi-Fi and Ethernet Matter devices** need nothing else.
- **Thread Matter devices** (most battery sensors, Eve, Nanoleaf, many locks and blinds) also need a **Thread
  border router** on your network. You probably have one already: Apple TV 4K or HomePod (mini), a Google Nest Hub
  (2nd gen) / Nest Hub Max / Nest Wifi Pro, a recent Amazon Echo (4th gen), a SmartThings hub (v3 / Station), or
  Home Assistant with a SkyConnect / Connect ZBT-1 dongle running OpenThread Border Router. The NAS doesn't need any
  Thread or Bluetooth hardware.

## 1. Start the Matter server
On a Synology (DSM 7.2+, Container Manager):
1. File Station: in the `docker` shared folder create `matter-server` and `matter-server/data`.
2. Container Manager → **Project → Create**. Project name `matter-server`, path `/docker/matter-server`,
   *Create docker-compose.yml*, and paste
   [deploy/synology/docker-compose.matter.yml](../../deploy/synology/docker-compose.matter.yml).
3. Change `user:` if your user id isn't `1026:100` (use the same value as in Haven's compose file) and the time zone.
   **Next → Done**.

The container uses **host networking** (`network_mode: host`): Matter discovers devices with IPv6 multicast and
reaches Thread devices through routes your border routers announce, which doesn't work behind Docker's NAT.
Its data folder holds your Matter *fabric* (the keys that let it control your devices): include it in your backups.

Not on a Synology? Uncomment the `matter-server` service at the end of Haven's
[docker-compose.yml](../../docker-compose.yml), or run it with
`docker run -d --name matter-server --restart unless-stopped --network host -v $PWD/matter:/data ghcr.io/matter-js/matterjs-server:stable`
(make the `matter` folder writable for uid 1000).

## 2. Connect Haven
Haven → **Settings → Matter** → turn it on, leave **Matter server address** empty → **Save**. The status
should change to *Connected*.

With the address empty, Haven looks for the Matter server on its own machine: `127.0.0.1` (when Haven itself
uses host networking), the NAS's address on Haven's Docker network (the normal setup), then
`host.docker.internal`. If the Matter server runs somewhere else, enter its address, e.g.
`ws://192.168.1.20:5580/ws` (just `192.168.1.20` works too).

## 3. Add a device
Matter devices join with a **pairing code**: 11 digits like `3497-011-2332`, 21 digits, or the text of a QR code
starting with `MT:`.

The normal way, because a NAS has no Bluetooth: **set the device up in the app you already use, then share it with
Haven.** The other app creates a fresh pairing code and opens a pairing window on the device:

| App | Where to get a code for another controller |
|---|---|
| **Apple Home** | Long-press the accessory → ⚙ Accessory Settings → **Turn On Pairing Mode** → copy the setup code |
| **Google Home** | Tap the device → ⚙ Settings → **Linked Matter apps & services** → **Link apps & services** → copy the code |
| **Amazon Alexa** | Devices → the device → ⚙ → **Other Assistants and Apps** → **Add Another** → copy the code |
| **SmartThings** | The device → ⋮ → **Share with other services** → copy the code |
| **Home Assistant** | Settings → Devices → the Matter device → **Share device** → copy the code |
| **Brand apps** (Eve, Nanoleaf, Aqara, Meross…) | Look for *Matter*, *Share*, *Connected services* or *Pairing mode* in the device's settings |

Then in Haven → Settings → Matter:
1. Paste the code into **Pairing code for a new device** (dashes and spaces are fine) → **Save**.
2. Press **Add device**. Adding takes 30 seconds to a few minutes; the device then shows up on the dashboard and
   the code field is emptied. Rename it or give it a room on the **Devices** page.

Shared codes are only valid for a few minutes (the other app says how long): paste it and press Add device right away.

**Brand-new devices:** Wi-Fi and Thread devices fresh out of the box are set up over **Bluetooth**, which a NAS
usually doesn't have: add them to Apple/Google/Alexa/SmartThings/Home Assistant first, then share them as above.
Devices connected by **Ethernet** (and Matter bridges such as the Hue Bridge or Aqara and SwitchBot hubs) can be
added straight from the code printed on them or in their manual, once they're in pairing mode. If your Matter server
does have Bluetooth set up, turn on *Add brand-new devices over Bluetooth* under Advanced.

To **remove** a device from the Matter server, use its web page at `http://NAS-IP:5580` (select the node →
Remove). The card disappears from Haven at the same time. Removing it from the other app doesn't remove it from Haven.

## What you get
Every Matter device is one card. Power strips and double relays get **one card per outlet** (like Tapo strips), and
every device behind a Matter **bridge** (Hue, Aqara, SwitchBot, IKEA DIRIGERA…) gets its own card. Updates are
instant: the Matter server subscribes to every device and Haven listens to its events.

| Matter (cluster) | Haven shows | Control |
|---|---|---|
| On/Off on a light | Light on/off | on/off |
| On/Off on a plug, outlet, relay | Switch (plugs are listed under **Power → Smart plugs**) | on/off |
| Level Control (lights) | Brightness 1–100 % | slider |
| Color Control | Color temperature (K, from mireds) and color (from xy or hue/saturation) | color temperature, color |
| Door Lock | Lock: locked / unlocked / jammed; door open/closed if the lock has a door sensor | lock / unlock |
| Window Covering | Open/closed, position (% open) | open / close / stop, position slider |
| Thermostat | Room temperature, mode (e.g. *Heat · heating*) | – |
| Fan Control (fans, air purifiers) | Power, fan speed % | on/off, speed slider |
| Boolean State on a contact sensor | Contact: open / closed | – |
| Boolean State on a water leak / rain / freeze detector | Leak (or rain, freeze) | – |
| Occupancy Sensing | Motion (PIR) or presence (radar / mmWave) | – |
| Temperature, Humidity, Pressure, Illuminance measurement | Temperature °C, humidity %, pressure hPa, light lux | – |
| Smoke CO Alarm | Smoke, CO, battery low | self-test |
| CO₂, PM2.5, PM10, PM1, TVOC, CO, NO₂, ozone, formaldehyde, radon concentration; Air Quality | Concentration in the device's unit (or Low/Medium/High), air quality | – |
| Electrical Power Measurement | Power W, voltage V, current A | – |
| Electrical Energy Measurement | Total energy kWh (lifetime counter) | – |
| Power Source | Battery %, battery low | – |

A device that drops off the network shows as offline (the Matter server reports it unavailable, or a bridge reports
the bridged device unreachable).

## Security
The Matter server has **no password**: anything that can reach port 5580 can control your Matter devices and open
pairing windows. Never forward port 5580 on your router. If you don't trust every device on your LAN, either use
DSM's firewall to allow port 5580 only from the NAS itself and Docker's networks (`172.16.0.0/12`), or set
`LISTEN_ADDRESS` in the compose file to the NAS's Docker bridge address (e.g. `172.17.0.1`) and enter
`ws://172.17.0.1:5580/ws` as the address in Haven. `DISABLE_DASHBOARD: "true"` hides the server's own web page.
Haven only sends the commands you trigger (and automations), and the pairing code is cleared once it has been used.

## Troubleshooting
- **"No Matter server found on this NAS"**: the matter-server container isn't running or can't start (Container
  Manager → Container → matter-server → Log). A *permission denied* on `/data` means the `user:` line doesn't match the
  owner of `matter-server/data`. If DSM's firewall is on, see the next point.
- **DSM firewall on?** With a final *Deny* rule, add *Allow* rules above it for **TCP 5580** from `172.16.0.0` /
  `255.240.0.0` (Haven → Matter server) and **UDP 5353, 5540** from *All* (Matter uses IPv6, so an IPv4 "LAN
  subnet" rule doesn't match it). A few devices use another UDP port; if one stays offline, test once with the
  firewall off.
- **"isn't a Matter server's WebSocket"**: the address points at something else; the path must be `/ws`.
- **"No device in pairing mode was found"**: the shared code expired, or the NAS can't see the device. Create a new
  code and press Add device right away. Check that IPv6 is on (step *What you need*), and that your router or mesh
  system doesn't filter multicast ("multicast optimisation", "IGMP/MLD snooping", mDNS reflectors on UniFi/Omada/
  Eero): turn those off.
- **"The device rejected the code"**: copy the code again (it changes every time you share).
- **Wi-Fi devices work but Thread devices stay offline**: the NAS has to accept the routes to the Thread network
  that your border routers announce (IPv6 "route information options"). Make sure IPv6 is set to *Auto*, not
  *Manual*, and restart the matter-server after enabling it. On some Linux kernels this also needs
  `net.ipv6.conf.<lan interface>.accept_ra_rt_info_max_plen=64` (over SSH: `sudo sysctl -w net.ipv6.conf.eth0.accept_ra_rt_info_max_plen=64`;
  make it permanent with a *Triggered Task → Boot-up* in Control Panel → Task Scheduler). Only one Thread network
  should exist: if you have border routers from several brands, let them join the same Thread network where the
  apps allow it.
- **A lock won't lock/unlock**: some locks require a PIN for remote commands; enter it under Settings → Matter →
  Advanced → *Door lock PIN*.
- **More detail**: the Matter server's web page (`http://NAS-IP:5580`) shows every node, its endpoints and
  attributes, and its log level can be raised to *debug* there.

## Limitations
- Thermostats are shown, not controlled (no set-point or mode changes yet); same for fan modes other than speed.
- Energy is the device's lifetime counter (*Total*), not *today* / *this month* like Tapo plugs.
- Buttons and remotes (Generic Switch events), cameras, robot vacuums, appliances (washers, fridges, dishwashers) and
  EV chargers aren't shown yet.
- Haven doesn't remove devices from the Matter server or share them onward; use the server's web page for that.
- Written against the matterjs-server WebSocket API (schema 15, server 1.6.0) and python-matter-server 8.1.2's
  protocol; Haven only uses commands both have (`start_listening`, `get_nodes`, `commission_with_code`,
  `device_command`, `write_attribute`).
