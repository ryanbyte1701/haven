# ChargePoint and Roborock

## ChargePoint (home EV charger)

Works with ChargePoint home chargers such as the Home Flex, through the same community library
(`python-chargepoint`) that the Home Assistant ChargePoint integration uses.

**Set up**
1. Haven → Settings → **ChargePoint** → turn it on → Save.
2. Choose **Sign in** and enter the email and password you use in the ChargePoint app.
   Your password is used once to get a session token; only the token is stored (`data/chargepoint.json`,
   readable only by Haven). ChargePoint renews the token on every update and Haven keeps the newest one.
3. If ChargePoint's bot protection blocks the sign-in, sign in at chargepoint.com in a browser, copy the
   `coulomb_sess` cookie, and paste it under **Advanced → Session token** together with your email.

**What you get**
- Status: Ready, Plugged in, Charging, Charged, Needs attention or Offline.
- While charging: power (W), energy this session (kWh) and time charging (min).
- **Charge limit** slider (the amperage settings your charger allows) and **Start / Stop** buttons.
  Stop only appears if your charger supports remote stop.

Refreshes every 60 seconds (Advanced → Refresh every).

## Roborock (robot vacuums)

Uses `python-roborock`, the library Home Assistant's built-in Roborock integration uses. Most vacuums that
use the Roborock app (S, Q, Qrevo and similar series) are supported. A few newer models that use a
different protocol show up in the log as "not supported yet".

**Set up**
1. Haven → Settings → **Roborock** → turn it on → Save.
2. Choose **Sign in** and enter the email of your Roborock app account. Roborock emails you a code; enter it.
   Only the account token is stored (`data/roborock.json`, readable only by Haven).
3. You must have accepted the user agreement in the Roborock app, or sign-in is refused.

After sign-in Haven talks to each vacuum directly on your network when it can, and through Roborock's
cloud otherwise.

**What you get**
- State: Cleaning, Returning, Charging, Docked, Paused, Idle or Error (with the error description).
- Battery, area and time of the last clean, and the remaining life of the filter, main brush and side brush.
- **Start / Pause / Dock / Locate** buttons and a **Suction** selector with the levels your model supports.

Refreshes every 30 seconds (Advanced → Refresh every). Brush and filter life refresh every ~10 minutes.
