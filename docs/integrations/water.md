# Water integrations

Turn these on in **Settings**. Each one has its own page with the fields below.

## WaterGuru (pool and spa)

**You need:** the email and password you use in the WaterGuru app.

**What you get:** one card per pool or spa, in the room "Pool" (you can move it). It shows water temperature,
pH, free chlorine and any other readings WaterGuru takes, plus cartridge and battery levels. The status line
shows WaterGuru's own advice when something is out of range (for example "Add 1 lb of shock"), or "In range".

**Good to know**
- WaterGuru has no public API. Haven signs in the same way the app does, through WaterGuru's Amazon Cognito
  account system, then reads the app's dashboard. The Home Assistant community integration and the
  `bdwilson/waterguru-api` project do the same. If WaterGuru changes its app, this can break until it's updated.
- WaterGuru only tests a few times a day, so readings change slowly. Haven checks every 10 minutes.
- Temperatures arrive in °F and are converted. This assumes the account reports °F (the US app's default).

## Flo by Moen (smart water shutoff and water sensors)

**You need:** the email and password for the Flo app (or the Moen Smart Water app). If you created your account in
the newer **Moen** app, switch on *Moen account*.

**What you get**
- **Shutoff card:** valve open/closed with Open and Close buttons, flow (gpm), pressure (psi), water temperature,
  gallons used today, and the Home / Away / Sleep mode. Sleep lasts 2 hours, then returns to Home (one of Flo's
  allowed sleep lengths).
- **Leak alert:** the card shows a leak when Flo has a pending critical alert.
- **Flo water sensors ("pucks"):** each gets its own card with leak, temperature, humidity and battery.

**Good to know**
- Flo has no token-only sign-in, so your email and password stay in Haven's settings file on the server. That file
  is readable only by Haven and is never sent to the browser.
- Haven uses `aioflo`, the library behind Home Assistant's Flo integration. It refreshes every minute and right after
  you open or close the valve.

## Rachio (irrigation)

**You need:** an API key. Sign in at **app.rach.io**, open **Account Settings** (top-right menu), and choose **GET API KEY**.

**What you get**
- **Controller card:** a status line (Ready, Watering *zone*, Standby, Rain delay with hours left, Offline), a
  **Standby** switch, and a **Rain delay** slider (0 to 72 hours; 0 cancels).
- **Zone cards:** each enabled zone gets its own card in the room "Outdoor", showing whether it's watering, with
  **Run 5m / Run 10m / Run 15m / Stop** buttons. When it last watered is kept as a detail.

**Good to know**
- Rachio allows about 1,700 API calls a day, so Haven refreshes every 5 minutes at most (the minimum you can set),
  plus once right after a command.
- Not confirmed against a real controller: whether sending a rain delay of 0 cancels it on every model. If it
  doesn't, cancel it in the Rachio app.
