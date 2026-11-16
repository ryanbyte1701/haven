# Power: TP-Link Tapo smart plugs and power strips

Haven adds Tapo **power products only**: smart plugs, outdoor plugs and power strips. Tapo bulbs, light
strips, cameras, hubs, sensors and robot vacuums are skipped, and so are Kasa-branded devices.

Tested models (through python-kasa, the library Home Assistant uses): P100, P105, P110, P110M, P115, P125M,
P300, P304M, P306, P316M, TP15, TP25 and other `SMART.TAPOPLUG` devices.

## Set up
1. Give each plug a **fixed IP address** in your router (DHCP reservation).
2. Haven → Settings → **TP-Link Tapo plugs** → turn on.
3. Enter the **email and password of your Tapo app account**. Tapo firmware requires them even for
   local control; Haven only uses them to talk to the plugs on your network.
4. List the plugs' IP addresses, comma-separated, e.g. `192.168.1.50, 192.168.1.51` → Save.

*Automatic discovery* (on by default) sends a broadcast on the container's network. With Docker's default
bridge network the broadcast never reaches your LAN, so list the IPs. With `network_mode: host` discovery
finds plugs by itself.

## What you get
- Each plug is a card with an on/off switch. Each outlet of a power strip (P300, P304M, P306, P316M)
  is its own device, so you can put outlets in different rooms.
- Models with energy monitoring (P110, P110M, P115, P125M, P304M, P316M) also show the power being drawn
  right now (W), today's and this month's energy (kWh), and voltage and current where reported.
- **Power** in the left menu gets a *Smart plugs* table: total watts now, kWh today and a switch per plug.
  Plugs also appear under **Controls**, and can be used in automations
  (e.g. "when the Space Heater draws more than 1500 W, turn it off").

Plugs are read locally every 30 seconds (Advanced → *Refresh every*). New plugs and plugs that were
offline are picked up again automatically within a few minutes.

## Troubleshooting
- **"Tapo rejected the email/password"**: use the Tapo app account the plugs are registered to. If you
  changed your password recently, power-cycle the plug once so it picks up the new one.
- **Plug offline**: check the IP, and that the plug is on 2.4 GHz Wi-Fi and reachable from the NAS.
- **Third-party compatibility**: newer firmware can turn off local control. In the Tapo app → Me →
  Third-Party Services, make sure *Third-Party Compatibility* is **on**.
