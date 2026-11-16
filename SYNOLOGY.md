# Running Haven on a Synology NAS

This works on DSM 7.2 or later with **Container Manager**, on Intel/AMD and ARM64 models. Older 32-bit
ARM models can't run Container Manager at all.

Haven is published as a ready-made image at **`ghcr.io/ryanbyte1701/haven`**. GitHub rebuilds it on every
change, so your NAS just pulls it; there's nothing to build or copy except one small compose file.

## 1. Install Container Manager
Package Center → search **Container Manager** → Install.

## 2. Make the folders
In **File Station**, open the `docker` shared folder (Container Manager creates it) and create:
- `docker/haven`
- `docker/haven/config`: optional `config.yaml` goes here (Haven starts without one). Create it anyway:
  the compose file mounts it, and if Docker creates it, it belongs to root.
- `docker/haven/data`: Haven saves your settings, layout and history here. Create it yourself so it belongs
  to your user; if Docker creates it, it belongs to root and Haven can't write to it.

## 3. Create the project
Container Manager → **Project** → **Create**
- Project name: `haven`
- Path: `/docker/haven`
- Source: **Create docker-compose.yml**, then paste the contents of
  [deploy/synology/docker-compose.yml](https://github.com/ryanbyte1701/haven/blob/main/deploy/synology/docker-compose.yml)
  (a Synology-ready version with the steps at the top) or the general
  [docker-compose.yml](https://github.com/ryanbyte1701/haven/blob/main/docker-compose.yml).
- Before you continue, check these lines in the pasted file:
  - `user: "1026:100"`: the user that owns `data` (see step 4).
  - `DASH_TOKEN: ""`: can stay empty. On the first visit Haven asks you to **create the admin account**; it
    needs the one-time *setup code* from the container log (Container Manager → Container → haven → Log).
  - `TZ: America/Phoenix`: your time zone.
- Next → leave *Web portal via Web Station* unchecked → **Done**.

Container Manager pulls the images and starts Haven and its MQTT broker. Open **http://NAS-IP:1701**, create the
admin account (or, upgrading from an older Haven with `DASH_TOKEN` set, **sign in as `admin` with your old
dashboard password**), go to **Settings**, and turn on your accounts. Add the rest of the household under
Settings → **Users & access**: *Admin* (everything), *Regular* (controls devices, no settings) or *Monitor*
(view only). Every card says where to find the keys, and changes apply right away.

## 4. The user ID (the step that usually goes wrong)
Haven writes to `data/`, so it has to run as the user that owns that folder. `docker-compose.yml` uses
`user: "1026:100"`, which is the first admin account created on most Synology NASes, in the `users` group.
To check yours: Control Panel → Terminal & SNMP → enable SSH, then `ssh you@nas-ip` and run `id`. Use the
`uid` and `gid` numbers shown.

If the IDs are wrong, the container log says **"Cannot write to /data"**. Either fix the numbers, or in
File Station right-click `data` → Properties → Permission → give that user Read & Write.

## 5. Updating Haven
Every change pushed to GitHub publishes a new `latest` image in a few minutes. To update:
- **Container Manager:** Project → haven → Action → **Stop**, then Action → **Build**. Build pulls the newest
  image and starts it again. Your settings, layout and history in `data/` are kept.
- **Or over SSH:** `cd /volume1/docker/haven && sudo docker compose pull && sudo docker compose up -d`

To stay on a specific version instead of `latest`, change the image line to a tag such as
`ghcr.io/ryanbyte1701/haven:1.0.0` (tags are listed on the repo's *Packages* page).

## 6. Firewall
Haven's dashboard has no business being reachable from the internet. Turn on the DSM
firewall (Control Panel → Security → Firewall → Enable firewall → Edit Rules) and, for the profile in use:
1. **Create** a rule: Ports → Select from a list → or *Custom* → TCP **1701** (dashboard, Ecowitt) and **1702** (HTTPS) → Source IP: **Specific IP → Subnet** = your LAN,
   e.g. `192.168.1.0` / `255.255.255.0` → Action **Allow**.
2. Only if you uncommented MQTT's `ports:` in the compose file, add **1883** to the same rule.
3. Make sure the last rule is *All / All → Deny* (or "If no rules are matched: Deny access").

## 7. Day-to-day
| Task | Where |
|---|---|
| Add or change an account | Haven → **Settings** (applies immediately) |
| Rearrange the dashboard | Haven → ✎ button → drag cards and rooms → Done |
| Change the password, user or ports | Project → haven → **YAML** tab, edit, then Action → **Build** |
| See errors | Container → haven → **Log** |

## 8. Port already in use?
Change the left number in `docker-compose.yml`, e.g. `"1801:1701"`. Use that same number for the Ecowitt
gateway's port and in your browser.

## 9. HTTPS / remote access
**Built-in HTTPS (on by default):** open **https://NAS-IP:1702**. Haven creates a self-signed certificate in
`data/tls/` on first start, so the browser asks you to accept it once. To get rid of the warning:
- **Use your NAS certificate:** DSM → Control Panel → Security → Certificate → select it → **Export**. Unzip
  into `haven/config/tls/` and in the Project's YAML set
  `HAVEN_TLS_CERT: /config/tls/fullchain.pem` and `HAVEN_TLS_KEY: /config/tls/privkey.pem`
  (DSM names them `fullchain.pem`/`privkey.pem`; if you only have `cert.pem`, use that). Re-export after
  DSM renews it, then restart Haven.
- **Or** add your hostname/IP to the self-signed certificate with `HAVEN_TLS_HOSTS: "haven.home,192.168.1.20"`,
  delete `data/tls`, restart, and accept it once per device.

Set `HAVEN_HTTPS_ONLY: "1"` to send browsers from port 1701 to HTTPS. Ecowitt gateways, `/metrics` and the
health check keep working over HTTP because gateways can't use HTTPS. `HAVEN_HTTPS_PORT: "0"` turns HTTPS off.

**Reverse proxy (for a hostname on port 443):**
Control Panel → Login Portal → Advanced → **Reverse Proxy** → Create:
- Source: HTTPS, your hostname, port 443 → Destination: HTTP, localhost, 1701
- **Custom Header** tab → Create → **WebSocket** (this is required for live updates)
- In the Project's YAML, uncomment `FORWARDED_ALLOW_IPS` and set it to the address the proxy connects from
  (the Docker bridge gateway, usually `172.17.0.1`; the container log shows client addresses). Without it Haven
  ignores the proxy's `X-Forwarded-*` headers, which is safe but logs the proxy's address instead of yours.

Use strong passwords for every account before exposing the dashboard outside your home ("Continue as viewer" only
works from home-network addresses). Add the proxy's hostname to `HAVEN_ALLOWED_HOSTS` (e.g. `haven.example.com`),
otherwise Haven answers "421" to that name; `X-Forwarded-Host` is not used, so keep the proxy's default of passing
the original `Host` header. If you can, use Tailscale or
QuickConnect/VPN instead of opening ports.

## 10. Optional: config files, Prometheus and running from source
These need the repo's files on the NAS. Easiest: install **Git Server** from Package Center (it provides the
`git` command), then over SSH:

```bash
cd /volume1/docker && sudo git clone https://github.com/ryanbyte1701/haven haven-src
```

Update later with `cd /volume1/docker/haven-src && sudo git pull`.

- **config.yaml**: copy `config/config.example.yaml` to `/docker/haven/config/config.yaml`. Every option is
  explained inside the file. Values saved from the Settings page take priority.
- **Prometheus**: Haven keeps a year of history on its own in `data/history.db`. To use Prometheus (for example
  with Grafana), copy the `prometheus` folder next to your compose file, delete the two lines `profiles:` and
  `- metrics` under the `prometheus` service (Container Manager doesn't support compose profiles), then
  Action → **Build**. In Haven, go to Settings → History & metrics → Store history in **prometheus**.
- **Build it yourself**: create the project with path `/docker/haven-src` and *Use existing docker-compose.yml*.
  In that file, comment out the `image:` line and uncomment `build: .`.

## 11. High availability (beta)
Run Haven on **two NASes**: one is *active*, the other waits on *standby* and takes over within about
10 seconds if the active one stops answering. Both share a **virtual IP** (VIP) that always points at the
active Haven, so you bookmark one address and Ecowitt gateways keep reporting to it.

**You need:** two NASes (DSM 7.2+, Container Manager) on the **same subnet / LAN segment**, and one unused
IPv4 address on that subnet that is **outside your router's DHCP range** (e.g. `192.168.1.50`). Give both
NASes fixed IPs.

**Install on both NASes**
1. Follow steps 1–2 and 4 above (folders and user ID) on each NAS.
2. Container Manager → Project → Create, and paste
   [deploy/synology/docker-compose.ha.yml](https://github.com/ryanbyte1701/haven/blob/main/deploy/synology/docker-compose.ha.yml)
   instead of the normal file. Change `HAVEN_UID`/`HAVEN_GID` (your user ID from step 4), `TZ` and `DASH_TOKEN`.
   This variant uses **host networking** (Haven listens directly on the NAS's ports 1701/1702, no `ports:`
   section) and adds the **NET_ADMIN** and **NET_RAW** capabilities so Haven can add the VIP to the network
   card and announce it (gratuitous ARP). It starts as root only for a moment: `app.privdrop` switches to your
   user keeping just those two capabilities; `read_only`, `cap_drop: ALL` and `no-new-privileges` stay on.
3. With host networking Haven can't use Docker names: in Haven → Settings → **MQTT** set the broker host to
   **`127.0.0.1`** (the compose file publishes Mosquitto on `127.0.0.1:1883` only).
4. Turn on NTP time sync on both NASes (Control Panel → Regional Options → Time). Heartbeats are rejected if
   the clocks differ by more than 5 minutes.

**Configure: the primary first**
1. On the first NAS open `http://NAS1-IP:1701` → Settings → **System → High availability (beta)**:
   *This Haven is the* `primary`, *Other Haven's address* = NAS2's own IP (e.g. `192.168.1.21`), a **shared
   secret** of 16+ characters, the **virtual IP**, prefix length (24 for 255.255.255.0). Turn it on, **Save**.
   After about 10 seconds it shows **Active · holds 192.168.1.50**.
2. On the second NAS open `http://NAS2-IP:1701` (create the admin there too, or use the same `DASH_TOKEN`),
   same page: `standby`, the other address = NAS1's IP, **the same secret and VIP**. Save. It shows **Standby**,
   copies the primary's settings (accounts, layout, automations, device names) and stops its own integrations
   and automations, so devices are only polled and controlled once.
3. From now on use **`http://VIP:1701`** for the dashboard and change settings there. The standby's own page
   still opens on its own IP and shows a banner pointing at the active Haven. Add the VIP to
   `HAVEN_TLS_HOSTS` if you use the self-signed HTTPS certificate.

**Test it:** Settings → High availability → **Fail over now** hands the VIP to the standby (and back). For a
real test, stop the primary's project (Container Manager → Project → Action → Stop) or unplug it: the
standby becomes active within about 10 s (heartbeat 2 s × 5 missed; both under *Advanced*). When the primary
returns it takes the VIP back (*Primary takes over again*, on by default). A clean stop hands over at once.

**Good to know**
- Split brain: if both become active (a network partition heals), the lower priority one steps down
  immediately. A node that can reach neither the other Haven nor its router (or the *Isolation check
  address*) gives up the VIP instead of grabbing it.
- If the VIP can't be added (no host networking or NET_ADMIN, e.g. the normal compose file), the page shows
  **Fault: Needs host networking and NET_ADMIN** and Haven keeps working as a single instance on its own IP.
- Heartbeats are signed with the shared secret (it is never sent), and the copied settings, which include
  account passwords, are encrypted with it. You can still use HTTPS between the two: enter the other address
  as `https://192.168.1.21:1702`; keep *Verify the other Haven's HTTPS certificate* off for the self-signed one.
- **Each NAS keeps its own history** (`data/history.db`); charts show what the node you're looking at recorded
  while it was active. Settings are copied from the active node only (the HA section, node identity and
  certificates are never copied). Changes made on a standby are overwritten by the active node's.
- MQTT/Zigbee2MQTT and cloud accounts are shared; only the active Haven talks to them. Each NAS runs its own
  Mosquitto: point MQTT devices (Zigbee2MQTT, Shelly, ESPHome) at the **VIP** so they follow the active Haven
  (then publish the broker as `"1883:1883"` with a password, see `mosquitto/mosquitto.conf`), or run one broker
  on a third device and set its address in Settings → MQTT.
- Beta limits: LAN only, IPv4 only, both NASes on the same layer-2 subnet, two nodes. Ecowitt uploads that
  reach a standby directly (its own IP) are ignored. `HAVEN_HA_DRY_RUN=1` is for testing only: it pretends to
  add the VIP.
