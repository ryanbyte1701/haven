# Air quality: Awair, Dyson, Levoit (VeSync)

All three are set up from **Settings** in Haven. Monitors show an air score, CO₂, VOC, PM2.5/PM10, temperature
and humidity. Purifiers add power, fan speed, mode and filter life controls.

## Awair (monitors, local)
1. In the **Awair Home** app, open each device → **Awair+** → **Developer Option** → **Local API** → Enable.
2. Give each Awair a fixed IP address in your router.
3. Haven → Settings → **Awair** → turn on → add the IP addresses (comma-separated) → Save.

Readings refresh every 60 seconds and never leave your network. The Air score (0–100) is shown as
Good (80+), Fair (60–79) or Poor (below 60).
*No local access (older models)?* Get a token from developer.getawair.com and paste it as the cloud
access token instead. The cloud is polled every 5 minutes to stay within Awair's daily limit.

## Dyson (purifiers and purifier-fans, local)
Works with the Pure Cool, Purifier Cool, Hot+Cool, Humidify+Cool and Big+Quiet ranges.
1. Haven → Settings → **Dyson** → pick your account country → Save.
2. Choose **Sign in**, enter your MyDyson email and password, then the code Dyson emails you.
   Haven only keeps each device's *local* key (in `data/dyson.json`), not your password.
3. Give each purifier a fixed IP in your router, then add it under **Devices** as `SERIAL=IP`, for example
   `TP7-US-ABC1234A=192.168.1.60`. The serial is on the sticker under the machine and in the MyDyson app.
4. Save. Haven connects to each purifier directly over your network.

Controls: power, fan speed 1–10, mode (Auto / Manual / Night) and oscillation.
Readings: PM2.5, PM10, VOC, NO₂, temperature, humidity and HEPA filter life, where the model has the sensor.
If a device shows as offline, check the IP and that the Dyson is connected to Wi-Fi in the MyDyson app.

## Levoit (VeSync cloud)
Works with the Core 200S/300S/400S/600S, Vital 100S/200S, EverestAir and LV-PUR131S.
1. Haven → Settings → **Levoit (VeSync)** → enter the email and password you use in the VeSync app.
2. Pick your account country → Save.

Controls: power, fan level and mode (the modes your model supports, e.g. Auto / Sleep / Manual / Turbo).
Readings: PM2.5, the air-quality label and filter life where the model reports them.
The sign-in token is cached in `data/vesync_auth.json`, so Haven doesn't log in on every restart.
