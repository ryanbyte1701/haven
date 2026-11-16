(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const root = document.documentElement;
  const devices = new Map();
  const sparks = new Map();
  const store = (k, v) => { try { v === undefined ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch {} };
  const load = (k, d) => { try { return localStorage.getItem(k) ?? d; } catch { return d; } };
  let view = load("filter", "all");
  let mode = load("viewmode", "cards");
  let sort = JSON.parse(load("sort", '{"key":"room","dir":1}'));
  let unit = load("unit", "F");
  let token = load("token", "");
  let appTitle = "Haven";
  let timeCfg = { hour12: undefined, timeZone: undefined };
  const timeOpts = () => ({ ...(timeCfg.hour12 !== undefined ? { hour12: timeCfg.hour12 } : {}), ...(timeCfg.timeZone ? { timeZone: timeCfg.timeZone } : {}) });
  let layout = { rooms: [], order: {}, pinned: [], views: [] };
  const VIEW_TYPES = { climate: "Climate", lights: "Lights", water: "Water", doors: "Doors", security: "Security",
    controls: "Controls", battery: "Batteries", power: "UPS" };
  const VIEW_ICONS = ["dashboard", "home", "bedtime", "lightbulb", "thermostat", "water_drop", "sensor_door", "shield_lock",
    "power", "grass", "partly_cloudy_day", "videocam", "home_work", "tune", "battery_full", "lan", "sprinkler", "detector_smoke"];
  const customView = () => view.startsWith("v:") ? (layout.views || []).find(v => "v:" + v.id === view) : null;
  let cardStyle = load("cardStyle", "standard");
  let dragging = false, sortables = [];
  const FAV = "★ Favorites";

  const SEEDS = { ocean: "#1E88E5", violet: "#6750A4", teal: "#00897B", sage: "#7C9A3D", desert: "#D9772B" };
  const GROUPS = {
    climate: ["temperature", "humidity", "pressure", "co2", "pm25", "pm10", "voc", "aqi", "air_score", "illuminance", "wind_speed", "rain_daily", "moisture"],
    water: ["leak", "valve", "moisture", "rain_daily", "flow_rate", "water_pressure", "water_usage_today", "ph", "orp", "free_chlorine", "watering"],
    doors: ["contact"],
    security: ["contact", "lock", "motion", "presence", "vibration", "tamper", "smoke", "co", "co_ppm", "heat", "alarm_panel", "ding"],
    controls: ["lock", "valve", "switch", "action", "light", "alarm_panel"],
    lights: ["light"],
  };
  const PAGES = new Set(["battery", "power", "settings", "devices", "air"]);
  const pluginPages = {};   // id -> { label, icon, title, render(el), onDevices?() }
  const TABS = [["climate", "Climate", "thermostat"], ["lights", "Lights", "lightbulb"], ["water", "Water", "water_drop"],
    ["doors", "Doors", "sensor_door"], ["security", "Security", "shield_lock"], ["controls", "Controls", "tune"]];
  const TAB_IDS = TABS.map(t => t[0]);
  const TITLES = { lights: "Lighting", climate: "Climate", water: "Water", doors: "Doors & windows", security: "Security", controls: "Controls",
    battery: "Batteries", power: "Power", settings: "Settings", devices: "Devices", air: "Air quality" };
  const LABEL = { humidity: "Humidity", moisture: "Soil", pressure: "Pressure", illuminance: "Light", co2: "CO₂", pm25: "PM2.5",
    wind_speed: "Wind", wind_gust: "Gust", rain_daily: "Rain", rain_rate: "Rate", uv: "UV", solar: "Solar",
    co_ppm: "CO", power: "Power", runtime: "Runtime", load: "Load", input_voltage: "Input", output_voltage: "Output",
    aqi: "AQI", air_score: "Air score", voc: "VOC", pm10: "PM10", ph: "pH", orp: "ORP", free_chlorine: "Chlorine",
    flow_rate: "Flow", water_pressure: "Pressure", water_usage_today: "Used today", cartridge_life: "Cartridge",
    filter_life: "Filter", fan_speed: "Fan", mode: "Mode", oscillation: "Oscillate", energy_session: "Session",
    charge_time: "Charging", amperage_limit: "Limit", clean_area: "Area", clean_time: "Clean time",
    main_brush_life: "Brush", side_brush_life: "Side brush", rain_delay: "Rain delay", standby: "Standby",
    energy_today: "Today", energy_month: "This month", voltage: "Voltage", current: "Current" };
  const pretty = k => LABEL[k] || (/^run_(\d+)m$/.test(k) ? `Run ${k.match(/\d+/)[0]} min` : null) || String(k).replace(/_/g, " ").replace(/^./, c => c.toUpperCase());
  const kindOf = d => d.attributes?.kind || "";
  const META_ICON = { wind_speed: "air", wind_gust: "air", rain_daily: "rainy", rain_rate: "rainy", uv: "sunny", solar: "sunny",
    co2: "co2", pm25: "air", pressure: "science", illuminance: "light_mode", co_ppm: "detector_smoke", moisture: "grass",
    runtime: "timer", load: "speed", input_voltage: "electrical_services", power: "bolt",
    voc: "aq", pm10: "air", aqi: "aq", co: "co2", filter_life: "filter_alt", cartridge_life: "filter_alt", flow_rate: "waves",
    water_pressure: "speed", water_usage_today: "water", free_chlorine: "science", orp: "science", ph: "water_ph",
    energy_session: "bolt", energy_today: "bolt", energy_month: "bolt", voltage: "electrical_services", current: "electrical_services", charge_time: "timer", clean_area: "grid_view", clean_time: "timer", main_brush_life: "cleaning_services", side_brush_life: "cleaning_services" };
  const SRC = { yolink: "YoLink", aqara: "Aqara", ecowitt: "Ecowitt", xsense: "X-Sense", mqtt: "MQTT", demo: "Demo", nut: "UPS", ring: "Ring", hue: "Hue",
    waterguru: "WaterGuru", flo: "Flo by Moen", rachio: "Rachio", awair: "Awair", dyson: "Dyson", vesync: "Levoit (VeSync)",
    chargepoint: "ChargePoint", roborock: "Roborock", airquality: "Outdoor AQI", tapo: "Tapo" };
  const ALARM_LABEL = { disarmed: "Disarmed", armed_home: "Armed · Home", armed_away: "Armed · Away", armed_night: "Armed · Night",
    arming: "Arming…", pending: "Entry delay", triggered: "TRIGGERED" };

  // ---------- icons ----------
  const icon = (name, filled = false) => {
    const p = window.MD_ICONS[name] || window.MD_ICONS.sensors;
    return `<svg class="ic" viewBox="0 -960 960 960" aria-hidden="true"><path d="${p[filled ? 1 : 0]}"/></svg>`;
  };
  const hydrateIcons = (el = document) => $$("[data-icon]", el).forEach(n => {
    if (!n.querySelector(":scope > svg.ic")) n.insertAdjacentHTML("afterbegin", icon(n.dataset.icon, n.hasAttribute("data-fill")));
  });

  // ---------- helpers ----------
  const api = (path, opts = {}) => fetch(path, { ...opts, headers: { "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}) } })
    .then(async r => { if (r.status === 401) { askToken(); throw new Error("unauthorized"); }
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText); return r.json(); });
  const msgHooks = [];
  function askToken() { const t = prompt("Dashboard password"); if (t) { token = t; store("token", t); location.reload(); } else signInBanner(); }
  function signInBanner() {
    if ($("#signin-banner")) return;
    document.body.insertAdjacentHTML("afterbegin", `<div id="signin-banner" class="lnotice signin">${icon("key")}<span><b>Password needed.</b> This dashboard is protected.</span>
      <button class="filled-btn" id="signin-btn">Sign in</button></div>`);
    $("#signin-btn").onclick = () => { $("#signin-banner").remove(); askToken(); };
  }
  const toUnit = c => unit === "F" ? c * 9 / 5 + 32 : c;
  const temp = c => c == null ? "–" : toUnit(c).toFixed(1);
  const ago = ts => { if (!ts) return ""; const s = Date.now() / 1000 - ts;
    return s < 60 ? "Just now" : s < 3600 ? `${Math.floor(s / 60)} min ago` : s < 86400 ? `${Math.floor(s / 3600)} h ago` : `${Math.floor(s / 86400)} d ago`; };
  const val = (d, k) => d.capabilities[k]?.value;
  const has = (d, k) => k in d.capabilities;
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmt = v => typeof v === "number" ? +v.toFixed(1) : esc(v);
  const n = v => v == null ? "–" : fmt(v);                        // any device value going into HTML
  const safeColor = c => /^#[0-9a-f]{6}$/i.test(String(c || "")) ? c : null;
  const visible = () => [...devices.values()].filter(d => !d.hidden);
  const lastUpdated = d => Math.max(0, ...Object.values(d.capabilities).map(c => c.updated || 0));
  const srcLabel = d => { const s = (d.model || "").startsWith("demo (") ? d.model.slice(6, -1) : d.source; return SRC[s] || s; };
  const runtimeText = m => m == null ? "–" : m >= 90 ? `${Math.floor(m / 60)} h ${Math.round(m % 60)} min` : `${Math.round(m)} min`;

  const isAlarm = d => ["leak", "smoke", "co", "heat"].some(k => val(d, k) === true) || (val(d, "co_ppm") ?? 0) >= 50 ||
    val(d, "alarm_panel") === "triggered" || val(d, "ups_status") === "low_battery";
  const needsAttention = d => isAlarm(d) || (val(d, "contact") === "open" && !has(d, "lock")) || val(d, "lock") === "unlocked" ||
    val(d, "ups_status") === "on_battery" || batteryLow(d) || !d.online;
  const batteryLow = d => { const b = val(d, "battery"); return (b != null && b <= 25) || val(d, "battery_low") === true; };
  const isOutdoor = d => /outdoor|weather|backyard|patio|garden/i.test(`${d.room} ${d.name}`);
  const isUps = d => has(d, "ups_status");
  const isPlug = d => kindOf(d) === "smart_plug";
  const coAlarm = d => has(d, "co") && typeof val(d, "co") !== "number";   // outdoor AQI services report CO as a number
  const isLight = d => d.attributes?.kind === "light" || d.attributes?.kind === "group" || (has(d, "light") && has(d, "brightness"));
  const isGroup = d => d.attributes?.kind === "group";
  const PRESETS = ["#ffb46b", "#ffe9c9", "#ff5a4e", "#ff9f1c", "#7bd389", "#4cc2ff", "#8f6bff", "#ff6fb5"];
  function kelvinRgb(k) {   // Tanner Helland approximation, good enough for a glow color
    const t = k / 100; let r, g, b;
    r = t <= 66 ? 255 : 329.7 * Math.pow(t - 60, -0.1332);
    g = t <= 66 ? 99.47 * Math.log(t) - 161.12 : 288.12 * Math.pow(t - 60, -0.0755);
    b = t >= 66 ? 255 : t <= 19 ? 0 : 138.52 * Math.log(t - 10) - 305.04;
    const c = v => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, "0");
    return `#${c(r)}${c(g)}${c(b)}`;
  }
  const glow = d => safeColor(val(d, "color")) || (Number(val(d, "color_temp")) ? kelvinRgb(Number(val(d, "color_temp"))) : "#ffd27a");

  // ---------- per-device presentation ----------
  function deviceIcon(d) {
    const k = kindOf(d);
    if (k === "pool") return ["pool", /low|high|out|alert/i.test(val(d, "status_text") || "") ? "warn" : ""];
    if (k === "air_monitor") { const sc = val(d, "air_score"); return ["aq_indoor", sc != null && sc < 60 ? "warn" : ""]; }
    if (k === "smart_plug") return ["outlet", val(d, "switch") ? "active" : ""];
    if (k === "air_purifier") return ["air_purifier", val(d, "switch") ? "active" : ""];
    if (k === "ev_charger") { const c = val(d, "charger_status"); return ["ev_station", c === "charging" ? "active" : c === "fault" ? "bad" : ""]; }
    if (k === "vacuum") { const v = val(d, "vacuum_state"); return ["vacuum", v === "cleaning" || v === "returning" ? "active" : v === "error" ? "bad" : ""]; }
    if (k === "irrigation_zone") return ["sprinkler", val(d, "watering") ? "active" : ""];
    if (k === "irrigation_controller") return ["sprinkler", val(d, "standby") ? "warn" : ""];
    if (k === "water_monitor" && !val(d, "leak")) return ["waves", (val(d, "flow_rate") || 0) > 0 ? "active" : ""];
    if (isUps(d)) { const s = val(d, "ups_status"); return [s === "online" ? "battery_charging_full" : "battery_alert", s === "low_battery" ? "bad" : s === "on_battery" ? "warn" : ""]; }
    if (isLight(d)) return [isGroup(d) ? "home_iot_device" : "lightbulb", ""];
    if (has(d, "alarm_panel")) { const s = val(d, "alarm_panel"); return ["shield", s === "triggered" ? "bad" : s && s !== "disarmed" ? "active" : ""]; }
    if (k === "aqi_station") { const a = val(d, "aqi"); return ["aq", a > 150 ? "bad" : a > 100 ? "warn" : ""]; }
    if (has(d, "smoke") || coAlarm(d) || has(d, "heat")) return ["detector_smoke", isAlarm(d) ? "bad" : ""];
    if (has(d, "leak")) return val(d, "leak") ? ["water_damage", "bad"] : ["water_drop", ""];
    if (has(d, "valve")) return ["valve", val(d, "valve") === "open" ? "active" : ""];
    if (has(d, "lock")) return val(d, "lock") === "unlocked" ? ["lock_open", "warn"] : ["lock", ""];
    if (has(d, "ding")) return ["doorbell", val(d, "ding") ? "warn" : val(d, "motion") ? "active" : ""];
    if (d.source === "ring" || d.attributes?.has_snapshot) return [has(d, "light") ? "lightbulb" : "videocam", val(d, "motion") ? "active" : ""];
    if (has(d, "contact")) return val(d, "contact") === "open" ? ["door_open", "warn"] : ["door_front", ""];
    if (has(d, "presence")) return ["sensor_occupied", val(d, "presence") ? "active" : ""];
    if (has(d, "motion")) return ["directions_run", val(d, "motion") ? "active" : ""];
    if (has(d, "switch")) return ["power_settings_new", val(d, "switch") ? "active" : ""];
    if (has(d, "wind_speed") || has(d, "rain_daily")) return ["partly_cloudy_day", ""];
    if (has(d, "moisture") && !has(d, "temperature")) return ["grass", ""];
    if (has(d, "temperature")) return ["device_thermostat", ""];
    return ["sensors", ""];
  }

  /** Main state as [text, tone] — used by cards (status line) and the table. */
  const VAC = { docked: "Docked", cleaning: "Cleaning", returning: "Returning to dock", paused: "Paused", charging: "Charging", idle: "Idle", error: "Error" };
  const EV = { available: "Not plugged in", plugged_in: "Plugged in", charging: "Charging", done: "Charged", fault: "Fault", offline: "Offline" };
  function statusOf(d) {
    const k = kindOf(d);
    if (has(d, "vacuum_state")) { const v = val(d, "vacuum_state"); return [VAC[v] || pretty(v || "unknown"), v === "error" ? "bad" : ""]; }
    if (has(d, "charger_status")) { const v = val(d, "charger_status"); return [EV[v] || pretty(v || "unknown"), v === "fault" ? "bad" : ""]; }
    if (k === "irrigation_zone") return val(d, "watering") ? ["Watering", ""] : ["Idle", ""];
    if (k === "smart_plug") return val(d, "switch") ? [`On${val(d, "power") != null ? " · " + fmt(val(d, "power")) + " W" : ""}`, ""] : ["Off", ""];
    if (k === "air_purifier") return val(d, "switch") ? [`On${val(d, "mode") ? " · " + pretty(val(d, "mode")) : ""}`, ""] : ["Off", ""];
    if (k === "irrigation_controller" && val(d, "standby")) return ["Standby", "warn"];
    if (isUps(d)) return { online: ["Online", ""], on_battery: ["On battery", "warn"], low_battery: ["Battery low", "bad"], offline: ["Off", "warn"] }[val(d, "ups_status")] || ["Unknown", ""];
    if (has(d, "alarm_panel")) { const s = val(d, "alarm_panel"); return [ALARM_LABEL[s] || s || "Unknown", s === "triggered" ? "bad" : ""]; }
    if (has(d, "smoke") || coAlarm(d)) return val(d, "smoke") ? ["Smoke detected", "bad"] : val(d, "co") ? ["Carbon monoxide", "bad"] : ["All clear", ""];
    if (has(d, "heat")) return val(d, "heat") ? ["High heat", "bad"] : ["Normal", ""];
    if (has(d, "leak")) return val(d, "leak") ? ["Leak detected", "bad"] : ["Dry", ""];
    if (has(d, "lock")) return val(d, "lock") === "locked" ? ["Locked", ""] : val(d, "lock") ? ["Unlocked", "warn"] : ["Unknown", ""];
    if (has(d, "valve")) return val(d, "valve") === "open" ? ["Open", ""] : val(d, "valve") ? ["Closed", ""] : ["Unknown", ""];
    if (has(d, "ding")) return val(d, "ding") ? ["Doorbell!", "warn"] : val(d, "motion") ? ["Motion", ""] : ["Quiet", ""];
    if (has(d, "contact")) return val(d, "contact") === "open" ? ["Open", "warn"] : ["Closed", ""];
    if (has(d, "presence")) return val(d, "presence") ? ["Occupied", ""] : ["Clear", ""];
    if (has(d, "motion")) return val(d, "motion") ? ["Motion", ""] : ["No motion", ""];
    if (isLight(d)) {
      if (isGroup(d)) { const m = (d.attributes.members || []).map(id => devices.get(id)).filter(Boolean);
        const on = m.filter(x => val(x, "light")).length; return [val(d, "light") ? `${on || "Some"} of ${m.length || "?"} on` : "All off", ""]; }
      return val(d, "light") ? [has(d, "brightness") ? `On · ${val(d, "brightness") ?? "–"}%` : "On", ""] : ["Off", ""]; }
    if (has(d, "light")) return val(d, "light") ? ["Light on", ""] : ["Light off", ""];
    if (has(d, "switch")) return val(d, "switch") ? ["On", ""] : ["Off", ""];
    if (val(d, "status_text")) return [val(d, "status_text"), /low|high|alert|fault|error|problem|leak/i.test(val(d, "status_text")) ? "warn" : ""];
    return null;
  }
  function readingText(d) {
    if (has(d, "temperature")) return `${temp(val(d, "temperature"))}°${unit}`;
    if (has(d, "moisture")) return `${val(d, "moisture") == null ? "–" : Math.round(val(d, "moisture"))}% soil`;
    if (isUps(d)) return `${n(val(d, "battery"))}% · ${runtimeText(val(d, "runtime"))}`;
    if (kindOf(d) === "aqi_station") return `AQI ${n(val(d, "aqi"))}`;
    if (has(d, "air_score")) return `Score ${n(val(d, "air_score"))}`;
    if (has(d, "ph")) return `pH ${n(val(d, "ph"))}`;
    if (val(d, "charger_status") === "charging" && val(d, "power") != null) return `${(val(d, "power") / 1000).toFixed(1)} kW`;
    return "";
  }
  function detailText(d) {
    const parts = [];
    if (has(d, "temperature") && val(d, "humidity") != null) parts.push(`${Math.round(val(d, "humidity"))}% RH`);
    if (has(d, "temperature") && has(d, "moisture")) parts.push(`soil ${Math.round(val(d, "moisture"))}%`);
    if (isUps(d)) parts.push(`load ${n(val(d, "load"))}%`, `${val(d, "input_voltage") ?? "–"} V in`);
    for (const k of ["wind_speed", "rain_daily", "co2", "pm25", "co_ppm", "illuminance", "pressure"])
      if (val(d, k) != null) parts.push(`${LABEL[k]} ${fmt(val(d, k))}${d.capabilities[k].unit ? " " + d.capabilities[k].unit : ""}`);
    if (has(d, "contact") && has(d, "lock")) parts.push(`door ${val(d, "contact") || "–"}`);
    const lm = d.attributes?.last_motion, ld = d.attributes?.last_ding;
    if (ld) parts.push(`ding ${ago(ld).toLowerCase()}`);
    if (lm) parts.push(`motion ${ago(lm).toLowerCase()}`);
    return parts.join(" · ");
  }

  function readingBlock(d) {
    const k = kindOf(d);
    if (k === "aqi_station") return `<div class="reading"><span class="v" style="color:${aqiColor(val(d, "aqi"))}">${n(val(d, "aqi"))}<small> AQI</small></span>${
      d.attributes?.dominant ? `<span class="sub">${icon("aq")}${esc(d.attributes.dominant)}</span>` : ""}</div>`;
    if (has(d, "air_score")) return `<div class="reading"><span class="v">${n(val(d, "air_score"))}<small>/100</small></span>${
      val(d, "co2") != null ? `<span class="sub">${icon("co2")}${n(Math.round(val(d, "co2")))}</span>` : ""}</div>`;
    if (has(d, "ph")) return `<div class="reading"><span class="v"><small>pH </small>${n(val(d, "ph"))}</span>${
      val(d, "free_chlorine") != null ? `<span class="sub">${icon("science")}${n(val(d, "free_chlorine"))} ppm</span>` : ""}</div>`;
    if (k === "air_purifier" && val(d, "pm25") != null) return `<div class="reading"><span class="v">${Math.round(val(d, "pm25"))}<small> PM2.5</small></span></div>`;
    if (k === "ev_charger" && val(d, "charger_status") === "charging" && val(d, "power") != null)
      return `<div class="reading"><span class="v">${(val(d, "power") / 1000).toFixed(1)}<small> kW</small></span>${val(d, "energy_session") != null ? `<span class="sub">${icon("bolt")}${n(val(d, "energy_session"))} kWh</span>` : ""}</div>`;
    if (k === "water_monitor" && val(d, "flow_rate") != null) return `<div class="reading"><span class="v">${(+val(d, "flow_rate")).toFixed(1)}<small> gpm</small></span>${
      val(d, "water_pressure") != null ? `<span class="sub">${icon("speed")}${Math.round(val(d, "water_pressure"))} psi</span>` : ""}</div>`;
    if (has(d, "temperature")) {
      return `<div class="reading"><span class="v">${temp(val(d, "temperature"))}<small>°${unit}</small></span>${
        has(d, "humidity") ? `<span class="sub">${icon("water_drop")}${val(d, "humidity") == null ? "–" : Math.round(val(d, "humidity"))}%</span>` : ""}</div>`;
    }
    if (has(d, "moisture")) return `<div class="reading"><span class="v">${val(d, "moisture") == null ? "–" : Math.round(val(d, "moisture"))}<small>% soil</small></span></div>`;
    if (isUps(d)) return `<div class="reading"><span class="v">${n(val(d, "battery"))}<small>%</small></span><span class="sub">${icon("timer")}${runtimeText(val(d, "runtime"))}</span></div>`;
    return "";
  }
  function statusBlock(d) {
    const s = statusOf(d);
    if (!s) return "";
    const extra = has(d, "contact") && has(d, "lock") ? `Door ${val(d, "contact") || "–"}` :
      d.attributes?.last_ding ? `Last ring ${ago(d.attributes.last_ding).toLowerCase()}` :
      d.attributes?.last_motion ? `Last motion ${ago(d.attributes.last_motion).toLowerCase()}` : "";
    return `<div class="status ${esc(s[1])}">${esc(s[0])}${extra ? `<span class="s2">${esc(extra)}</span>` : ""}</div>`;
  }
  function metaBlock(d) {
    const skip = new Set(["temperature", "humidity", "battery", "battery_low", "leak", "smoke", "co", "heat", "contact", "lock", "valve",
      "switch", "motion", "presence", "vibration", "action", "tamper", "ups_status", "charging", "replace_battery", "overload",
      "output_voltage", "runtime", "alarm_panel", "ding", "light", "wind_dir", "brightness", "color_temp", "color",
      "status_text", "vacuum_state", "charger_status", "charging", "watering", "air_score", "ph", "mode", "fan_speed", "oscillation",
      "standby", "rain_delay", "amperage_limit"]);
    const k = kindOf(d);
    if (k === "pool") skip.add("free_chlorine");
    if (k === "water_monitor") { skip.add("flow_rate"); skip.add("water_pressure"); }
    if (has(d, "air_score")) skip.add("co2");
    if (k === "aqi_station") skip.add("aqi");
    if (k === "air_purifier") skip.add("pm25");
    if (k === "ev_charger" && val(d, "charger_status") === "charging") { skip.add("power"); skip.add("energy_session"); }
    if (!has(d, "temperature")) skip.add("moisture");
    const chips = [];
    if (isUps(d) && val(d, "charging")) chips.push(`<span>${icon("bolt")}Charging</span>`);
    if (val(d, "replace_battery")) chips.push(`<span class="low">${icon("battery_alert")}Replace battery</span>`);
    for (const [k, c] of Object.entries(d.capabilities)) {
      if (skip.has(k) || c.value == null) continue;
      if (typeof c.value === "boolean") continue;
      chips.push(`<span>${META_ICON[k] ? icon(META_ICON[k]) : ""}${esc(c.label || pretty(k))} ${fmt(c.value)}${c.unit ? " " + esc(c.unit) : ""}</span>`);
    }
    const b = val(d, "battery");
    if (!isUps(d)) {
      if (b != null) chips.push(`<span class="${b <= 25 ? "low" : ""}">${icon(batteryIcon(b))}${b}%</span>`);
      else if (val(d, "battery_low")) chips.push(`<span class="low">${icon("battery_alert")}Low</span>`);
    }
    return chips.length ? `<div class="meta">${chips.join("")}</div>` : "";
  }
  const batteryIcon = b => b == null ? "battery_full" : b <= 10 ? "battery_alert" : b <= 25 ? "battery_1_bar" : b <= 60 ? "battery_3_bar" : b <= 90 ? "battery_5_bar" : "battery_full";

  // Estimated row heights (px) so cards can fit content into a fixed height.
  const rowH = html => html.startsWith('<div class="slider-row') ? 28 : html.startsWith('<div class="colors') ? 26 : 40;
  const controlsBlock = (d, compact = false) => controlRows(d).join("");
  function controlRows(d) {
    const c = d.capabilities, out = [];
    const seg = (capName, items) => `<div class="segmented">${items.map(([v, label, ic, on]) =>
      `<button data-cmd="${esc(capName)}" data-val="${esc(JSON.stringify(v))}" class="${on ? "on" : ""}" aria-pressed="${!!on}">${on ? icon("check_circle", true) : icon(ic)}${esc(label)}</button>`).join("")}</div>`;
    const sw = (capName, on, label) => `<div class="switch-row"><span>${esc(label)}</span>
      <button class="m3-switch ${on ? "on" : ""}" role="switch" aria-checked="${!!on}" data-cmd="${esc(capName)}" data-val="${esc(JSON.stringify(!on))}" aria-label="${esc(label)} ${esc(d.name)}"></button></div>`;
    if (c.lock?.writable) out.push(seg("lock", [["lock", "Lock", "lock", val(d, "lock") === "locked"], ["unlock", "Unlock", "lock_open", val(d, "lock") === "unlocked"]]));
    if (c.valve?.writable) out.push(seg("valve", [["open", "Open", "water", val(d, "valve") === "open"], ["close", "Close", "valve", val(d, "valve") === "closed"]]));
    if (c.alarm_panel?.writable) { const s = val(d, "alarm_panel");
      out.push(seg("alarm_panel", [["disarm", "Off", "shield", s === "disarmed"], ["arm_home", "Home", "bedtime", s === "armed_home"], ["arm_away", "Away", "directions_walk", s === "armed_away"]])); }
    if (c.switch?.writable) out.push(sw("switch", val(d, "switch"), "Power"));
    // generic controls for any other writable capability (purifier modes, fan speed, charge limit, standby…)
    const HANDLED = new Set(["lock", "valve", "alarm_panel", "switch", "light", "brightness", "color_temp", "color", "action"]);
    for (const [k, cp] of Object.entries(c)) {
      if (!cp.writable || HANDLED.has(k)) continue;
      const off = (has(d, "switch") && !val(d, "switch")) || (has(d, "light") && !val(d, "light"));
      if (cp.options?.length) {
        const opts = cp.options;
        out.push(opts.length <= 3 ? seg(k, opts.map(o => [o, pretty(o), "tune", String(cp.value) === String(o)]))
          : `<label class="selrow"><span>${esc(cp.label || pretty(k))}</span><select data-cmdsel="${esc(k)}">${opts.map(o => `<option value="${esc(o)}" ${String(cp.value) === String(o) ? "selected" : ""}>${esc(pretty(o))}</option>`).join("")}</select></label>`);
      } else if (cp.min != null && cp.max != null) {
        out.push(slider(k, cp.value, cp.min, cp.max, META_ICON[k] || (k === "fan_speed" ? "mode_fan" : "tune"), "", off, v => `${v}${cp.unit ? " " + cp.unit : ""}`, cp.step, `${d.name} ${cp.label || pretty(k)}`));
      } else if (typeof cp.value === "boolean" || cp.value == null) {
        out.push(sw(k, !!cp.value, cp.label || pretty(k)));
      }
    }
    if (c.light?.writable && isLight(d)) out.push(lightControls(d, view === "lights" || panelFull));
    else if (c.light?.writable) out.push(sw("light", val(d, "light"), "Light"));
    const btns = [];
    if (d.attributes?.has_snapshot) btns.push(`<button class="tonal-btn" data-snapshot>${icon("photo_camera")}Snapshot</button>`);
    if (c.action?.writable) btns.push(...(c.action.options || []).map(o =>
      `<button class="tonal-btn" data-cmd="action" data-val="${esc(JSON.stringify(o))}">${icon({ mute: "notifications", siren: "campaign", start: "power_settings_new", stop: "power_off", pause: "timer", dock: "home", locate: "notifications_active" }[o] || (/^run/.test(o) ? "sprinkler" : "science"))}${esc(pretty(o))}</button>`));
    if (btns.length) out.push(`<div class="tonal-row">${btns.join("")}</div>`);
    return out.flatMap(x => Array.isArray(x) ? x : [x]).filter(Boolean);
  }

  function slider(capName, value, min, max, ic, cls = "", disabled = false, label = v => `${v}%`, step = 1, ariaName = "") {
    const num = (x, dflt) => Number.isFinite(Number(x)) && x !== null && x !== "" ? Number(x) : dflt;
    min = num(min, 0); max = num(max, 100); if (max <= min) max = min + 1; step = num(step, 1) || 1;
    const v = Math.min(max, Math.max(min, num(value, min))), pct = ((v - min) / (max - min) * 100).toFixed(1);
    const suffix = label(0).replace(/^0/, "");
    return `<div class="slider-row">${icon(ic)}<input type="range" class="m3-slider ${cls}" min="${min}" max="${max}" step="${step || 1}" value="${v}" data-slider="${esc(capName)}" data-suffix="${esc(suffix)}"
      style="--pct:${pct}%" ${disabled ? "disabled" : ""} aria-label="${esc(ariaName || pretty(capName))}"><output>${esc(label(v))}</output></div>`;
  }
  const phone = () => innerWidth < 840;
  const headSwitch = d => !phone() && isLight(d) && d.capabilities.light?.writable ? `<button class="m3-switch ${val(d, "light") ? "on" : ""}" role="switch"
    aria-checked="${!!val(d, "light")}" data-cmd="light" data-val="${esc(JSON.stringify(!val(d, "light")))}" aria-label="Toggle ${esc(d.name)}"></button>` : "";
  function lightControls(d, full) {
    const on = !!val(d, "light"), c = d.capabilities, a = d.attributes || {};
    const parts = [];   // the on/off switch lives in the card header (see headSwitch), or a row on phones
    if (phone() && c.light?.writable) parts.push(`<div class="switch-row"><span>Light</span><button class="m3-switch ${on ? "on" : ""}" role="switch" aria-checked="${on}" data-cmd="light" data-val="${esc(JSON.stringify(!on))}" aria-label="Toggle ${esc(d.name)}"></button></div>`);
    if (c.brightness) parts.push(slider("brightness", val(d, "brightness"), 1, 100, "light_mode", "", !on, undefined, 1, `${d.name} brightness`));
    if (full && c.color_temp) parts.push(slider("color_temp", val(d, "color_temp"), a.ct_min || 2200, a.ct_max || 6500, "thermostat", "ct", !on, v => `${v}K`, 1, `${d.name} color temperature`));
    if (full && c.color) parts.push(`<div class="colors">${PRESETS.map(h => `<button data-color="${h}" style="background:${h}" class="${(val(d, "color") || "").toLowerCase() === h ? "on" : ""}" aria-label="Color ${h}"></button>`).join("")}
      <label title="Custom color"><input type="color" data-colorpick value="${safeColor(val(d, "color")) || "#ffffff"}" aria-label="Custom color for ${esc(d.name)}"></label></div>`);
    return parts;
  }

  const sparkCap = d => !hasControls(d) && !isUps(d) && ["temperature", "moisture", "humidity"].find(k => has(d, k) && typeof val(d, k) === "number");
  const hasControls = d => Object.values(d.capabilities).some(c => c.writable) || d.attributes?.has_snapshot;
  function sparkSvg(points, capName, H = 40) {
    if (!points || points.length < 2) return "";
    const vs = points.map(p => capName === "temperature" ? toUnit(p.v) : p.v);
    const min = Math.min(...vs), max = Math.max(...vs), span = max - min || 1, W = 220;
    const xy = vs.map((v, i) => [i / (vs.length - 1) * W, H - 2 - (v - min) / span * (H - 4)]);
    const line = xy.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join("");
    return `<svg class="spark" style="height:${H}px" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><path class="area" d="${line}L${W},${H}L0,${H}Z"/><path class="line" d="${line}"/></svg>`;
  }

  // ---------- layout: room order, device order, favorites ----------
  const roomRank = r => { const i = layout.rooms.indexOf(r); return i < 0 ? 1e6 : i; };
  const devRank = (room, id) => { const o = layout.order[room] || []; const i = o.indexOf(id); return i < 0 ? 1e6 : i; };
  const editing = () => document.body.classList.contains("editing");
  function orderRooms(byRoom, floatAlarms = true) {
    const alarmed = r => byRoom[r].some(isAlarm);
    return Object.keys(byRoom).sort((a, b) =>
      (floatAlarms && !editing() ? alarmed(b) - alarmed(a) : 0) ||          // safety first, except while arranging
      roomRank(a) - roomRank(b) || (a === "Other") - (b === "Other") || a.localeCompare(b));
  }
  function orderDevices(room, items, fallback) {
    return items.sort((a, b) => devRank(room, a.id) - devRank(room, b.id) || fallback(a, b));
  }
  function roomSections(shown, { favorites = false, fallback = (a, b) => a.name.localeCompare(b.name), extraFirst = "" } = {}) {
    const pinned = favorites ? layout.pinned.map(id => shown.find(d => d.id === id)).filter(Boolean) : [];
    const pinnedIds = new Set(pinned.map(d => d.id));
    const byRoom = {};
    for (const d of shown) if (!pinnedIds.has(d.id)) (byRoom[d.room || "Other"] ||= []).push(d);
    const render1 = (name, items, isFav = false) => `<section class="room ${isFav ? "fav" : ""}" data-room="${esc(name)}">
      <h2>${editing() && !isFav ? `<span class="grip">${icon("drag_indicator")}</span>` : ""}${esc(name)}</h2>
      <div class="grid" data-room="${esc(name)}">${items.map(card).join("") || (isFav ? `<p class="drop-hint">Drag cards here to pin them to the top of Home</p>` : "")}</div></section>`;
    const fav = favorites && (pinned.length || editing()) ? render1(FAV, pinned, true) : "";
    return extraFirst + fav + orderRooms(byRoom).map(r => render1(r, orderDevices(r, byRoom[r], fallback))).join("");
  }

  function card(d) {
    if (cardStyle === "minimal") return miniCard(d);
    const [ic, tone] = deviceIcon(d);
    const sc = sparkCap(d);
    const updated = lastUpdated(d);
    const lit = isLight(d) && val(d, "light");
    return `<article class="card ${isAlarm(d) ? "alarm" : ""} ${d.online ? "" : "offline"} ${lit ? "lit" : ""} ${isGroup(d) ? "group" : ""}" data-id="${esc(d.id)}" ${lit ? `style="--lit:${glow(d)}"` : ""}>
      <div class="card-head"><div class="avatar ${tone}">${icon(ic, !!tone)}</div>
        <div class="titles"><div class="name" role="button" tabindex="0" title="${esc(d.name)} — details">${esc(d.name)}</div><div class="src">${esc(srcLabel(d))}${d.online ? "" : " · Offline"}</div></div>
        <button class="card-edit" data-edit aria-label="Edit">${icon("edit")}</button>${headSwitch(d)}</div>
      ${fitCard(d, sc)}
      <div class="updated">${updated ? ago(updated) : ""}</div></article>`;
  }

  // Fill the fixed-height card in priority order; whatever doesn't fit goes behind "More".
  let panelFull = false;
  function fitCard(d, sc) {
    const phone = innerWidth < 840;
    const budget = (phone ? 272 : 276) - (phone ? 28 : 32) - (d.name.length > (phone ? 13 : 20) ? 60 : 44) - 16 - 10;
    const reading = readingBlock(d), status = statusBlock(d), meta = metaBlock(d), rows = controlRows(d);
    const spark = sc ? `<button class="spark-btn" data-hist="${esc(sc)}" aria-label="Show history">${sparkSvg(sparks.get(d.id + "|" + sc), sc)}</button>` : "";
    const items = [];
    if (reading) items.push(["r", reading, phone ? 40 : 52]);
    if (status) items.push(["s", status, status.includes('class="s2"') ? 52 : 32]);
    if (rows[0]) items.push(["c", rows[0], rowH(rows[0])]);
    if (meta) items.push(["m", meta, 24]);
    rows.slice(1).forEach(r => items.push(["c", r, rowH(r)]));
    if (spark) items.push(["p", spark, 40]);
    const fit = room => { let used = 0; const keep = new Set();
      items.forEach((it, i) => { if (used + it[2] + 10 <= room) { used += it[2] + 10; keep.add(i); } }); return keep; };
    let keep = fit(budget);
    if (keep.size < items.length) keep = fit(budget - 50);          // leave room for the More/Details button
    const hiddenCtl = items.some((it, i) => it[0] === "c" && !keep.has(i));
    // keep document order: reading, status, spark, meta, controls
    const order = { r: 0, s: 1, p: 2, m: 3, c: 4 };
    const kept = items.map((it, i) => [it, i]).filter(([, i]) => keep.has(i)).sort((a, b) => order[a[0][0]] - order[b[0][0]] || a[1] - b[1]).map(([it]) => it[1]);
    const anyHidden = hiddenCtl || keep.size < items.length;
    return kept.join("") + (anyHidden ? `<button class="more-ctl" data-open-device>${icon("tune")}${hiddenCtl ? "More controls" : "Details"}</button>` : "");
  }

  // Device panel: everything about one device, all controls, tappable history.
  let panelId = null;
  // Controls inside dialogs are plain buttons: only "Close" may submit (and close) the panel.
  document.addEventListener("submit", e => {
    if (e.target.closest("#devpanel") && e.submitter?.value !== "close") e.preventDefault();
  }, true);
  function openDevicePanel(id) { panelId = id; renderDevicePanel(); if (!$("#devpanel").open) $("#devpanel").showModal(); }
  function renderDevicePanel() {
    const d = devices.get(panelId); if (!d || !$("#devpanel").open && panelId == null) return;
    const [ic, tone] = deviceIcon(d);
    panelFull = true;
    const rows = controlRows(d).join("");
    panelFull = false;
    const nums = Object.entries(d.capabilities).filter(([, c]) => typeof c.value === "number" && !c.writable);
    $("#devpanel-body").innerHTML = `<div data-id="${esc(d.id)}" class="panel-inner">
      <div class="card-head"><div class="avatar ${tone}">${icon(ic, !!tone)}</div><div class="titles"><div class="name">${esc(d.name)}</div>
        <div class="src">${esc([d.room, srcLabel(d), d.model].filter(Boolean).join(" · "))}${d.online ? "" : " · Offline"}</div></div>${headSwitch(d)}</div>
      ${readingBlock(d)}${statusBlock(d)}
      ${nums.length ? `<div class="panel-grid">${nums.map(([k, c]) => `<button class="pstat" data-hist="${esc(k)}">${icon(META_ICON[k] || (k === "temperature" ? "thermostat" : k === "humidity" ? "water_drop" : k === "battery" ? "battery_full" : "sensors"))}<div><small>${esc(c.label || pretty(k))}</small><b>${k === "temperature" ? temp(c.value) + "°" + unit : fmt(c.value) + (c.unit ? " " + esc(c.unit) : "")}</b></div></button>`).join("")}</div>` : ""}
      ${rows ? `<div class="panel-ctl">${rows}</div>` : ""}
      <div class="panel-foot"><span class="muted small">Updated ${ago(lastUpdated(d)).toLowerCase()}</span><button class="text-btn" data-edit>${icon("edit")}Edit</button></div></div>`;
  }

  function quickAction(d) {
    const c = d.capabilities;
    const btn = (capName, value, ic, label, on) => `<button class="quick ${on ? "on" : ""}" data-cmd="${esc(capName)}" data-val="${esc(JSON.stringify(value))}" aria-label="${esc(label)}" title="${esc(label)}">${icon(ic, on)}</button>`;
    if (c.light?.writable || c.switch?.writable) { const k = c.light?.writable ? "light" : "switch", on = !!val(d, k);
      return `<button class="m3-switch sm ${on ? "on" : ""}" role="switch" aria-checked="${on}" data-cmd="${k}" data-val="${esc(JSON.stringify(!on))}" aria-label="Toggle ${esc(d.name)}"></button>`; }
    if (c.lock?.writable) { const locked = val(d, "lock") === "locked";
      return btn("lock", locked ? "unlock" : "lock", locked ? "lock" : "lock_open", locked ? "Unlock" : "Lock", !locked); }
    if (c.valve?.writable) { const open = val(d, "valve") === "open";
      return btn("valve", open ? "close" : "open", "valve", open ? "Close valve" : "Open valve", open); }
    return "";
  }
  function miniCard(d) {
    const [ic, tone] = deviceIcon(d);
    const st = statusOf(d);
    let big = "", small = "";
    if (has(d, "ph") && typeof val(d, "ph") === "number") { big = `${fmt(val(d, "ph"))}<small> pH</small>`; small = [has(d, "temperature") ? `${temp(val(d, "temperature"))}°` : "", st ? st[0] : ""].filter(Boolean).join(" · "); }
    else if (has(d, "temperature")) { big = `${temp(val(d, "temperature"))}<small>°</small>`; small = [val(d, "humidity") != null ? `${Math.round(val(d, "humidity"))}% RH` : "", st ? st[0] : ""].filter(Boolean).join(" · "); }
    else if (has(d, "moisture")) { big = `${val(d, "moisture") == null ? "–" : Math.round(val(d, "moisture"))}<small>%</small>`; small = "Soil moisture"; }
    else if (isUps(d)) { big = `${n(val(d, "battery"))}<small>%</small>`; small = `${st[0]} · ${runtimeText(val(d, "runtime"))}`; }
    else if (st) { big = `<span class="word">${esc(st[0])}</span>`; small = detailText(d); }
    const b = val(d, "battery");
    const lowBat = batteryLow(d) && !isUps(d) ? `<span class="mini-bat">${icon("battery_alert", true)}${b != null ? n(b) + "%" : "Low"}</span>` : "";
    const lit = isLight(d) && val(d, "light");
    return `<article class="card mini ${isAlarm(d) ? "alarm" : ""} ${d.online ? "" : "offline"} ${lit ? "lit" : ""} ${st && st[1] ? "tone-" + st[1] : ""}" data-id="${esc(d.id)}" ${lit ? `style="--lit:${glow(d)}"` : ""}>
      <div class="mini-head"><span class="mini-ic ${tone}">${icon(ic, !!tone)}</span><span class="name" title="${esc(d.name)}">${esc(d.name)}</span>
        <button class="card-edit" data-edit aria-label="Edit">${icon("edit")}</button>${quickAction(d)}</div>
      <div class="mini-big">${big}</div>
      <div class="mini-small">${esc(small) || "&nbsp;"}${lowBat}${d.online ? "" : `<span class="mini-bat">${icon("wifi_off")}Offline</span>`}</div>
    </article>`;
  }

  function typeMatch(d, t) {
    if (t === "battery") return val(d, "battery") != null || val(d, "battery_low") != null;
    if (t === "power") return isUps(d) || isPlug(d);
    if (t === "lights") return isLight(d);
    const g = GROUPS[t]; return !!g && g.some(k => has(d, k) && (t !== "controls" || d.capabilities[k].writable) && !(k === "co" && !coAlarm(d)));
  }
  const autoTabs = d => TAB_IDS.filter(t => typeMatch(d, t));
  const tabsOf = d => Array.isArray(d.categories) ? d.categories : autoTabs(d);
  function viewMatch(v, d) {
    if ((v.devices || []).includes(d.id)) return true;
    const rules = [v.rooms?.length && (() => v.rooms.includes(d.room || "Other")), v.types?.length && (() => v.types.some(t => TAB_IDS.includes(t) ? tabsOf(d).includes(t) : typeMatch(d, t))),
      v.sources?.length && (() => v.sources.includes(d.source))].filter(Boolean);
    return rules.length > 0 && rules.every(f => f());
  }
  function matches(d) {
    if (view === "all") return true;
    const cv = customView(); if (cv) return viewMatch(cv, d);
    if (TAB_IDS.includes(view)) return tabsOf(d).includes(view);
    const g = GROUPS[view];
    return g && g.some(k => has(d, k) && (view !== "controls" || d.capabilities[k].writable));
  }

  // ---------- table ----------
  const SORTERS = {
    name: d => d.name.toLowerCase(),
    room: d => `${String(roomRank(d.room || "Other")).padStart(7, "0")}|${(d.room || "~").toLowerCase()}|${String(devRank(d.room || "Other", d.id)).padStart(7, "0")}|${d.name.toLowerCase()}`,
    status: d => `${isAlarm(d) ? 0 : needsAttention(d) ? 1 : 2}|${(statusOf(d) || [readingText(d)])[0]}`,
    battery: d => val(d, "battery") ?? (val(d, "battery_low") === true ? 5 : val(d, "battery_low") === false ? 100 : 999),
    updated: d => -lastUpdated(d),
    source: d => srcLabel(d),
  };
  function tableView(list) {
    const s = SORTERS[sort.key] || SORTERS.room;
    list.sort((a, b) => { const x = s(a), y = s(b); return (x < y ? -1 : x > y ? 1 : 0) * sort.dir; });
    const th = (key, label, cls = "") => `<th class="${cls} ${sort.key === key ? "sorted" : ""}" ${key ? `data-sort="${key}"` : ""}>${label}${
      sort.key === key ? `<span class="arrow">${icon(sort.dir > 0 ? "arrow_upward" : "arrow_downward")}</span>` : ""}</th>`;
    const rows = list.map(d => {
      const [ic, tone] = deviceIcon(d);
      const st = statusOf(d), rd = readingText(d);
      const b = val(d, "battery"), bl = val(d, "battery_low");
      return `<tr data-id="${esc(d.id)}" class="${isAlarm(d) ? "alarm" : ""} ${d.online ? "" : "offline"}">
        <td class="c-name"><div class="dev"><span class="avatar ${tone}">${icon(ic, !!tone)}</span><div><b>${esc(d.name)}</b><small>${esc(srcLabel(d))}${d.online ? "" : " · Offline"}</small></div>
          <button class="card-edit" data-edit aria-label="Edit">${icon("edit")}</button></div></td>
        <td>${esc(d.room || "—")}</td>
        <td class="muted">${esc(srcLabel(d))}</td>
        <td><span class="val ${st ? st[1] : ""}">${rd ? esc(rd) : ""}${rd && st ? " · " : ""}${st ? esc(st[0]) : ""}</span></td>
        <td class="muted">${esc(detailText(d))}</td>
        <td>${b != null ? levelBar(b) : bl != null ? `<span class="val ${bl ? "bad" : ""}">${bl ? "Low" : "OK"}</span>` : `<span class="muted">—</span>`}</td>
        <td class="muted">${ago(lastUpdated(d))}</td>
        <td class="c-ctl">${controlsBlock(d, true)}</td></tr>`;
    }).join("");
    return `<div class="table-wrap"><table class="dtable"><thead><tr>${th("name", "Device", "c-name")}${th("room", "Room")}${th("source", "Integration")}${th("status", "Status")}${th("", "Details")}${th("battery", "Battery")}${th("updated", "Updated")}${th("", "Controls", "c-ctl")}</tr></thead><tbody>${rows}</tbody></table></div>`;
  }
  const levelBar = raw => { const b = Math.max(0, Math.min(100, Number(raw) || 0));
    return `<span class="level ${b <= 25 ? "low" : b <= 50 ? "mid" : ""}"><span class="bar"><i style="width:${Math.max(3, b)}%"></i></span><span>${Math.round(b)}%</span></span>`; };

  // ---------- summary tiles + chips ----------
  const tile = (cls, ic, value, label, fill) => `<div class="tile ${cls}"><div class="ti">${icon(ic, fill)}</div><div class="tv">${value}</div><div class="tl">${esc(label)}</div></div>`;
  function homeTiles(list) {
    const temps = list.filter(d => typeof val(d, "temperature") === "number");
    const outdoor = temps.find(d => isOutdoor(d) && has(d, "humidity")) || temps.find(isOutdoor);
    const indoor = temps.filter(d => !isOutdoor(d) && d !== outdoor && !isUps(d));
    const avg = indoor.length ? indoor.reduce((a, d) => a + val(d, "temperature"), 0) / indoor.length : null;
    const leaks = list.filter(d => val(d, "leak") === true);
    const openValves = list.filter(d => val(d, "valve") === "open");
    const open = list.filter(d => val(d, "contact") === "open" && !has(d, "lock"));
    const unlocked = list.filter(d => val(d, "lock") === "unlocked");
    const safety = list.filter(d => ["smoke", "co", "heat"].some(k => val(d, k) === true) || val(d, "alarm_panel") === "triggered");
    const ups = list.filter(isUps);
    const tiles = [];
    if (outdoor) tiles.push(tile("primary", "partly_cloudy_day", `${temp(val(outdoor, "temperature"))}<small>°${unit}</small>`,
      `Outside${has(outdoor, "humidity") ? ` · ${Math.round(val(outdoor, "humidity"))}% humidity` : ""}`, true));
    if (avg != null) tiles.push(tile("", "home", `${temp(avg)}<small>°${unit}</small>`, `Inside · avg of ${indoor.length}`));
    tiles.push(leaks.length ? tile("bad", "water_damage", `${leaks.length} leak${leaks.length > 1 ? "s" : ""}`, leaks.map(d => d.name).join(", "), true)
      : tile("good", "water_drop", "All dry", `${openValves.length} valve${openValves.length === 1 ? "" : "s"} open`, true));
    tiles.push(safety.length ? tile("bad", "local_fire_department", "Alarm", safety.map(d => d.name).join(", "), true)
      : (open.length + unlocked.length) ? tile("warn", unlocked.length ? "lock_open" : "door_open", `${open.length + unlocked.length} not secure`,
        [...open.map(d => d.name + " open"), ...unlocked.map(d => d.name)].slice(0, 2).join(" · "), true)
      : tile("good", "shield_lock", "Secure", "Doors closed · locks locked", true));
    const onBatt = ups.filter(d => val(d, "ups_status") !== "online");
    if (onBatt.length) tiles.push(tile(onBatt.some(d => val(d, "ups_status") === "low_battery") ? "bad" : "warn", "power_off",
      "On battery", `${onBatt[0].name} · ${runtimeText(val(onBatt[0], "runtime"))} left`, true));
    return tiles.join("");
  }
  function doorTiles(list) {
    const c = list.filter(d => has(d, "contact"));
    const open = c.filter(d => val(d, "contact") === "open"), closed = c.length - open.length;
    return tile(open.length ? "warn" : "good", open.length ? "door_open" : "door_front", `${open.length} open`,
      open.length ? open.map(d => d.name).slice(0, 3).join(", ") : "Everything is closed", true) +
      tile("", "sensor_door", `${closed} closed`, `${c.length} contact sensor${c.length === 1 ? "" : "s"}`) +
      tile("", "battery_alert", `${c.filter(batteryLow).length}`, "need new batteries");
  }
  function renderChips(list) {
    const chips = [];
    for (const d of list) {
      if (batteryLow(d) && view !== "battery") chips.push(["warn", "battery_alert", `${d.name} battery low`]);
      if (!d.online) chips.push(["warn", "wifi_off", `${d.name} offline`]);
    }
    $("#alerts").innerHTML = chips.map(([c, ic, t]) => `<span class="chip ${c}">${icon(ic, true)}${esc(t)}</span>`).join("");
  }

  // ---------- battery page ----------
  function batteryPage(list) {
    const bat = list.filter(d => val(d, "battery") != null || val(d, "battery_low") != null);
    const lvl = d => val(d, "battery") ?? (val(d, "battery_low") ? 10 : 100);
    bat.sort((a, b) => lvl(a) - lvl(b) || a.name.localeCompare(b.name));
    const groups = [["Replace soon", d => lvl(d) <= 25], ["Getting low", d => lvl(d) > 25 && lvl(d) <= 50], ["Good", d => lvl(d) > 50]];
    const withPct = bat.filter(d => val(d, "battery") != null);
    const avg = withPct.length ? Math.round(withPct.reduce((a, d) => a + val(d, "battery"), 0) / withPct.length) : null;
    const low = bat.filter(d => lvl(d) <= 25);
    $("#summary").innerHTML = tile(low.length ? "warn" : "good", low.length ? "battery_alert" : "battery_full", `${low.length}`,
      low.length ? "need new batteries" : "All batteries healthy", true) +
      tile("", "battery_5_bar", avg != null ? `${avg}<small>%</small>` : "–", "Average level") +
      tile("", "sensors", `${bat.length}`, "battery-powered devices");
    $("#alerts").innerHTML = "";
    const row = d => {
      const b = val(d, "battery");
      return `<div class="batt-row" data-id="${esc(d.id)}"><span class="avatar ${lvl(d) <= 25 ? "warn" : ""}">${icon(batteryIcon(lvl(d)), lvl(d) <= 25)}</span>
        <div><b>${esc(d.name)}</b><small>${esc([d.room, srcLabel(d), d.model].filter(Boolean).join(" · "))}</small></div>
        ${b != null ? levelBar(b) : `<span class="level ${val(d, "battery_low") ? "low" : ""}"><span class="bar"><i style="width:${val(d, "battery_low") ? 10 : 100}%"></i></span><span>${val(d, "battery_low") ? "Low" : "OK"}</span></span>`}
        <span class="when">${ago(lastUpdated(d))}</span></div>`;
    };
    $("#rooms").innerHTML = bat.length ? `<div class="batt-list">${groups.map(([label, fn]) => {
      const g = bat.filter(fn); return g.length ? `<div class="batt-group">${label} · ${g.length}</div>${g.map(row).join("")}` : ""; }).join("")}</div>`
      : `<p class="empty">No battery-powered devices yet.</p>`;
  }

  // ---------- lights page ----------
  function lightsPage(list) {
    const lights = list.filter(d => tabsOf(d).includes("lights") && !isGroup(d)), groups = list.filter(d => isGroup(d) && tabsOf(d).includes("lights"));
    const on = lights.filter(d => val(d, "light"));
    $("#summary").innerHTML = tile(on.length ? "primary" : "", "lightbulb", `${on.length}<small> on</small>`,
      on.length ? on.map(d => d.name).slice(0, 3).join(", ") + (on.length > 3 ? ` +${on.length - 3}` : "") : "Everything is off", !!on.length) +
      tile("", "home_iot_device", `${lights.length}`, `lights · ${groups.length} room${groups.length === 1 ? "" : "s"} & zones`);
    $("#alerts").innerHTML = lights.length ? `<div class="light-tools"><button class="tonal-btn" data-all-lights="false">${icon("power_settings_new")}All off</button>
      <button class="tonal-btn" data-all-lights="true">${icon("lightbulb")}All on</button></div>` : "";
    if (!lights.length && !groups.length) {
      $("#rooms").innerHTML = `<div class="empty-state">${icon("lightbulb")}<h3>No lights yet</h3><p>Pair your Philips Hue bridge under Settings → Philips Hue. Zigbee2MQTT bulbs show up automatically through MQTT.</p>
        <button class="tonal-btn" data-goto="settings">${icon("settings")}Open Settings</button></div>`; return;
    }
    const groupSec = groups.length ? `<section class="room" data-fixed><h2>Rooms &amp; zones</h2><div class="grid">${groups.sort((a, b) => a.name.localeCompare(b.name)).map(card).join("")}</div></section>` : "";
    $("#rooms").innerHTML = roomSections(lights, { extraFirst: groupSec });
    initSortables();
  }

  // ---------- devices page: every device, its tabs, room, visibility ----------
  let devQuery = "", devFilter = "all";
  let devSort = JSON.parse(load("devSort", '{"key":"room","dir":1}'));
  let devSel = { source: "", type: "", room: "" };
  const TYPE_NAMES = { ...Object.fromEntries(TABS.map(([t, l]) => [t, l])), battery: "Batteries", power: "UPS", none: "In no tab" };
  const primaryType = d => { const t = tabsOf(d); return t.length ? TYPE_NAMES[t[0]] : isUps(d) ? "UPS" : "No tab"; };
  const DEV_SORTS = {
    name: ["Name", d => d.name.toLowerCase(), null],
    room: ["Room", d => `${d.room ? 0 : 1}|${(d.room || "").toLowerCase()}|${d.name.toLowerCase()}`, d => d.room || "No room"],
    type: ["Type", d => `${primaryType(d)}|${d.name.toLowerCase()}`, primaryType],
    source: ["Integration", d => `${srcLabel(d)}|${d.name.toLowerCase()}`, srcLabel],
    updated: ["Last update", d => -lastUpdated(d), null],
    battery: ["Battery", d => val(d, "battery") ?? (val(d, "battery_low") === true ? 5 : 999), null],
  };
  function devicesPage() {
    $("#summary").innerHTML = "";
    $("#alerts").innerHTML = `<div class="devtools">
      <label class="search">${icon("search")}<input id="devsearch" type="search" placeholder="Search devices, rooms, brands" value="${esc(devQuery)}" autocomplete="off"><small id="devcount"></small></label>
      <div class="devfilters" id="devfilters">
        ${devSelect("source", "Integration", [...new Set([...devices.values()].map(srcLabel))].sort().map(x => [x, x]))}
        ${devSelect("type", "Type", Object.entries(TYPE_NAMES))}
        ${devSelect("room", "Room", [...new Set([...devices.values()].map(d => d.room || "No room"))].sort().map(x => [x, x]))}
        ${[["changed", "Tabs changed"], ["hidden", "Hidden"]].map(([k, l]) =>
          `<button type="button" class="fchip ${devFilter === k ? "on" : ""}" data-devfilter="${k}">${devFilter === k ? icon("check") : ""}${l}</button>`).join("")}
        <span class="devsort"><label class="selchip">${icon("swap_vert")}<span>Sort: ${(DEV_SORTS[devSort.key] || DEV_SORTS.room)[0]}</span><select id="devsort">${Object.entries(DEV_SORTS).map(([k, [l]]) =>
          `<option value="${k}" ${devSort.key === k ? "selected" : ""}>${l}</option>`).join("")}</select></label>
          <button type="button" class="icon-btn" id="devsortdir" title="${devSort.dir > 0 ? "Ascending" : "Descending"}">${icon(devSort.dir > 0 ? "arrow_upward" : "arrow_downward")}</button></span>
      </div></div>`;
    $("#rooms").innerHTML = `<div class="devlist"></div>`;
    renderDeviceList();
  }
  function devSelect(key, label, options) {
    const cur = devSel[key];
    return `<label class="selchip ${cur ? "on" : ""}">${cur ? icon("check") : ""}<span>${esc(cur ? (options.find(o => o[0] === cur) || [0, cur])[1] : label)}</span>
      <select data-devsel="${key}" aria-label="${label}"><option value="">All ${label === "Type" ? "types" : label === "Room" ? "rooms" : "integrations"}</option>${options.map(([v, l]) =>
        `<option value="${esc(v)}" ${cur === v ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></label>`;
  }
  function renderDeviceList() {
    const box = $(".devlist"); if (!box) return;
    const q = devQuery.trim().toLowerCase();
    const all = [...devices.values()].filter(d => !q || `${d.name} ${d.room} ${srcLabel(d)} ${d.model}`.toLowerCase().includes(q))
      .filter(d => devFilter === "changed" ? Array.isArray(d.categories) : devFilter === "hidden" ? d.hidden : true)
      .filter(d => !devSel.source || srcLabel(d) === devSel.source)
      .filter(d => !devSel.room || (d.room || "No room") === devSel.room)
      .filter(d => !devSel.type || (devSel.type === "none" ? !tabsOf(d).length : devSel.type === "battery" ? typeMatch(d, "battery")
        : devSel.type === "power" ? isUps(d) || isPlug(d) : tabsOf(d).includes(devSel.type)));
    const [, keyFn, groupFn] = DEV_SORTS[devSort.key] || DEV_SORTS.room;
    all.sort((a, b) => { const x = keyFn(a), y = keyFn(b); return (x < y ? -1 : x > y ? 1 : 0) * devSort.dir; });
    const counts = { all: devices.size, changed: [...devices.values()].filter(d => Array.isArray(d.categories)).length,
      hidden: [...devices.values()].filter(d => d.hidden).length };
    box.innerHTML = `<p class="devnote">${icon("info")}<span>Tabs are picked automatically from what each device reports. Tap a tab to add or remove a device; changed devices are marked <b>Custom</b> and can be reset to automatic. Every device always appears on Home unless it's hidden. ${counts.changed ? `<b>${counts.changed}</b> changed.` : ""}${counts.hidden ? ` <b>${counts.hidden}</b> hidden.` : ""}</span></p>
      <div class="devhead"><span>Device</span><span>Room</span><span>Shows in tabs</span><span>Visible</span></div>` +
      (all.map((d, i) => {
        const [ic, tone] = deviceIcon(d), tabs = tabsOf(d), custom = Array.isArray(d.categories), auto = autoTabs(d);
        const g = groupFn ? groupFn(d) : null, prevG = i && groupFn ? groupFn(all[i - 1]) : null;
        const header = g != null && g !== prevG ? `<div class="devgroup">${esc(g)}<small>${all.filter(x => groupFn(x) === g).length}</small></div>` : "";
        return header + `<div class="devrow ${d.hidden ? "is-hidden" : ""}" data-id="${esc(d.id)}">
          <div class="dcell dname"><span class="avatar ${tone}">${icon(ic, !!tone)}</span><div><b>${esc(d.name)}</b><small>${esc([srcLabel(d), d.model].filter(Boolean).join(" · "))}</small></div>
            <button class="icon-btn" data-edit title="Rename, move or pin">${icon("edit")}</button></div>
          <div class="dcell droom ${d.room ? "" : "muted"}">${esc(d.room || "No room")}</div>
          <div class="dcell dtabs">${TABS.map(([t, l, ti]) => `<button type="button" class="tabchip ${tabs.includes(t) ? "on" : ""} ${!custom ? "auto" : auto.includes(t) !== tabs.includes(t) ? "changed" : ""}"
              data-tab="${t}" title="${tabs.includes(t) ? "Remove from" : "Add to"} ${l}">${icon(ti, tabs.includes(t))}${l}</button>`).join("")}
            ${custom ? `<button type="button" class="text-btn reset-tabs" data-reset-tabs title="Back to automatic: ${esc(auto.map(t => TABS.find(x => x[0] === t)[1]).join(", ") || "no tabs")}">${icon("restart_alt")}Custom · reset</button>`
              : `<span class="autotag">Auto</span>`}</div>
          <div class="dcell dvis"><button class="m3-switch sm ${d.hidden ? "" : "on"}" role="switch" aria-checked="${!d.hidden}" data-visible aria-label="Show ${esc(d.name)}"></button></div>
        </div>`; }).join("") || `<p class="empty">No devices match these filters.</p>`);
    const n = $("#devcount"); if (n) n.textContent = `${all.length} of ${devices.size}`;
  }
  async function patchDevice(id, body, msg) {
    try { const d = await api(`/api/devices/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(body) });
      devices.set(id, d); renderDeviceList(); renderBadges(); if (msg) snack(msg); }
    catch (err) { snack(err.message); }
  }

  // ---------- air quality page ----------
  const AQI_BANDS = [[50, "Good", "#00b050"], [100, "Moderate", "#e6b800"], [150, "Unhealthy for sensitive groups", "#ff7e00"],
    [200, "Unhealthy", "#e53935"], [300, "Very unhealthy", "#8f3f97"], [1e9, "Hazardous", "#7e0023"]];
  const aqiBand = a => AQI_BANDS.find(b => (Number.isFinite(Number(a)) ? Number(a) : 0) <= b[0]) || AQI_BANDS[AQI_BANDS.length - 1];
  const aqiColor = a => a == null || !isFinite(a) ? "inherit" : aqiBand(Number(a))[2];
  // [capability, label, unit, good-max, moderate-max, higherIsBetter]
  const AIR_METRICS = [["aqi", "AQI", "", 50, 100], ["air_score", "Air score", "/100", 80, 60, true], ["pm25", "PM2.5", "µg/m³", 12, 35.4],
    ["pm10", "PM10", "µg/m³", 54, 154], ["co2", "CO₂", "ppm", 800, 1200], ["voc", "VOC", "ppb", 333, 1000], ["ozone", "Ozone", "µg/m³", 100, 160],
    ["no2", "NO₂", "µg/m³", 40, 100], ["so2", "SO₂", "µg/m³", 40, 125], ["co", "CO", "µg/m³", 4000, 10000], ["co_ppm", "CO", "ppm", 9, 35],
    ["dust", "Dust", "µg/m³", 50, 150], ["uv", "UV", "", 2, 7], ["humidity", "Humidity", "%", null, null], ["filter_life", "Filter", "%", null, null]];
  const AIR_KEYS = new Set(["aqi", "air_score", "pm25", "pm10", "co2", "voc", "ozone", "no2", "so2", "dust", "co_ppm"]);
  function airTone(m, v) {
    const [k, , , good, mod, hib] = m;
    if (v == null || typeof v !== "number") return "";
    if (k === "humidity") return v < 25 || v > 65 ? "mod" : "good";
    if (k === "filter_life") return v < 10 ? "bad" : v < 25 ? "mod" : "good";
    if (good == null) return "";
    return hib ? (v >= good ? "good" : v >= mod ? "mod" : "bad") : (v <= good ? "good" : v <= mod ? "mod" : "bad");
  }
  function airPage(list) {
    const stations = list.filter(d => kindOf(d) === "aqi_station");
    const airDevs = list.filter(d => kindOf(d) !== "aqi_station" && Object.keys(d.capabilities).some(k => AIR_KEYS.has(k) && typeof val(d, k) === "number"));
    $("#summary").innerHTML = ""; $("#alerts").innerHTML = "";
    const tile = (k, label, v, unit) => v == null ? "" : `<button class="aqtile" data-id="${esc(stations[0]?.id || "")}" data-hist="${esc(k)}"><small>${esc(label)}</small><b>${fmt(v)}</b><span>${esc(unit)}</span></button>`;
    const outdoor = stations.map(d => {
      const a = val(d, "aqi"), [, cat, col] = aqiBand(a), at = d.attributes || {};
      const fc = (at.forecast || []).slice(0, 25), maxA = Math.max(100, ...fc.map(x => x.aqi || 0));
      const bars = fc.length ? `<div class="aqfc">${fc.map(x => `<i title="${esc(new Date(Number(x.t) * 1000).toLocaleString([], { weekday: "short", hour: "numeric", ...timeOpts() }))}: AQI ${Number(x.aqi) || 0}" style="height:${Math.max(4, (Number(x.aqi) || 0) / maxA * 100)}%;background:${aqiColor(Number(x.aqi))}"></i>`).join("")}</div>
        <div class="aqfc-axis"><span>Now</span><span>+12 h</span><span>+24 h</span></div>` : "";
      return `<section class="aqhero" data-id="${esc(d.id)}" style="--aq:${col}">
        <div class="aqmain"><div class="aqring"><b>${n(a)}</b><small>AQI</small></div>
          <div class="aqinfo"><h3>${esc(cat)}</h3><p>${esc([at.place, at.dominant ? `mostly ${at.dominant}` : "", at.provider].filter(Boolean).join(" · "))}</p>
          <small class="muted">${at.observed ? `Observed ${ago(at.observed).toLowerCase()}` : ""} · updates hourly</small></div></div>
        <div class="aqtiles">${[["pm25", "PM2.5", "µg/m³"], ["pm10", "PM10", "µg/m³"], ["ozone", "Ozone", "µg/m³"], ["no2", "NO₂", "µg/m³"], ["so2", "SO₂", "µg/m³"],
          ["co", "CO", "µg/m³"], ["dust", "Dust", "µg/m³"], ["uv", "UV index", ""], ["pm25_aqi", "PM2.5 AQI", ""], ["pm10_aqi", "PM10 AQI", ""], ["ozone_aqi", "Ozone AQI", ""]]
          .map(([k, l, u]) => val(d, k) == null ? "" : `<button class="aqtile" data-hist="${esc(k)}"><small>${l}</small><b>${fmt(val(d, k))}</b><span>${u}</span></button>`).join("")}</div>
        ${bars ? `<div class="aqfc-wrap"><small>Next 24 hours</small>${bars}</div>` : ""}
      </section>`;
    }).join("") || `<section class="aqhero empty"><div class="empty-state">${icon("aq")}<h3>Add your outdoor air quality</h3>
      <p>Enter your ZIP code and Haven checks the AQI every hour (Open-Meteo, no key needed; EPA AirNow optional).</p>
      <button class="tonal-btn" data-goto-setting="airquality">${icon("settings")}Set up</button></div></section>`;
    const cols = AIR_METRICS.filter(m => airDevs.some(d => typeof val(d, m[0]) === "number"));
    const table = airDevs.length ? `<div class="table-wrap"><table class="dtable airtable"><thead><tr><th class="c-name">Device</th><th>Room</th>${cols.map(m => `<th>${m[1]}</th>`).join("")}<th>Updated</th></tr></thead><tbody>${
      airDevs.sort((a, b) => (a.room || "~").localeCompare(b.room || "~") || a.name.localeCompare(b.name)).map(d => {
        const [ic, tone] = deviceIcon(d);
        return `<tr data-id="${esc(d.id)}"><td class="c-name"><div class="dev"><span class="avatar ${tone}">${icon(ic, !!tone)}</span><div><b>${esc(d.name)}</b><small>${esc(srcLabel(d))}</small></div></div></td>
          <td>${esc(d.room || "—")}</td>${cols.map(m => { const v = val(d, m[0]); return `<td>${typeof v === "number" ? `<button class="aqcell ${airTone(m, v)}" data-hist="${m[0]}">${m[0] === "temperature" ? temp(v) : fmt(v)}<small>${m[2]}</small></button>` : `<span class="muted">—</span>`}</td>`; }).join("")}
          <td class="muted">${ago(lastUpdated(d))}</td></tr>`; }).join("")}</tbody></table></div>` : `<p class="empty">No indoor air sensors yet. Awair monitors, purifiers (Dyson, Levoit) and Ecowitt PM2.5/CO₂ sensors appear here.</p>`;
    const purifiers = list.filter(d => kindOf(d) === "air_purifier");
    $("#rooms").innerHTML = `<div class="airpage">${outdoor}
      <h2 class="sec-title">Indoor air <small>${airDevs.length} device${airDevs.length === 1 ? "" : "s"} · tap a value for its history</small></h2>${table}
      <div class="aqlegend"><span><i class="good"></i>Good</span><span><i class="mod"></i>Moderate</span><span><i class="bad"></i>Poor</span></div>
      ${purifiers.length ? `<h2 class="sec-title">Purifiers</h2><div class="grid">${purifiers.map(card).join("")}</div>` : ""}</div>`;
  }

  // ---------- power page ----------
  let nutInfo = null, nutHist = {}, powerRange = load("powerRange", "24h");
  async function loadPower() {
    try { nutInfo = await api("/api/nut/server"); } catch { nutInfo = null; }
    await Promise.all(visible().filter(isUps).map(async d => {
      for (const k of ["battery", "load"]) try { nutHist[d.id + "|" + k] = await api(`/api/devices/${encodeURIComponent(d.id)}/history/${k}?range=${powerRange}`); } catch {}
    }));
    if (view === "power") scheduleQuiet();
  }
  function gauge(pct, tone) {
    const r = 54, c = 2 * Math.PI * r, p = Math.max(0, Math.min(100, pct ?? 0));
    return `<svg class="gauge ${tone}" viewBox="0 0 128 128"><circle cx="64" cy="64" r="${r}" class="track"/>
      <circle cx="64" cy="64" r="${r}" class="fill" stroke-dasharray="${c}" stroke-dashoffset="${c * (1 - p / 100)}"/></svg>`;
  }
  function plugPanel(plugs) {
    if (!plugs.length) return "";
    const total = plugs.reduce((a, d) => a + (Number(val(d, "power")) || 0), 0);
    const today = plugs.reduce((a, d) => a + (Number(val(d, "energy_today")) || 0), 0);
    const metered = plugs.some(d => has(d, "power"));
    const rows = plugs.slice().sort((a, b) => (Number(val(b, "power")) || 0) - (Number(val(a, "power")) || 0) || a.name.localeCompare(b.name)).map(d => {
      const on = !!val(d, "switch");
      return `<tr data-id="${esc(d.id)}" class="${d.online ? "" : "offline"}">
        <td class="c-name"><div class="dev"><span class="avatar ${on ? "active" : ""}">${icon("outlet", on)}</span><div><b>${esc(d.name)}</b><small>${esc([d.room, d.model].filter(Boolean).join(" · "))}${d.online ? "" : " · Offline"}</small></div></div></td>
        <td class="num">${has(d, "power") ? `${n(val(d, "power"))} W` : "–"}</td>
        <td class="num">${has(d, "energy_today") ? `${n(val(d, "energy_today"))} kWh` : "–"}</td>
        <td class="num">${has(d, "energy_month") ? `${n(val(d, "energy_month"))} kWh` : "–"}</td>
        <td class="c-act"><button class="m3-switch ${on ? "on" : ""}" role="switch" aria-checked="${on}" data-cmd="switch" data-val="${esc(JSON.stringify(!on))}" aria-label="Toggle ${esc(d.name)}" ${d.online ? "" : "disabled"}></button></td></tr>`;
    }).join("");
    return `<section class="ups-panel plugs" data-id="plugs">
      <div class="ups-head"><div><h3>${icon("outlet")} Smart plugs</h3><small>${plugs.filter(d => val(d, "switch")).length} of ${plugs.length} on</small></div>
        ${metered ? `<span class="pill">${icon("bolt", true)}${n(Math.round(total))} W now · ${n(Math.round(today * 100) / 100)} kWh today</span>` : ""}</div>
      <div class="table-wrap"><table class="plug-table"><thead><tr><th>Plug</th><th class="num">Using</th><th class="num">Today</th><th class="num">This month</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
    </section>`;
  }
  function powerPage(list) {
    const ups = list.filter(isUps), plugs = list.filter(isPlug);
    $("#summary").innerHTML = "";
    $("#alerts").innerHTML = ups.length ? `<div class="segmented ranges" id="power-ranges">${histRanges.filter(r => r !== "1h").map(r =>
      `<button type="button" data-prange="${r}" class="${r === powerRange ? "on" : ""}">${r}</button>`).join("")}</div>` : "";
    const stat = (ic, label, value) => `<div class="pstat">${icon(ic)}<div><small>${esc(label)}</small><b>${esc(value)}</b></div></div>`;
    const panels = ups.map(d => {
      const v = d.attributes?.nut_vars || {}, s = val(d, "ups_status");
      const tone = s === "low_battery" ? "bad" : s === "on_battery" ? "warn" : "";
      const [st] = statusOf(d);
      const hb = nutHist[d.id + "|battery"], hl = nutHist[d.id + "|load"];
      return `<section class="ups-panel ${tone}" data-id="${esc(d.id)}">
        <div class="ups-head"><div><h3>${esc(d.name)}</h3><small>${esc([d.model, d.room, d.attributes?.nut_host].filter(Boolean).join(" · "))}</small></div>
          <span class="pill ${tone}">${icon(s === "online" ? "power" : "power_off", true)}${esc(st)}${val(d, "charging") ? " · charging" : ""}</span></div>
        <div class="ups-body">
          <div class="gauge-wrap">${gauge(val(d, "battery"), tone)}<div class="gauge-label"><b>${n(val(d, "battery"))}<small>%</small></b><span>${runtimeText(val(d, "runtime"))}</span></div></div>
          <div class="pstats">
            ${stat("speed", "Load", `${n(val(d, "load"))}%`)}
            ${stat("bolt", "Power", val(d, "power") != null ? `${val(d, "power")} W` : "–")}
            ${stat("electrical_services", "Input", val(d, "input_voltage") != null ? `${val(d, "input_voltage")} V` : "–")}
            ${stat("power", "Output", val(d, "output_voltage") != null ? `${val(d, "output_voltage")} V` : "–")}
            ${stat("battery_full", "Battery", v["battery.voltage"] ? `${v["battery.voltage"]} V` : v["battery.type"] || "–")}
            ${stat("timer", "Runtime", runtimeText(val(d, "runtime")))}
          </div>
        </div>
        ${hb || hl ? `<div class="ups-charts"><button class="spark-btn" data-hist="battery"><small>Charge · ${powerRange}</small>${sparkSvg(hb, "battery", 56)}</button><button class="spark-btn" data-hist="load"><small>Load · ${powerRange}</small>${sparkSvg(hl, "load", 56)}</button></div>` : ""}
        ${val(d, "replace_battery") ? `<div class="chip bad">${icon("battery_alert", true)}UPS reports the battery needs replacing</div>` : ""}
        <details class="raw"><summary>All UPS values (${Object.keys(v).length})</summary>
          <table>${Object.entries(v).sort().map(([k, x]) => `<tr><td>${esc(k)}</td><td>${esc(x)}</td></tr>`).join("")}</table></details>
      </section>`;
    }).join("");
    const host = location.hostname;
    const srv = nutInfo && nutInfo.enabled ? `<section class="ups-panel nut-server">
        <div class="ups-head"><div><h3>${icon("dns")} NUT server</h3><small>Other machines can use Haven as their UPS server</small></div>
          <span class="pill">${icon("check_circle", true)}Listening on ${nutInfo.port}</span></div>
        <div class="nut-grid">
          <div><small>Server address</small><b>${esc(host)}:${nutInfo.port}</b></div>
          <div><small>UPS names</small><b>${nutInfo.names.map(esc).join(", ") || "–"}</b></div>
          <div><small>Connected clients</small><b>${nutInfo.clients.length ? nutInfo.clients.map(c => esc(c.address.split(":")[0]) + (c.ups ? ` (${esc(c.ups)})` : "")).join(", ") : "None right now"}</b></div>
        </div>
        <p class="hint">${icon("info")}<span><b>Synology:</b> Control Panel → Hardware &amp; Power → UPS → Enable UPS support → Network UPS type: <i>Synology UPS server</i> → IP <b>${esc(host)}</b>.
          Haven answers as UPS <b>${esc(nutInfo.alias)}</b> with the username/password set in Settings → UPS (NUT).
          <br><b>Others</b> (Proxmox, TrueNAS, Unraid, upsmon): <code>${esc(nutInfo.alias)}@${esc(host)}:${nutInfo.port}</code></span></p>
      </section>` : "";
    $("#rooms").innerHTML = ups.length || srv || plugs.length ? `<div class="power-wrap">${panels}${plugPanel(plugs)}${srv}</div>`
      : `<div class="empty-state">${icon("power")}<h3>No UPS or smart plugs yet</h3><p>Add your UniFi UPS under Settings → UPS (NUT), or Tapo plugs under Settings → TP-Link Tapo plugs.</p>
         <button class="tonal-btn" data-goto="settings">${icon("settings")}Open Settings</button></div>`;
  }

  // ---------- settings page ----------
  let settingsData = null;
  async function loadSettings() {
    try { settingsData = await api("/api/settings"); } catch (e) { settingsData = { error: e.message }; }
    if (view === "settings") renderSettings();
  }
  function fieldHtml(sec, f) {
    const v = sec.values[f.key];
    const id = `${sec.id}--${f.key}`;
    const help = f.help ? `<small class="fhelp">${esc(f.help)}</small>` : "";
    if (f.type === "bool") return `<label class="check field"><input type="checkbox" data-key="${f.key}" ${v ? "checked" : ""}><span>${esc(f.label)}</span></label>${help}`;
    if (f.type === "select") return `<label class="textfield"><select data-key="${f.key}" id="${id}">${f.options.map(o => `<option ${o === v ? "selected" : ""}>${o}</option>`).join("")}</select><span>${esc(f.label)}</span></label>${help}`;
    if (f.type === "servers") return `<div class="servers" data-key="${f.key}"><div class="field-label">${esc(f.label)}</div>${(v || []).map(serverRow).join("")}
      <button type="button" class="text-btn" data-add-server>${icon("add")} Add UPS</button></div>`;
    const type = f.type === "password" ? "password" : f.type === "number" ? "number" : "text";
    const value = f.type === "list" ? (v || []).join(", ") : v ?? "";
    return `<label class="textfield"><input type="${type}" data-key="${f.key}" id="${id}" value="${esc(value)}" placeholder=" " autocomplete="${type === "password" ? "new-password" : "off"}"><span>${esc(f.label)}</span></label>${help}`;
  }
  const serverRow = (s = {}) => `<div class="server-row">
    ${[["name", "Name", "text"], ["host", "IP address", "text"], ["port", "Port", "number"], ["ups", "UPS name (optional)", "text"],
       ["username", "Username (optional)", "text"], ["password", "Password (optional)", "password"], ["room", "Room", "text"]].map(([k, l, t]) =>
      `<label class="textfield"><input type="${t}" data-skey="${k}" value="${esc(s[k] ?? (k === "port" ? 3493 : ""))}" placeholder=" " autocomplete="off"><span>${l}</span></label>`).join("")}
    <button type="button" class="icon-btn" data-del-server aria-label="Remove">${icon("delete")}</button></div>`;
  // Settings: a calm list of rows; each opens its own detail page.
  let settingsSub = null;   // null = list, "appearance", "security" or a section id
  const ADVANCED = new Set(["poll_seconds", "discovery", "discovery_prefix", "server.primary_alias", "server.allow", "server.allow_fsd", "passkeys",
    "sample_seconds", "prometheus_url", "metrics_enabled", "metrics_token", "region", "session_token", "access_token"]);
  const DASH_GROUP = new Set(["history", "demo", "time"]);
  function humanErr(st) {
    if (/errno 98|address already in use/i.test(st)) return "Port already in use";
    if (/name resolution|nodename|getaddrinfo|name or service not known/i.test(st)) return "Can't find that host";
    if (/refused/i.test(st)) return "Connection refused – is the device on?";
    if (/timed? ?out/i.test(st)) return "Timed out – check the address";
    if (/401|403|denied|unauthori[sz]ed|invalid (password|credentials)/i.test(st)) return "Sign-in rejected – check the password";
    const m = st.replace(/^error:\s*/i, "").replace(/\[errno -?\d+\]\s*/i, "");
    return "Problem: " + (m.length > 80 ? m.slice(0, 77) + "…" : m);
  }
  function secStatus(sec) {
    const st = sec.status || "";
    if (!sec.enabled) return ["Off", "off"];
    if (/needs|sign-in|pairing/i.test(st)) return [st.replace(/\s*\(.*\)$/, "").replace(/^needs/, "Needs"), "warn"];
    if (/error|refused|failed|denied|timed out/i.test(st)) return [humanErr(st), "bad"];
    if (/^ok/.test(st)) return [sec.id === "history" ? `On · ${st.split(":")[1]?.trim() || "saving"}` : sec.id === "demo" ? "On" : "Connected", "good"];
    return [st ? st[0].toUpperCase() + st.slice(1) : "Starting…", "info"];
  }
  function appearanceSummary() {
    return [{ light: "Light", dark: "Dark", auto: "Auto" }[load("mode", "auto")], root.dataset.seed[0].toUpperCase() + root.dataset.seed.slice(1),
      `°${unit}`, cardStyle === "minimal" ? "Minimal cards" : "Standard cards", mode === "table" ? "Table" : "Cards"].join(" · ");
  }
  function renderSettings() {
    $("#summary").innerHTML = ""; $("#alerts").innerHTML = "";
    if (!settingsData) { $("#rooms").innerHTML = `<p class="empty">Loading…</p>`; return; }
    if (settingsData.error) { $("#rooms").innerHTML = `<p class="empty">${esc(settingsData.error)}</p>`; return; }
    if (settingsSub) return renderSettingsDetail();
    const row = (key, ic, title, sub, tone = "", extra = "") => `<button type="button" class="lrow" data-open-setting="${key}">
        <span class="avatar ${tone === "off" ? "dim" : ""}">${icon(ic)}</span>
        <span class="ltext"><b>${esc(title)}</b><small class="lstat ${tone}">${tone && tone !== "off" ? "<i></i>" : ""}<span>${esc(sub)}</span></small></span>${extra}${icon("chevron_right")}</button>`;
    const accounts = settingsData.sections.filter(s => !DASH_GROUP.has(s.id));
    const on = accounts.filter(s => s.enabled), off = accounts.filter(s => !s.enabled);
    const issues = on.filter(s => ["bad", "warn"].includes(secStatus(s)[1])).length;
    const group = (title, note, rows) => rows ? `<section class="lgroup"><div class="lgroup-head"><h3>${title}</h3>${note ? `<small>${note}</small>` : ""}</div><div class="lcard">${rows}</div></section>` : "";
    const hist = settingsData.sections.find(s => s.id === "history"), demo = settingsData.sections.find(s => s.id === "demo");
    $("#rooms").innerHTML = `<div class="settings-wrap list">
      ${settingsData.auth ? "" : `<button type="button" class="lnotice" data-open-setting="security">${icon("key")}<span><b>No password set.</b> Anyone on your network can change your accounts.</span>${icon("chevron_right")}</button>`}
      ${group("Your accounts", `${on.length} on${issues ? ` · ${issues} need${issues === 1 ? "s" : ""} attention` : ""}`, on.map(s => { const [t, tone] = secStatus(s); return row(s.id, s.icon, s.label, t, tone); }).join(""))}
      ${group("Add an account", "", off.map(s => row(s.id, s.icon, s.label, "Off", "off")).join(""))}
      ${group("Dashboard", "", row("appearance", "palette", "Appearance", appearanceSummary()) +
        (() => { const t = settingsData.sections.find(s => s.id === "time"); return t ? row("time", t.icon, t.label, ...secStatus(t)) : ""; })() +
        (hist ? row("history", hist.icon, hist.label, ...secStatus(hist)) : "") + (demo ? row("demo", demo.icon, demo.label, ...secStatus(demo)) : ""))}
      ${group("Security", "", row("security", "key", "Dashboard password", settingsData.auth ? "On" : "Not set", settingsData.auth ? "good" : "warn"))}
      <p class="muted small lfoot">Accounts are saved on the server in <code>${esc(settingsData.file)}</code>. Passwords are never sent back to the browser.</p>
    </div>`;
  }
  // ---- Ecowitt gateways: only trusted gateways are listened to ----
  const ecoGw = { data: null, at: 0, loading: false };
  async function loadEcoGateways() {
    ecoGw.loading = true;
    try { ecoGw.data = await api("/api/ecowitt/gateways"); } catch { ecoGw.data = null; }
    ecoGw.loading = false; ecoGw.at = Date.now();
    if (view === "settings" && settingsSub === "ecowitt") renderSettings();
  }
  function ecoGatewaysHtml(g) {
    const ago = t => t ? `${Math.max(0, Math.round((Date.now() / 1000 - t) / 60))} min ago` : "";
    const row = (x, pending) => `<div class="gw-row ${pending ? "pending" : ""}"><span class="avatar ${pending ? "warn" : ""}">${icon(pending ? "help" : "check_circle", !pending)}</span>
      <div class="gw-txt"><b>${esc(x.model || "Ecowitt gateway")} <small>PASSKEY ${esc(x.passkey)}</small></b>
      <small>${pending ? `Sending from ${esc(x.ip || "?")} · ${esc(ago(x.last_seen))} · ${esc(x.count)} report${x.count === 1 ? "" : "s"}` : x.from_config ? "Trusted in config.yaml" : "Trusted"}</small></div>
      ${pending ? `<button type="button" class="tonal-btn" data-eco-approve="${esc(x.id)}">${icon("check")}Trust</button>` : ""}
      ${x.from_config ? "" : `<button type="button" class="icon-btn" data-eco-forget="${esc(x.id)}" aria-label="${pending ? "Ignore" : "Stop trusting"} this gateway">${icon("delete")}</button>`}</div>`;
    return `<div class="gw-list"><h3>Gateways</h3>
      ${g.pending.length ? `<p class="hint warn">${icon("info")}<span>A gateway Haven doesn't know yet is sending data. Its readings are ignored until you trust it. Trust it only if the address matches your gateway's IP (shown in the WS View Plus app).</span></p>` : ""}
      ${g.pending.map(x => row(x, true)).join("")}${g.approved.map(x => row(x, false)).join("")}
      ${!g.pending.length && !g.approved.length ? `<p class="muted small">No gateway has sent data yet.</p>` : ""}</div>`;
  }

  function renderSettingsDetail() {
    const back = `<button type="button" class="backbtn" data-settings-back>${icon("arrow_back")}Settings</button>`;
    let body = "";
    if (settingsSub === "appearance") {
      const pref = load("mode", "auto");
      const seg = (id, items, cur, attr) => `<div class="segmented" id="${id}">${items.map(([v, l, ic]) => `<button type="button" data-${attr}="${v}" class="${cur === v ? "on" : ""}">${ic ? icon(ic) : ""}${l}</button>`).join("")}</div>`;
      body = `<div class="dhead"><span class="avatar big">${icon("palette")}</span><div><h2>Appearance</h2><small>Saved on this device only</small></div></div>
        <section class="set-card" id="appearance">
          <div class="frow"><span>Theme</span>${seg("mode-seg", [["light", "Light", "light_mode"], ["dark", "Dark", "dark_mode"], ["auto", "Auto", "brightness_auto"]], pref, "mode")}</div>
          <div class="frow"><span>Color</span><div class="swatches" id="swatches">${Object.entries(SEEDS).map(([n, c]) =>
            `<button type="button" class="swatch ${root.dataset.seed === n ? "on" : ""}" data-seed="${n}" style="background:${c}" title="${n}">${root.dataset.seed === n ? icon("check_circle", true) : ""}</button>`).join("")}</div></div>
          <div class="frow"><span>Temperature</span>${seg("unit-seg", [["F", "°F"], ["C", "°C"]], unit, "unit")}</div>
          <div class="frow"><span>Card style</span>${seg("style-seg", [["standard", "Standard", "dashboard"], ["minimal", "Minimal", "view_agenda"]], cardStyle, "cstyle")}</div>
          <div class="frow"><span>Layout</span>${seg("layout-seg", [["cards", "Cards", "grid_view"], ["table", "Table", "table_rows"]], mode, "layout")}</div>
        </section>`;
    } else if (settingsSub === "security") {
      body = `<div class="dhead"><span class="avatar big">${icon("key")}</span><div><h2>Dashboard password</h2><small class="lstat ${settingsData.auth ? "good" : "warn"}"><i></i>${settingsData.auth ? "On" : "Not set"}</small></div></div>
        <section class="set-card prose">
          <p>${settingsData.auth ? "A password is required to use this dashboard, its controls and these settings." : "Without a password, anyone on your network can open this page and control your devices. Accounts, sign-ins and automations can't be changed until a password is set."}</p>
          <p>Set it in <code>docker-compose.yml</code>:</p><pre>environment:\n  DASH_TOKEN: "your-password"</pre>
          <p>Then restart Haven (Synology: Container Manager → Project → haven → Action → Build). Each browser asks for it once.</p>
        </section>`;
    } else {
      const sec = settingsData.sections.find(x => x.id === settingsSub);
      if (!sec) { settingsSub = null; return renderSettings(); }
      const [st, tone] = secStatus(sec);
      const basic = sec.fields.filter(f => !ADVANCED.has(f.key)), adv = sec.fields.filter(f => ADVANCED.has(f.key));
      if (sec.id === "ecowitt" && sec.enabled && !ecoGw.loading && Date.now() - ecoGw.at > 5000) loadEcoGateways();
      const push = sec.push_info ? `<p class="hint">${icon("info")}<span>On the gateway: Server <b>${esc(location.hostname)}</b> · Port <b>${esc(location.protocol === "https:" ? (sec.push_info.http_port || 1701) : (location.port || 80))}</b> · Path <b>${esc(ecoGw.data?.push_path || "/data/report")}</b> · Protocol <b>Ecowitt</b>
        ${ecoGw.data?.push_path ? "<br>Using this exact path (it's a secret) trusts the gateway automatically. With plain /data/report you approve it below." : ""}</span></p>` : "";
      const gws = sec.id === "ecowitt" && sec.enabled && ecoGw.data ? ecoGatewaysHtml(ecoGw.data) : "";
      const loginBtn = sec.login ? `<button type="button" class="tonal-btn" data-login="${sec.login === true ? sec.id : sec.login}">${icon("login")}${sec.login === "hue" ? "Pair bridge" : "Sign in"}</button>` : "";
      body = `<section class="set-card detail" data-section="${sec.id}">
          <div class="dhead"><span class="avatar big">${icon(sec.icon)}</span><div><h2>${esc(sec.label)}</h2><small class="lstat ${tone}">${tone !== "off" ? "<i></i>" : ""}<span>${esc(st)}</span></small></div>
            <label class="enable-lbl">${sec.enabled ? "On" : "Off"}<button type="button" class="m3-switch ${sec.enabled ? "on" : ""}" data-enable role="switch" aria-checked="${sec.enabled}" aria-label="Turn ${esc(sec.label)} on or off"></button></label></div>
          <details class="howto" ${sec.enabled && tone === "good" ? "" : "open"}><summary>${icon("help")}How to set up</summary><p>${esc(sec.help)}</p>${push}</details>
          ${gws}
          ${basic.length ? `<div class="set-fields">${basic.map(f => fieldHtml(sec, f)).join("")}</div>` : ""}
          ${adv.length ? `<details class="adv"><summary>Advanced</summary><div class="set-fields">${adv.map(f => fieldHtml(sec, f)).join("")}</div></details>` : ""}
          <div class="set-actions">${loginBtn}<span style="flex:1"></span><button type="button" class="filled-btn" data-save>${icon("save")}Save</button></div>
        </section>`;
    }
    $("#rooms").innerHTML = `<div class="settings-wrap detail">${back}${body}</div>`;
  }

  function collectSection(el) {
    const values = {};
    $$("[data-key]", el).forEach(i => {
      if (i.classList.contains("servers")) {
        values[i.dataset.key] = $$(".server-row", i).map(r => Object.fromEntries($$("[data-skey]", r).map(x => [x.dataset.skey, x.value.trim()])));
      } else values[i.dataset.key] = i.type === "checkbox" ? i.checked : i.value;
    });
    return { enabled: $("[data-enable]", el).classList.contains("on"), values };
  }

  // ---------- custom views ----------
  function openViewEditor(existing) {
    const dlg = $("#viewdlg"), f = $("#view-form");
    const v = existing ? JSON.parse(JSON.stringify(existing)) : { id: Math.random().toString(36).slice(2, 9), name: "", icon: "dashboard", rooms: [], types: [], sources: [], devices: [] };
    $("#view-title").textContent = existing ? "Edit view" : "New view";
    $("#view-delete").hidden = !existing;
    f.name.value = v.name;
    const all = [...devices.values()].filter(d => !d.hidden);
    const rooms = [...new Set(all.map(d => d.room || "Other"))].sort();
    const sources = [...new Set(all.map(d => d.source))].sort();
    const chips = (key, items, label = x => x) => items.map(x => `<button type="button" class="fchip ${v[key].includes(x) ? "on" : ""}" data-pick="${key}" data-v="${esc(x)}">${v[key].includes(x) ? icon("check") : ""}${esc(label(x))}</button>`).join("");
    const draw = () => {
      $("#icon-pick").innerHTML = VIEW_ICONS.map(i => `<button type="button" class="${v.icon === i ? "on" : ""}" data-icon-pick="${i}" aria-label="${i}">${icon(i, v.icon === i)}</button>`).join("");
      $("#pick-rooms").innerHTML = chips("rooms", rooms);
      $("#pick-types").innerHTML = chips("types", Object.keys(VIEW_TYPES), t => VIEW_TYPES[t]);
      $("#pick-sources").innerHTML = chips("sources", sources, x => SRC[x] || x);
      $("#pick-devices").innerHTML = all.sort((a, b) => (a.room || "").localeCompare(b.room || "") || a.name.localeCompare(b.name)).map(d =>
        `<label><input type="checkbox" data-pick-dev="${esc(d.id)}" ${v.devices.includes(d.id) ? "checked" : ""}><span>${esc(d.name)}</span><small>${esc(d.room || "Other")} · ${esc(srcLabel(d))}</small></label>`).join("");
      const n = all.filter(d => viewMatch(v, d)).length;
      $("#view-count").innerHTML = `${icon("info")}<span>${n} device${n === 1 ? "" : "s"} match${n === 1 ? "es" : ""} right now</span>`;
    };
    f.onclick = e => {
      const c = e.target.closest("[data-pick]"); if (c) { const k = c.dataset.pick, x = c.dataset.v;
        v[k] = v[k].includes(x) ? v[k].filter(y => y !== x) : [...v[k], x]; draw(); return; }
      const ip = e.target.closest("[data-icon-pick]"); if (ip) { v.icon = ip.dataset.iconPick; draw(); }
    };
    f.onchange = e => { const cb = e.target.closest("[data-pick-dev]"); if (!cb) return;
      v.devices = cb.checked ? [...v.devices, cb.dataset.pickDev] : v.devices.filter(x => x !== cb.dataset.pickDev); draw(); };
    $("#view-delete").onclick = async () => {
      dlg.close("cancel");
      if (!(await confirmAction(`Delete "${existing.name}"?`, "Only the view is removed. Devices and their rooms stay as they are.", "delete"))) return;
      layout.views = layout.views.filter(x => x.id !== existing.id); await saveLayout(); go("all"); snack("View deleted");
    };
    dlg.onclose = async () => {
      if (dlg.returnValue !== "save") return;
      v.name = f.name.value.trim() || "View";
      layout.views = existing ? layout.views.map(x => x.id === v.id ? v : x) : [...(layout.views || []), v];
      await saveLayout(); renderViewNav.key = null; go("v:" + v.id); snack(existing ? "View updated" : `"${v.name}" added to the menu`);
    };
    draw(); dlg.returnValue = ""; dlg.showModal(); setTimeout(() => f.name.focus(), 50);
  }

  // ---------- arranging (edit mode) ----------
  function destroySortables() { sortables.forEach(x => { try { x.destroy(); } catch {} }); sortables = []; }
  function renderEditBar() {
    let bar = $("#editbar");
    if (!editing() || PAGES.has(view) && view !== "lights" || mode === "table") { bar?.remove(); return; }
    if (!bar) { $("#rooms").insertAdjacentHTML("beforebegin", `<div id="editbar" class="editbar">${icon("edit")}<span class="short"><b>Arranging.</b> Drag cards and room titles. Drop on ★ Favorites to pin.</span><span class="long"><b>Arranging.</b> Drag cards to reorder or move them between rooms. Drag a room by its title to reorder rooms${view === "all" ? ". Drag cards into Favorites to pin them to the top" : ""}. Use ✎ on a card to rename or hide it.</span>
      <button class="text-btn" data-reset-layout>Reset</button><button class="filled-btn" data-done-edit>${icon("check")}Done</button></div>`); hydrateIcons($("#editbar")); }
  }
  function initSortables() {
    if (!editing() || !window.Sortable || mode === "table") return;
    const common = { animation: 160, delay: 120, delayOnTouchOnly: true, forceFallback: true, fallbackTolerance: 4, ghostClass: "drag-ghost", chosenClass: "drag-chosen",
      filter: ".m3-switch, .segmented, .tonal-btn, .quick, input, .card-edit, .colors", preventOnFilter: false,
      onStart: () => { dragging = true; }, };
    $$("#rooms .grid").forEach(g => {
      if (g.closest("[data-fixed]")) return;
      sortables.push(Sortable.create(g, { ...common, group: "cards", draggable: ".card", onEnd: onCardDrop }));
    });
    sortables.push(Sortable.create($("#rooms"), { ...common, draggable: ".room:not(.fav):not([data-fixed])", handle: "h2", onEnd: onRoomDrop }));
  }
  async function saveLayout() {
    try { layout = await api("/api/layout", { method: "PUT", body: JSON.stringify(layout) }); }
    catch (e) { snack(`Couldn't save layout: ${e.message}`); }
  }
  async function onCardDrop(e) {
    dragging = false;
    const id = e.item.dataset.id, from = e.from.dataset.room, to = e.to.dataset.room;
    // capture the visible order of every room grid
    $$("#rooms .grid[data-room]").forEach(g => {
      const ids = $$(":scope > .card", g).map(c => c.dataset.id);
      if (g.dataset.room === FAV) layout.pinned = ids;
      else {
        // keep positions of devices hidden by the current filter
        const prev = (layout.order[g.dataset.room] || []).filter(x => !ids.includes(x) && devices.get(x)?.room === g.dataset.room);
        layout.order[g.dataset.room] = [...ids, ...prev];
      }
    });
    if (from !== to) {
      if (from === FAV) layout.pinned = layout.pinned.filter(x => x !== id);
      if (to !== FAV) {
        const d = devices.get(id), newRoom = to === "Other" ? "" : to;
        if (d && (d.room || "") !== newRoom) {
          try { await api(`/api/devices/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ room: newRoom }) });
            snack(`${d.name} moved to ${to}`); } catch (err) { snack(err.message); }
        }
      } else snack(`${devices.get(id)?.name} pinned to Favorites`);
    }
    await saveLayout(); schedule();
  }
  async function onRoomDrop() {
    dragging = false;
    const shownRooms = $$("#rooms > .room:not(.fav):not([data-fixed])").map(s => s.dataset.room);
    const rest = layout.rooms.filter(r => !shownRooms.includes(r));
    layout.rooms = [...shownRooms, ...rest];
    await saveLayout(); schedule();
  }

  // ---------- main render ----------
  function render() {
    destroySortables();
    renderViewNav();
    if (view.startsWith("v:") && !customView() && layout.views) { view = "all"; store("filter", view); }
    renderHeader();
    renderEditBar();
    renderBadges();
    const list = visible();
    $("#viewmode").style.display = PAGES.has(view) ? "none" : "";
    document.body.dataset.view = view;
    if (view === "settings") { if (!$(".settings-wrap")) renderSettings(); return; }
    if (view === "devices") { if (!$(".devlist")) devicesPage(); else renderDeviceList(); return; }
    if (pluginPages[view]) {
      const pg = pluginPages[view], el = $("#rooms");
      if (el.dataset.plugin !== view) { $("#summary").innerHTML = ""; $("#alerts").innerHTML = ""; el.dataset.plugin = view; el._html = null; nativeHTML.set.call(el, ""); pg.render(el); }
      else pg.onDevices?.(el);
      return;
    }
    delete $("#rooms").dataset.plugin;
    if (view === "battery") return batteryPage(list);
    if (view === "air") return airPage(list);
    if (view === "power") return powerPage(list);
    if (view === "lights") return lightsPage(list);
    $("#summary").innerHTML = view === "all" && list.length ? homeTiles(list) : view === "doors" ? doorTiles(list) : "";
    renderChips(list);
    const shown = list.filter(matches);
    if (view === "doors") shown.sort((a, b) => (val(b, "contact") === "open") - (val(a, "contact") === "open"));
    if (!shown.length) { $("#rooms").innerHTML = customView() ? `<div class="empty-state">${icon(customView().icon)}<h3>Nothing matches this view yet</h3><p>Pick rooms, types, brands or devices for it.</p><button class="tonal-btn" data-edit-view>${icon("tune")}Edit view</button></div>`
      : `<p class="empty">${list.length ? "Nothing in this view yet." : "No devices yet. Add your accounts in Settings."}</p>`; return; }
    if (mode === "table") { $("#rooms").innerHTML = tableView(shown); return; }
    const fallback = (a, b) => isAlarm(b) - isAlarm(a) || (view === "doors" ? (val(b, "contact") === "open") - (val(a, "contact") === "open") : 0) || a.name.localeCompare(b.name);
    $("#rooms").innerHTML = roomSections(shown, { favorites: view === "all", fallback });
    initSortables();
    $("#roomlist").innerHTML = [...new Set(list.map(d => d.room).filter(Boolean))].map(r => `<option value="${esc(r)}">`).join("");
  }
  function renderViewNav() {
    const views = layout.views || [];
    const key = JSON.stringify(views.map(v => [v.id, v.name, v.icon]));
    if (renderViewNav.key === key) return; renderViewNav.key = key;
    $("#custom-views").innerHTML = views.map(v => `<button data-f="v:${esc(v.id)}"><span class="ind">${icon(v.icon)}</span><span class="lbl">${esc(v.name)}</span></button>`).join("");
    $("#more-views").innerHTML = views.map(v => `<button value="v" data-f="v:${esc(v.id)}">${icon(v.icon)}${esc(v.name)}</button>`).join("");
  }
  function renderBadges() {
    const list = visible();
    const low = list.filter(batteryLow).length, open = list.filter(d => val(d, "contact") === "open" && !has(d, "lock")).length;
    const pw = list.filter(d => isUps(d) && val(d, "ups_status") !== "online").length;
    const badAir = list.filter(d => (kindOf(d) === "aqi_station" && val(d, "aqi") > 100) || (val(d, "air_score") != null && val(d, "air_score") < 60)).length;
    $("#badge-air").textContent = badAir || "";
    $("#badge-lights").textContent = list.filter(d => isLight(d) && !isGroup(d) && val(d, "light")).length || "";
    $("#badge-battery").textContent = low || ""; $("#badge-doors").textContent = open || ""; $("#badge-power").textContent = pw || "";
    $("#badge-more").textContent = (pw + low) || "";
  }
  let pending = false, sliding = false;
  const schedule = () => { if (!pending) { pending = true; requestAnimationFrame(() => { pending = false; if (sliding || dragging) return setTimeout(schedule, 300); render(); }); } };
  const scheduleQuiet = () => { document.body.classList.add("quiet"); schedule(); };

  function renderHeader() {
    const now = new Date(), h = now.getHours();
    const cv = customView();
    $("#editview").hidden = !cv;
    $("#greeting").textContent = cv ? cv.name : TITLES[view] || (h < 5 ? "Good night" : h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening");
    $("#subtitle").textContent = (view === "all" ? `${appTitle} · ` : "") + now.toLocaleDateString([], { weekday: innerWidth < 840 ? "short" : "long", month: innerWidth < 840 ? "short" : "long", day: "numeric", ...timeOpts() }) +
      " · " + now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", ...timeOpts() });
    $$("#nav [data-f]").forEach(b => b.classList.toggle("on", b.dataset.f === view));
    $(".more-btn").classList.toggle("on", !$(`#nav [data-f="${view}"][data-phone]`));
    $$("#more [data-f]").forEach(b => b.classList.toggle("on", b.dataset.f === view));
    const vm = $("#viewmode"); vm.innerHTML = icon(mode === "table" ? "grid_view" : "table_rows");
    vm.title = vm.ariaLabel = mode === "table" ? "Card view" : "Table view";
  }
  function go(v) {
    view = v; store("filter", v); document.body.classList.remove("quiet");
    if (v === "settings") { settingsSub = null; $("#rooms").innerHTML = ""; loadSettings(); }
    if (v === "power") loadPower();
    schedule(); window.scrollTo({ top: 0, behavior: "smooth" });
  }

  // ---------- data ----------
  async function loadSparks() {
    await Promise.all(visible().map(async d => {
      const sc = sparkCap(d); if (!sc) return;
      try { sparks.set(d.id + "|" + sc, await api(`/api/devices/${encodeURIComponent(d.id)}/history/${sc}`)); } catch {}
    }));
    if (!PAGES.has(view)) scheduleQuiet();
  }
  async function loadStatus() {
    try {
      const h = await fetch("/api/health").then(r => r.json());
      $("#status").innerHTML = Object.entries(h.adapters).map(([n, s]) => `<span><b>${esc(SRC[n] || n)}</b> ${esc(s)}</span>`).join("") + `<span>${h.devices} devices</span>`;
    } catch {}
  }
  function connect() {
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
    ws.onopen = () => {
      if (token) ws.send(JSON.stringify({ type: "auth", token }));   // the password never goes in the URL
      $("#conn").classList.add("on"); $("#conn").ariaLabel = "Live"; connect.delay = 2000; };
    ws.onclose = e => {
      $("#conn").classList.remove("on"); $("#conn").ariaLabel = "Disconnected, reconnecting";
      if (e.code === 4401) return signInBanner();
      if (e.code === 4429) { connect.delay = 30000; snack("Too many wrong passwords from this device. Waiting before trying again."); }
      if (e.code === 4403) return snack("This page was opened from another site, so live updates are blocked.");
      connect.delay = Math.min(30000, (connect.delay || 2000) * 1.6);       // back off: 2 s … 30 s
      setTimeout(connect, connect.delay);
    };
    ws.onmessage = e => {
      let m; try { m = JSON.parse(e.data); } catch { return; }            // never let one bad message blank the page
      msgHooks.forEach(fn => { try { fn(m); } catch {} });
      if (m.type === "layout") { layout = m.layout; if (!dragging) schedule(); return; }
      if (m.type === "snapshot") { if (m.layout) layout = m.layout; devices.clear(); m.devices.forEach(d => devices.set(d.id, d)); schedule(); loadSparks(); if (view === "power") loadPower(); }
      else if (m.type === "device") { devices.set(m.device.id, m.device); scheduleQuiet(); if (panelId === m.device.id && $("#devpanel").open && !sliding) renderDevicePanel(); }
      else if (m.type === "remove") { devices.delete(m.id); scheduleQuiet(); }
    };
  }

  // ---------- feedback ----------
  let snackTimer;
  function snack(text) {
    const s = $("#snackbar"); s.textContent = text; s.classList.add("show");
    clearTimeout(snackTimer); snackTimer = setTimeout(() => s.classList.remove("show"), 3600);
  }
  function confirmAction(title, text, ic) {
    const dlg = $("#confirm");
    $("#confirm-title").textContent = title; $("#confirm-text").textContent = text;
    $("#confirm-icon").innerHTML = icon(ic); dlg.returnValue = "";
    dlg.showModal();
    return new Promise(res => dlg.onclose = () => res(dlg.returnValue === "yes"));
  }

  // ---------- sign-in flows (Ring, Aqara) ----------
  function genericSignin(sec) {
    const dlg = $("#login"), fields = $("#login-fields"), help = $("#login-help");
    let step = 1; const creds = {};
    const tf = f => `<label class="textfield"><input name="${f.key}" type="${f.type === "password" ? "password" : "text"}" placeholder=" " required autocomplete="off"><span>${esc(f.label)}</span></label>`;
    const show = (msg) => { $("#login-title").textContent = `Sign in to ${sec.label}`;
      help.textContent = msg || (step === 1 ? sec.login_help || "" : "Enter the code that was just sent to you.");
      fields.innerHTML = step === 1 ? sec.login_fields.map(tf).join("") : tf({ key: "code", label: "Verification code" });
      $("#login-go").textContent = "Continue"; setTimeout(() => $("input", fields)?.focus(), 50); };
    $("#login-form").onsubmit = async e => {
      if (e.submitter?.value !== "go") return; e.preventDefault();
      Object.assign(creds, Object.fromEntries(new FormData($("#login-form"))));
      $("#login-go").disabled = true; $("#login-go").textContent = "Working…";
      try { const res = await api(`/api/settings/${sec.id}/signin`, { method: "POST", body: JSON.stringify(creds) });
        if (res.need_code) { step = 2; show(res.message); } else { dlg.close(); snack(`Signed in to ${sec.label}`); loadSettings(); } }
      catch (err) { help.textContent = err.message; $("#login-go").textContent = "Try again"; }
      finally { $("#login-go").disabled = false; }
    };
    show(); dlg.showModal();
  }
  function loginFlow(kind) {
    const gsec = settingsData?.sections?.find(x => x.id === kind && x.login_fields);
    if (gsec) return genericSignin(gsec);
    const dlg = $("#login"), fields = $("#login-fields"), help = $("#login-help");
    let step = 1, creds = {};
    const tf = (n, l, t = "text") => `<label class="textfield"><input name="${n}" type="${t}" placeholder=" " required autocomplete="${t === "password" ? "current-password" : "off"}"><span>${l}</span></label>`;
    const NAMES = { ring: "Ring", aqara: "Aqara", hue: "Philips Hue" };
    const show = async () => {
      $("#login-title").textContent = kind === "hue" ? "Pair Hue Bridge" : `Sign in to ${NAMES[kind]}`;
      if (kind === "hue") {
        $("#login-go").textContent = "Continue";
        if (step === 1) {
          help.textContent = "Looking for Hue bridges on your network…";
          fields.innerHTML = tf("host", "Bridge IP address");
          const current = settingsData?.sections?.find(x => x.id === "hue")?.values?.host;
          if (current) $("input[name=host]", fields).value = current;
          try { const r = await api("/api/settings/hue/discover");
            const found = (r.bridges || []).map(b => b.host);
            if (found.length && !$("input[name=host]", fields).value) $("input[name=host]", fields).value = found[0];
            help.textContent = found.length ? `Found ${found.length} bridge${found.length > 1 ? "s" : ""}: ${found.join(", ")}` :
              "No bridge found automatically. Enter its IP address (Hue app → Settings → Bridges → ⓘ).";
          } catch { help.textContent = "Enter your Hue Bridge's IP address (Hue app → Settings → Bridges → ⓘ)."; }
        } else {
          help.textContent = "Press the round link button on top of the Hue Bridge, then tap Continue within 30 seconds.";
          fields.innerHTML = "";
        }
        return;
      }
      if (step === 1) {
        help.textContent = kind === "ring" ? "Your password is only used once to get a token; it isn't stored." : "Aqara will send a verification code to this account.";
        fields.innerHTML = kind === "ring" ? tf("email", "Email") + tf("password", "Password", "password") : tf("account", "Aqara account (email or phone)");
      } else {
        help.textContent = kind === "ring" ? "Enter the verification code Ring just sent you." : "Enter the code Aqara sent you.";
        fields.innerHTML = tf("code", "Verification code");
      }
      $("#login-go").textContent = "Continue";
      setTimeout(() => $("input", fields)?.focus(), 50);
    };
    $("#login-form").onsubmit = async e => {
      if (e.submitter?.value !== "go") return;
      e.preventDefault();
      const data = Object.fromEntries(new FormData($("#login-form")));
      Object.assign(creds, data);
      $("#login-go").disabled = true; $("#login-go").textContent = "Working…";
      try {
        let res;
        if (kind === "hue") res = await api("/api/settings/hue/pair", { method: "POST", body: JSON.stringify({ host: creds.host }) });
        else {
          const body = kind === "ring" ? { email: creds.email, password: creds.password, code: creds.code } : { account: creds.account, code: creds.code };
          res = await api(`/api/settings/${kind}/login`, { method: "POST", body: JSON.stringify(body) });
        }
        if (res.need_code || res.need_button) { step = 2; await show(); }
        else { dlg.close(); snack(kind === "hue" ? "Hue Bridge paired" : `Signed in to ${NAMES[kind]}`); loadSettings(); }
      } catch (err) { help.textContent = err.message; $("#login-go").textContent = "Try again"; }
      finally { $("#login-go").disabled = false; }
    };
    show(); dlg.showModal();
  }

  // ---------- history dialog ----------
  let histRanges = ["1h", "24h", "7d", "30d", "1y"], histBackend = "memory", hist = null;
  api("/api/history/info").then(i => { if (i.ranges?.length) histRanges = i.ranges; histBackend = i.backend; }).catch(() => {});
  const fmtTime = (t, span) => new Date(t * 1000).toLocaleString([], { ...(span > 2 * 86400 ? { month: "short", day: "numeric" } : { hour: "numeric", minute: "2-digit" }), ...timeOpts() });
  async function openHistory(id, capName, range = load("histRange", "24h")) {
    const d = devices.get(id); hist = { id, capName, range };
    const c = d?.capabilities[capName];
    const conv = v => capName === "temperature" ? toUnit(v) : v;
    const u = capName === "temperature" ? `°${unit}` : (c?.unit || "");
    $("#hist-title").textContent = `${d?.name || ""} · ${LABEL[capName] || (capName === "temperature" ? "Temperature" : capName === "battery" ? "Battery" : capName)}`;
    $("#hist-ranges").innerHTML = histRanges.map(r => `<button type="button" data-hrange="${r}" class="${r === range ? "on" : ""}">${r}</button>`).join("");
    $("#hist-chart").innerHTML = `<div class="snap-wait">${icon("timer")}<span>Loading…</span></div>`;
    $("#hist-stats").innerHTML = "";
    if (!$("#history").open) $("#history").showModal();
    let pts = [];
    try { pts = await api(`/api/devices/${encodeURIComponent(id)}/history/${capName}?range=${range}`); } catch (e) { $("#hist-chart").innerHTML = `<div class="snap-wait">${esc(e.message)}</div>`; return; }
    if (hist.range !== range || hist.id !== id) return;
    if (pts.length < 2) { $("#hist-chart").innerHTML = `<div class="snap-wait">${icon("info")}<span>Not enough data for ${range} yet${histBackend === "memory" ? ". Turn on history storage in Settings → History & metrics to keep more than 24 hours." : "."}</span></div>`; return; }
    const W = 640, H = 220, P = { l: 44, r: 12, t: 12, b: 26 };
    const vs = pts.map(p => conv(p.v)), t0 = pts[0].t, t1 = pts[pts.length - 1].t;
    let lo = Math.min(...vs), hi = Math.max(...vs); if (hi - lo < 1e-6) { lo -= 1; hi += 1; }
    const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
    const X = t => P.l + (t - t0) / ((t1 - t0) || 1) * (W - P.l - P.r), Y = v => P.t + (1 - (v - lo) / (hi - lo)) * (H - P.t - P.b);
    const line = pts.map((p, i) => `${i ? "L" : "M"}${X(p.t).toFixed(1)},${Y(conv(p.v)).toFixed(1)}`).join("");
    const grid = [0, 0.25, 0.5, 0.75, 1].map(f => { const v = lo + (hi - lo) * f, y = Y(v);
      return `<line x1="${P.l}" x2="${W - P.r}" y1="${y}" y2="${y}" class="g"/><text x="${P.l - 6}" y="${y + 4}" text-anchor="end">${+v.toFixed(1)}</text>`; }).join("");
    const ticks = [0, 0.5, 1].map(f => { const t = t0 + (t1 - t0) * f;
      return `<text x="${X(t)}" y="${H - 6}" text-anchor="${f === 0 ? "start" : f === 1 ? "end" : "middle"}">${fmtTime(t, t1 - t0)}</text>`; }).join("");
    $("#hist-chart").innerHTML = `<svg viewBox="0 0 ${W} ${H}" class="hchart">${grid}${ticks}
      <path class="area" d="${line}L${X(t1)},${H - P.b}L${X(t0)},${H - P.b}Z"/><path class="line" d="${line}"/></svg>`;
    const avg = vs.reduce((a, b) => a + b, 0) / vs.length;
    $("#hist-stats").innerHTML = [["Low", Math.min(...vs)], ["Average", avg], ["High", Math.max(...vs)], ["Now", vs[vs.length - 1]]]
      .map(([l, v]) => `<div><small>${l}</small><b>${+v.toFixed(1)}${esc(u)}</b></div>`).join("");
  }

  // ---------- snapshot ----------
  let snapId = null;
  async function showSnapshot(id) {
    snapId = id; const d = devices.get(id);
    $("#snap-title").textContent = d ? d.name : "Snapshot";
    $("#snap-frame").innerHTML = `<div class="snap-wait">${icon("photo_camera")}<span>Asking the camera for a snapshot…</span></div>`;
    if (!$("#snapshot").open) $("#snapshot").showModal();
    try {
      const r = await fetch(`/api/ring/${encodeURIComponent(id)}/snapshot`, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
      if (showSnapshot.url) URL.revokeObjectURL(showSnapshot.url);
      const url = showSnapshot.url = URL.createObjectURL(await r.blob());
      $("#snap-frame").innerHTML = `<img src="${url}" alt="Snapshot from ${esc(d?.name)}"><small>${new Date().toLocaleTimeString([], timeOpts())}</small>`;
    } catch (e) { $("#snap-frame").innerHTML = `<div class="snap-wait">${icon("wifi_off")}<span>${esc(e.message)}</span></div>`; }
  }
  $("#snap-refresh").onclick = () => snapId && showSnapshot(snapId);

  // ---------- interactions ----------
  const VERB = { lock: "Lock", unlock: "Unlock", open: "Open", close: "Close", test: "Test", mute: "Mute", siren: "Sound siren on",
    disarm: "Disarm", arm_home: "Arm (home)", arm_away: "Arm (away)" };
  document.addEventListener("click", async e => {
    const t = e.target;
    const b = t.closest("[data-cmd]");
    if (b) {
      const id = b.closest("[data-id]").dataset.id, d = devices.get(id);
      const capName = b.dataset.cmd, value = JSON.parse(b.dataset.val);
      if (b.closest(".segmented") && b.classList.contains("on")) return;   // already in that state
      const quiet = capName === "action" && !["test", "siren", "mute"].includes(value);
      if (!quiet && ["lock", "valve", "action", "alarm_panel"].includes(capName)) {
        const ic = { lock: value === "lock" ? "lock" : "lock_open", valve: "valve", action: value === "siren" ? "campaign" : "detector_smoke", alarm_panel: "shield" }[capName];
        const msg = capName === "action" ? (value === "siren" ? "The siren will sound for 30 seconds." : "The alarm will sound briefly.") : "This controls a physical device in your home.";
        if (!(await confirmAction(`${VERB[value] || value} ${d.name}?`, msg, ic))) return;
      }
      if (b.dataset.busy) return;
      b.dataset.busy = "1"; b.setAttribute("aria-disabled", "true");   // not .disabled: that drops keyboard focus
      try {
        await api(`/api/devices/${encodeURIComponent(id)}/command`, { method: "POST", body: JSON.stringify({ capability: capName, value }) });
        snack(["switch", "light"].includes(capName) ? `${d.name} ${capName === "light" ? "light " : ""}turned ${value ? "on" : "off"}` : `${VERB[value] || value} sent to ${d.name}`);
      } catch (err) { snack(`Couldn't reach ${d.name}: ${err.message}`); resetView(); }
      finally { delete b.dataset.busy; b.removeAttribute("aria-disabled"); }
      return;
    }
    if (t.closest("[data-snapshot]")) return showSnapshot(t.closest("[data-id]").dataset.id);
    if (t.closest("[data-open-device]") || (t.closest(".card .name") && !editing())) return openDevicePanel(t.closest("[data-id]").dataset.id);
    const hb = t.closest("[data-hist]"); if (hb) return openHistory(hb.closest("[data-id]").dataset.id, hb.dataset.hist);
    const hr = t.closest("[data-hrange]"); if (hr && hist) { store("histRange", hr.dataset.hrange); return openHistory(hist.id, hist.capName, hr.dataset.hrange); }
    const pr = t.closest("[data-prange]"); if (pr) { powerRange = pr.dataset.prange; store("powerRange", powerRange); return loadPower(); }
    if (t.closest("[data-edit]")) {
      const id = t.closest("[data-id]").dataset.id, d = devices.get(id), dlg = $("#edit"), f = dlg.querySelector("form");
      f.name.value = d.name; f.room.value = d.room || ""; f.hidden.checked = d.hidden; f.pinned.checked = layout.pinned.includes(id); dlg.returnValue = "";
      dlg.showModal();
      dlg.onclose = async () => { if (dlg.returnValue !== "save") return;
        try { await api(`/api/devices/${encodeURIComponent(id)}`, { method: "PATCH",
          body: JSON.stringify({ name: f.name.value, room: f.room.value, hidden: f.hidden.checked }) });
          const pin = f.pinned.checked, was = layout.pinned.includes(id);
          if (pin !== was) { layout.pinned = pin ? [...layout.pinned, id] : layout.pinned.filter(x => x !== id); await saveLayout(); schedule(); }
          snack("Saved"); }
        catch (err) { snack(err.message); } };
      return;
    }
    const th = t.closest("th[data-sort]");
    if (th) { const k = th.dataset.sort; sort = { key: k, dir: sort.key === k ? -sort.dir : 1 }; store("sort", JSON.stringify(sort)); scheduleQuiet(); return; }
    const tabBtn = t.closest(".devrow [data-tab]");
    if (tabBtn) { const id = tabBtn.closest("[data-id]").dataset.id, d = devices.get(id), tab = tabBtn.dataset.tab;
      const cur = tabsOf(d), next = cur.includes(tab) ? cur.filter(x => x !== tab) : [...cur, tab];
      const label = TABS.find(x => x[0] === tab)[1];
      return patchDevice(id, { categories: TAB_IDS.filter(x => next.includes(x)) }, `${d.name} ${cur.includes(tab) ? "removed from" : "added to"} ${label}`); }
    if (t.closest("[data-reset-tabs]")) { const id = t.closest("[data-id]").dataset.id;
      return patchDevice(id, { auto_categories: true }, `${devices.get(id).name} back to automatic tabs`); }
    if (t.closest(".devrow [data-visible]")) { const id = t.closest("[data-id]").dataset.id, d = devices.get(id);
      return patchDevice(id, { hidden: !d.hidden }, `${d.name} ${d.hidden ? "shown" : "hidden"}`); }
    const df = t.closest("[data-devfilter]"); if (df) { devFilter = devFilter === df.dataset.devfilter ? "all" : df.dataset.devfilter; devicesPage(); return; }
    if (t.closest("#devsortdir")) { devSort.dir = -devSort.dir; store("devSort", JSON.stringify(devSort)); devicesPage(); return; }
    const os = t.closest("[data-open-setting]"); if (os) { settingsSub = os.dataset.openSetting; renderSettings(); window.scrollTo({ top: 0 }); return; }
    const ea = t.closest("[data-eco-approve], [data-eco-forget]");
    if (ea) {
      const approve = ea.hasAttribute("data-eco-approve"), id = ea.dataset.ecoApprove || ea.dataset.ecoForget;
      try { ecoGw.data = await api(`/api/ecowitt/gateways/${encodeURIComponent(id)}${approve ? "/approve" : ""}`, { method: approve ? "POST" : "DELETE" });
        snack(approve ? "Gateway trusted. Its sensors appear with its next report." : "Gateway removed"); loadSettings(); }
      catch (err) { snack(err.message); }
      return;
    }
    if (t.closest("[data-settings-back]")) { settingsSub = null; renderSettings(); window.scrollTo({ top: 0 }); return; }
    if (t.closest("[data-new-view]")) { if ($("#more").open) $("#more").close(); return openViewEditor(null); }
    if (t.closest("[data-edit-view]") || t.closest("#editview")) return openViewEditor(customView());
    const nav = t.closest("[data-f]");
    if (nav) { if (nav.closest("#more")) $("#more").close(); go(nav.dataset.f); return; }
    if (t.closest("[data-open=more]")) { hydrateIcons($("#more")); $("#more").showModal(); return; }
    if (t.closest("[data-goto]")) return go(t.closest("[data-goto]").dataset.goto);
    const gs = t.closest("[data-goto-setting]"); if (gs) { go("settings"); settingsSub = gs.dataset.gotoSetting; return; }
    // settings page
    const card = t.closest(".set-card[data-section]");
    if (card) {
      if (t.closest("[data-enable]")) { const sw = t.closest("[data-enable]"); sw.classList.toggle("on"); sw.ariaChecked = sw.classList.contains("on");
        if (sw.parentElement.classList.contains("enable-lbl")) sw.parentElement.firstChild.textContent = sw.classList.contains("on") ? "On" : "Off";
        return; }
      if (t.closest("[data-add-server]")) { t.closest("[data-add-server]").insertAdjacentHTML("beforebegin", serverRow()); return; }
      if (t.closest("[data-del-server]")) { t.closest(".server-row").remove(); return; }
      if (t.closest("[data-login]")) return loginFlow(t.closest("[data-login]").dataset.login);
      if (t.closest("[data-save]")) {
        const btn = t.closest("[data-save]"); btn.disabled = true;
        try {
          const sec = await api(`/api/settings/${card.dataset.section}`, { method: "PUT", body: JSON.stringify(collectSection(card)) });
          const i = settingsData.sections.findIndex(s => s.id === sec.id); settingsData.sections[i] = sec;
          snack(`${sec.label} saved${sec.enabled ? " and restarted" : " and turned off"}`);
          setTimeout(loadSettings, 2500);
          renderSettings(); loadStatus();
        } catch (err) { snack(err.message); }
        finally { btn.disabled = false; }
        return;
      }
    }
    const ms = t.closest("#mode-seg [data-mode]"); if (ms) { store("mode", ms.dataset.mode); applyMode(ms.dataset.mode); renderSettings(); return; }
    const sw = t.closest("#swatches .swatch"); if (sw) { root.dataset.seed = sw.dataset.seed; store("seed", sw.dataset.seed); applyMode(load("mode", "auto")); renderSettings(); return; }
    const us = t.closest("#unit-seg [data-unit]"); if (us) { unit = us.dataset.unit; store("unit", unit); renderSettings(); return; }
    const cs = t.closest("#style-seg [data-cstyle]"); if (cs) { cardStyle = cs.dataset.cstyle; store("cardStyle", cardStyle); document.body.dataset.cardStyle = cardStyle; renderSettings(); return; }
    const ls = t.closest("#layout-seg [data-layout]"); if (ls) { mode = ls.dataset.layout; store("viewmode", mode); renderSettings(); renderHeader(); return; }
  });

  async function sendCmd(id, capability, value, quiet = true) {
    const d = devices.get(id);
    try { await api(`/api/devices/${encodeURIComponent(id)}/command`, { method: "POST", body: JSON.stringify({ capability, value }) }); }
    catch (err) { snack(`Couldn't reach ${d?.name || "device"}: ${err.message}`); resetView(); if (!quiet) throw err; }
  }
  document.addEventListener("keydown", e => {
    if ((e.key === "Enter" || e.key === " ") && e.target.matches?.(".card .name[role=button]") && !editing()) {
      e.preventDefault(); openDevicePanel(e.target.closest("[data-id]").dataset.id); }
  });
  document.addEventListener("input", e => {
    if (e.target.id === "devsearch") { devQuery = e.target.value; renderDeviceList(); return; }
    const r = e.target.closest("[data-slider]"); if (!r) return;
    const pct = (r.value - r.min) / (r.max - r.min) * 100; r.style.setProperty("--pct", pct + "%");
    r.nextElementSibling.textContent = `${r.value}${r.dataset.suffix ?? "%"}`;
    sliding = true;
  });
  document.addEventListener("change", e => {
    const ds = e.target.closest("[data-devsel]");
    if (ds) { devSel[ds.dataset.devsel] = ds.value; devicesPage(); return; }
    if (e.target.id === "devsort") { devSort = { key: e.target.value, dir: 1 }; store("devSort", JSON.stringify(devSort)); devicesPage(); return; }
    const r = e.target.closest("[data-slider]");
    if (r) { sliding = false; sendCmd(r.closest("[data-id]").dataset.id, r.dataset.slider, +r.value); return; }
    const cs = e.target.closest("[data-cmdsel]");
    if (cs) { sendCmd(cs.closest("[data-id]").dataset.id, cs.dataset.cmdsel, cs.value); return; }
    const cp = e.target.closest("[data-colorpick]");
    if (cp) sendCmd(cp.closest("[data-id]").dataset.id, "color", cp.value);
  });
  document.addEventListener("click", e => {
    const c = e.target.closest("[data-color]");
    if (c) return sendCmd(c.closest("[data-id]").dataset.id, "color", c.dataset.color);
    const all = e.target.closest("[data-all-lights]");
    if (all) { const on = all.dataset.allLights === "true";
      const targets = visible().filter(d => isLight(d) && !isGroup(d) && !!val(d, "light") !== on);
      targets.forEach(d => sendCmd(d.id, "light", on)); snack(`Turning ${targets.length} light${targets.length === 1 ? "" : "s"} ${on ? "on" : "off"}`); }
  });

  function applyMode(pref) {
    root.dataset.mode = pref === "auto" ? (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light") : pref;
    $('meta[name="theme-color"]').content = getComputedStyle(root).getPropertyValue("--md-surface").trim();
  }
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => load("mode", "auto") === "auto" && applyMode("auto"));
  const setEditing = on => { document.body.classList.toggle("editing", on); $("#editmode").classList.toggle("on", on);
    if (on && mode === "table") snack("Switch to card view to drag things around"); document.body.classList.remove("quiet"); schedule(); };
  $("#editmode").onclick = () => setEditing(!editing());
  document.addEventListener("click", async e => {
    if (e.target.closest("[data-done-edit]")) { setEditing(false); snack("Layout saved"); }
    if (e.target.closest("[data-reset-layout]") && await confirmAction("Reset layout?", "Rooms and devices go back to automatic order and Favorites are cleared. Room assignments stay.", "dashboard")) {
      layout = { rooms: [], order: {}, pinned: [], views: layout.views || [] }; await saveLayout(); schedule(); snack("Layout reset"); }
  });
  $("#viewmode").onclick = () => { mode = mode === "table" ? "cards" : "table"; store("viewmode", mode); document.body.classList.remove("quiet"); schedule(); };
  addEventListener("scroll", () => $(".appbar").classList.toggle("scrolled", scrollY > 24), { passive: true });

  // ---------- in-place updates: live data must not wipe focus, scroll or open menus ----------
  const nativeHTML = Object.getOwnPropertyDescriptor(Element.prototype, "innerHTML");
  const KEYED = ".card[data-id], tr[data-id], .devrow[data-id], .batt-row[data-id], .ups-panel[data-id], .aqhero[data-id]";
  // outermost keyed items only (a keyed panel may hold keyed rows; those are patched with their panel)
  const topKeyed = root => [...root.querySelectorAll(KEYED)].filter(k => !k.parentElement?.closest(KEYED) || !root.contains(k.parentElement.closest(KEYED)));
  function skeleton(root) {            // structure with keyed items blanked out (but their identity kept)
    const c = document.createElement("div");
    c.append(root.cloneNode(true));                     // a fragment or an element, compare both the same way
    if (root.nodeType === 1) c.replaceChildren(...root.cloneNode(true).childNodes);
    topKeyed(c).forEach(k => k.replaceWith(document.createComment(k.dataset.id)));
    return nativeHTML.get.call(c);
  }
  function focusKey(node) {
    const a = document.activeElement; if (!a || !node.contains(a)) return null;
    for (const attr of ["data-cmd", "data-cmdsel", "data-slider", "data-hist", "data-edit", "data-open-device", "data-tab", "data-visible", "data-color", "data-colorpick", "data-snapshot"])
      if (a.hasAttribute(attr)) return `[${attr}="${CSS.escape(a.getAttribute(attr))}"]` + (a.dataset.val && a.getAttribute("role") !== "switch" && !a.classList.contains("quick") ? `[data-val="${CSS.escape(a.dataset.val)}"]` : "");
    return a.classList.contains("name") ? ".name" : null;
  }
  function patchHTML(el, html) {
    if (el._html === html) return;                       // nothing changed
    const tpl = document.createElement("template"); nativeHTML.set.call(tpl, html);
    const olds = topKeyed(el), news = topKeyed(tpl.content);
    if (el._html != null && olds.length && olds.length === news.length && olds.every((o, i) => o.dataset.id === news[i].dataset.id)
        && skeleton(el) === skeleton(tpl.content)) {
      const scrolls = [...el.querySelectorAll(".table-wrap")].map(w => w.scrollLeft);
      olds.forEach((o, i) => {
        const nn = news[i];
        if (o.outerHTML === nn.outerHTML) return;
        const fk = focusKey(o); o.replaceWith(nn);
        if (fk) nn.querySelector(fk)?.focus({ preventScroll: true });
      });
      el.querySelectorAll(".table-wrap").forEach((w, i) => { w.scrollLeft = scrolls[i] || 0; });
    } else {
      // full rebuild: still put focus back on the same control of the same device
      const a = document.activeElement, owner = a && el.contains(a) ? a.closest("[data-id]") : null;
      const fk = owner ? focusKey(owner) : null, oid = owner?.dataset.id;
      nativeHTML.set.call(el, html);
      if (fk && oid) el.querySelector(`[data-id="${CSS.escape(oid)}"]`)?.querySelector(fk)?.focus({ preventScroll: true });
    }
    el._html = html;
  }
  for (const id of ["rooms", "summary", "alerts"]) {
    const el = document.getElementById(id);
    Object.defineProperty(el, "innerHTML", { configurable: true, get() { return nativeHTML.get.call(this); }, set(v) { patchHTML(this, String(v)); } });
  }

  { const d = document.querySelector(".destinations");
    const upd = () => d && d.classList.toggle("more-below", d.scrollTop + d.clientHeight < d.scrollHeight - 4);
    d?.addEventListener("scroll", upd, { passive: true }); addEventListener("resize", upd); setTimeout(upd, 300); }
  { let wasPhone = phone(); addEventListener("resize", () => { if (phone() !== wasPhone) { wasPhone = phone(); render(); } }); }

  // after a failed command, redraw from the real state (a slider/select left where the user put it is wrong)
  function resetView() { for (const id of ["rooms", "summary", "alerts"]) { const el = document.getElementById(id); el._html = null; } $("#devpanel-body") && ($("#devpanel-body")._html = null); schedule(); }

  // ---------- small API for page plugins (e.g. /static/automations.js) ----------
  window.Haven = {
    api, icon, esc, snack, confirmAction, hydrateIcons, val, has, srcLabel, deviceIcon, statusOf, ago,
    get devices() { return devices; }, go: id => go(id), onMessage: fn => msgHooks.push(fn), timeOpts, rerender: () => { const el = $("#rooms"); delete el.dataset.plugin; schedule(); },
    registerPage(id, page) {
      pluginPages[id] = page; PAGES.add(id); TITLES[id] = page.title || page.label;
      const btn = `<button data-f="${id}"><span class="ind">${icon(page.icon)}</span><span class="lbl">${esc(page.label)}</span></button>`;
      $('#nav [data-f="devices"]').insertAdjacentHTML("afterend", btn);
      $('#more [data-f="devices"]').insertAdjacentHTML("afterend", `<button value="${id}" data-f="${id}">${icon(page.icon)}${esc(page.label)}</button>`);
      if (view === id) schedule();
    },
  };
  document.dispatchEvent(new Event("haven:ready"));

  hydrateIcons();
  document.body.dataset.cardStyle = cardStyle;
  applyMode(load("mode", "auto"));
  fetch("/api/config", { headers: token ? { Authorization: `Bearer ${token}` } : {} }).then(r => r.json()).then(c => {
    if (c.ui?.title) { appTitle = c.ui.title; document.title = appTitle; }
    const tf = c.time_format || c.time?.time_format, tz = c.timezone || c.time?.timezone;
    timeCfg = { hour12: tf === "12h" ? true : tf === "24h" ? false : undefined, timeZone: tz || undefined };
    try { new Intl.DateTimeFormat([], timeOpts()); } catch { timeCfg.timeZone = undefined; }   // ignore an invalid zone
    renderHeader(); }).catch(() => {});
  if (view === "settings") loadSettings();
  if (view === "power") loadPower();
  renderHeader();
  setInterval(renderHeader, 15000);
  setInterval(() => !PAGES.has(view) && scheduleQuiet(), 30000);
  setInterval(loadSparks, 300000);
  setInterval(() => view === "power" && loadPower(), 10000);
  setInterval(loadStatus, 15000);
  loadStatus();
  connect();
})();
