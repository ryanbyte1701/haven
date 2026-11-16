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
  [docker-compose.yml](https://github.com/ryanbyte1701/haven/blob/main/docker-compose.yml) from the repo.
- Before you continue, check these lines in the pasted file:
  - `user: "1026:100"`: the user that owns `data` (see step 4).
  - `DASH_TOKEN: ""`: put a password between the quotes. This protects the dashboard, its controls and
    the Settings page where your accounts live.
  - `TZ: America/Phoenix`: your time zone.
- Next → leave *Web portal via Web Station* unchecked → **Done**.

Container Manager pulls the images and starts Haven and its MQTT broker. Open **http://NAS-IP:1701**, go to
**Settings**, and turn on your accounts. Every card says where to find the keys, and changes apply right away.

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
Haven's dashboard and its NUT server have no business being reachable from the internet. Turn on the DSM
firewall (Control Panel → Security → Firewall → Enable firewall → Edit Rules) and, for the profile in use:
1. **Create** a rule: Ports → Select from a list → or *Custom* → TCP **1701** (dashboard, Ecowitt), **1702** (HTTPS) and
   **3493** (Haven's NUT server) → Source IP: **Specific IP → Subnet** = your LAN, e.g. `192.168.1.0` /
   `255.255.255.0` → Action **Allow**.
2. Only if you uncommented MQTT's `ports:` in the compose file, add **1883** to the same rule.
3. Make sure the last rule is *All / All → Deny* (or "If no rules are matched: Deny access").

Even with the firewall off, Haven's NUT server only answers private (LAN) addresses unless you list other
networks under Settings → UPS (NUT) → Allowed client networks.

### Using Haven as your Synology's UPS server
Once your UniFi UPS shows on Haven's **Power** page, go to Control Panel → Hardware & Power → **UPS** → tick
*Enable UPS support* → Network UPS type **Synology UPS server** → enter the IP of the machine running Haven → Apply.
If the Synology runs Haven itself, use the NAS's own IP, and leave *Enable network UPS server* (on the same
page) **off**, because it would take port 3493.

Synology only reads status and shuts itself down when the battery runs low, so nothing else is needed. If you
also want one client to be able to force *every* machine on the UPS to shut down (NUT "FSD" / primary mode),
turn on Settings → UPS (NUT) → Advanced → **Let clients force a shutdown**; it's off by default and requires
the NUT username and a strong NUT password (12+ characters, not the default `secret`); Synology's own
read-only login with `monuser` / `secret` keeps working.

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

Set `DASH_TOKEN` before exposing the dashboard outside your home, and add the proxy's hostname to
`HAVEN_ALLOWED_HOSTS` (e.g. `haven.example.com`), otherwise Haven answers "421" to that name. If you can, use Tailscale or
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
