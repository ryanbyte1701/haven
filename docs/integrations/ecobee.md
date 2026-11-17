# ecobee thermostats and room sensors

Haven reads and controls ecobee thermostats through ecobee's cloud, and shows every remote room sensor
(SmartSensor / room sensor) as its own device.

## Set up
1. Haven → Settings → **Ecobee** → turn on → **Save**.
2. Choose **Sign in** and pick one of these:
   - **ecobee account (most people).** Enter the email and password you use at ecobee.com and leave the
     API key empty. If your account has two-step verification, Haven asks for the code from your
     authenticator app or the text message ecobee sends. Codes sent by email or push notification
     can't be answered from Haven: sign in once at ecobee.com, then try again.
   - **Developer API key (only if you already have one).** Enter just the key. Haven shows a 4-character PIN.
     On ecobee.com open the menu → **My Apps** → **Add application**, type the PIN and choose **Authorize**,
     then press **Finish sign-in** in Haven. The PIN is valid for about 9 minutes.
3. Your thermostats and sensors appear within a few seconds.

Haven keeps only ecobee's tokens (in `data/ecobee.json`, readable only by Haven), never your password. They
renew automatically. If ecobee ends the session (for example you removed Haven under My Apps, or changed
your password), Settings → Ecobee says **Needs sign-in**: sign in again.

### About API keys
ecobee stopped accepting new developer registrations in early 2025, and its developer page still says it
isn't taking new ones, so most people can't get an API key. Keys made before then still work. The account
sign-in doesn't need a key. It uses the same login as ecobee's own website, which is what Home Assistant's
built-in ecobee integration has used since release 2026.3. ecobee hasn't documented that login for other
apps, so a change on ecobee's side could break it until Haven is updated.

HomeKit (local) control isn't supported.

## What you get
**Thermostat:** temperature, humidity, what it's doing (Heating / Cooling / Fan running / Idle, with the
comfort setting or "Hold"), occupancy from the thermostat's own sensor, and the outside temperature from
ecobee's weather.
Controls: **Mode** (Heat, Cool, Auto, Off, plus Aux heat only on heat pumps; only the modes your
system supports are shown), **Heat to** and **Cool to** setpoints (in °F or °C, following Settings →
Appearance), **Fan** auto/on and **Resume schedule**.

Setpoint and fan changes are sent as a hold. By default it lasts until the next schedule change. Set
**Keep temperature changes** to *Until I resume the schedule* for a hold that stays until you press
Resume schedule. In Auto mode Haven keeps the heat and cool setpoints at least as far apart as the
thermostat requires (usually 5 °F).

**Room sensors:** temperature and occupancy (Occupied / Clear). A sensor that ecobee can't hear from
(dead battery, out of range) shows as offline.

## Updates
ecobee asks apps to check for changes no more often than every 3 minutes. Haven reads ecobee's small
"what changed" summary every 3 minutes and only downloads the full thermostat data when something did
change, plus right after you change a setting. **Refresh now** on the settings page fetches everything
at once.
