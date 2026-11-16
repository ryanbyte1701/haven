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

Container Manager pulls the images and starts Haven and its MQTT broker. Open **http://NAS-IP:8080**, go to
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
If the DSM firewall is on (Control Panel → Security → Firewall → Edit Rules), allow these ports from your local
network: **8080** (dashboard, Ecowitt), **1883** (MQTT) and **3493** (Haven's NUT server).

### Using Haven as your Synology's UPS server
Once your UniFi UPS shows on Haven's **Power** page, go to Control Panel → Hardware & Power → **UPS** → tick
*Enable UPS support* → Network UPS type **Synology UPS server** → enter the IP of the machine running Haven → Apply.
If the Synology runs Haven itself, use the NAS's own IP, and leave *Enable network UPS server* (on the same
page) **off**, because it would take port 3493.

## 7. Day-to-day
| Task | Where |
|---|---|
| Add or change an account | Haven → **Settings** (applies immediately) |
| Rearrange the dashboard | Haven → ✎ button → drag cards and rooms → Done |
| Change the password, user or ports | Project → haven → **YAML** tab, edit, then Action → **Build** |
| See errors | Container → haven → **Log** |

## 8. Port already in use?
Change the left number in `docker-compose.yml`, e.g. `"8099:8080"`. Use that same number for the Ecowitt
gateway's port and in your browser.

## 9. Optional: HTTPS / remote access
Control Panel → Login Portal → Advanced → **Reverse Proxy** → Create:
- Source: HTTPS, your hostname, port 443 → Destination: HTTP, localhost, 8080
- **Custom Header** tab → Create → **WebSocket** (this is required for live updates)

Set `DASH_TOKEN` before exposing the dashboard outside your home. If you can, use Tailscale or
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
