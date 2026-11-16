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
    climate: ["temperature", "humidity", "pressure", "co2", "pm25", "illuminance", "wind_speed", "rain_daily", "moisture"],
    water: ["leak", "valve", "moisture", "rain_daily"],
    doors: ["contact"],
    security: ["contact", "lock", "motion", "presence", "vibration", "tamper", "smoke", "co", "co_ppm", "heat", "alarm_panel", "ding"],
    controls: ["lock", "valve", "switch", "action", "light", "alarm_panel"],
    lights: ["light"],
  };
  const PAGES = new Set(["battery", "power", "settings", "devices"]);
  const TABS = [["climate", "Climate", "thermostat"], ["lights", "Lights", "lightbulb"], ["water", "Water", "water_drop"],
    ["doors", "Doors", "sensor_door"], ["security", "Security", "shield_lock"], ["controls", "Controls", "tune"]];
  const TAB_IDS = TABS.map(t => t[0]);
  const TITLES = { lights: "Lighting", climate: "Climate", water: "Water", doors: "Doors & windows", security: "Security", controls: "Controls",
    battery: "Batteries", power: "Power", settings: "Settings", devices: "Devices" };
  const LABEL = { humidity: "Humidity", moisture: "Soil", pressure: "Pressure", illuminance: "Light", co2: "CO₂", pm25: "PM2.5",
    wind_speed: "Wind", wind_gust: "Gust", rain_daily: "Rain", rain_rate: "Rate", uv: "UV", solar: "Solar",
    co_ppm: "CO", power: "Power", runtime: "Runtime", load: "Load", input_voltage: "Input", output_voltage: "Output" };
  const META_ICON = { wind_speed: "air", wind_gust: "air", rain_daily: "rainy", rain_rate: "rainy", uv: "sunny", solar: "sunny",
    co2: "co2", pm25: "air", pressure: "science", illuminance: "light_mode", co_ppm: "detector_smoke", moisture: "grass",
    runtime: "timer", load: "speed", input_voltage: "electrical_services", power: "bolt" };
  const SRC = { yolink: "YoLink", aqara: "Aqara", ecowitt: "Ecowitt", xsense: "X-Sense", mqtt: "MQTT", demo: "Demo", nut: "UPS", ring: "Ring", hue: "Hue" };
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
  function askToken() { const t = prompt("Dashboard password"); if (t) { token = t; store("token", t); location.reload(); } }
  const toUnit = c => unit === "F" ? c * 9 / 5 + 32 : c;
  const temp = c => c == null ? "–" : toUnit(c).toFixed(1);
  const ago = ts => { if (!ts) return ""; const s = Date.now() / 1000 - ts;
    return s < 60 ? "Just now" : s < 3600 ? `${Math.floor(s / 60)} min ago` : s < 86400 ? `${Math.floor(s / 3600)} h ago` : `${Math.floor(s / 86400)} d ago`; };
  const val = (d, k) => d.capabilities[k]?.value;
  const has = (d, k) => k in d.capabilities;
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmt = v => typeof v === "number" ? +v.toFixed(1) : esc(v);
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
  const glow = d => val(d, "color") || (val(d, "color_temp") ? kelvinRgb(val(d, "color_temp")) : "#ffd27a");

  // ---------- per-device presentation ----------
  function deviceIcon(d) {
    if (isUps(d)) { const s = val(d, "ups_status"); return [s === "online" ? "battery_charging_full" : "battery_alert", s === "low_battery" ? "bad" : s === "on_battery" ? "warn" : ""]; }
    if (isLight(d)) return [isGroup(d) ? "home_iot_device" : "lightbulb", ""];
    if (has(d, "alarm_panel")) { const s = val(d, "alarm_panel"); return ["shield", s === "triggered" ? "bad" : s && s !== "disarmed" ? "active" : ""]; }
    if (has(d, "smoke") || has(d, "co") || has(d, "heat")) return ["detector_smoke", isAlarm(d) ? "bad" : ""];
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
  function statusOf(d) {
    if (isUps(d)) return { online: ["Online", ""], on_battery: ["On battery", "warn"], low_battery: ["Battery low", "bad"], offline: ["Off", "warn"] }[val(d, "ups_status")] || ["Unknown", ""];
    if (has(d, "alarm_panel")) { const s = val(d, "alarm_panel"); return [ALARM_LABEL[s] || s || "Unknown", s === "triggered" ? "bad" : ""]; }
    if (has(d, "smoke") || has(d, "co")) return val(d, "smoke") ? ["Smoke detected", "bad"] : val(d, "co") ? ["Carbon monoxide", "bad"] : ["All clear", ""];
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
    return null;
  }
  function readingText(d) {
    if (has(d, "temperature")) return `${temp(val(d, "temperature"))}°${unit}`;
    if (has(d, "moisture")) return `${val(d, "moisture") == null ? "–" : Math.round(val(d, "moisture"))}% soil`;
    if (isUps(d)) return `${val(d, "battery") ?? "–"}% · ${runtimeText(val(d, "runtime"))}`;
    return "";
  }
  function detailText(d) {
    const parts = [];
    if (has(d, "temperature") && val(d, "humidity") != null) parts.push(`${Math.round(val(d, "humidity"))}% RH`);
    if (has(d, "temperature") && has(d, "moisture")) parts.push(`soil ${Math.round(val(d, "moisture"))}%`);
    if (isUps(d)) parts.push(`load ${val(d, "load") ?? "–"}%`, `${val(d, "input_voltage") ?? "–"} V in`);
    for (const k of ["wind_speed", "rain_daily", "co2", "pm25", "co_ppm", "illuminance", "pressure"])
      if (val(d, k) != null) parts.push(`${LABEL[k]} ${fmt(val(d, k))}${d.capabilities[k].unit ? " " + d.capabilities[k].unit : ""}`);
    if (has(d, "contact") && has(d, "lock")) parts.push(`door ${val(d, "contact") || "–"}`);
    const lm = d.attributes?.last_motion, ld = d.attributes?.last_ding;
    if (ld) parts.push(`ding ${ago(ld).toLowerCase()}`);
    if (lm) parts.push(`motion ${ago(lm).toLowerCase()}`);
    return parts.join(" · ");
  }

  function readingBlock(d) {
    if (has(d, "temperature")) {
      return `<div class="reading"><span class="v">${temp(val(d, "temperature"))}<small>°${unit}</small></span>${
        has(d, "humidity") ? `<span class="sub">${icon("water_drop")}${val(d, "humidity") == null ? "–" : Math.round(val(d, "humidity"))}%</span>` : ""}</div>`;
    }
    if (has(d, "moisture")) return `<div class="reading"><span class="v">${val(d, "moisture") == null ? "–" : Math.round(val(d, "moisture"))}<small>% soil</small></span></div>`;
    if (isUps(d)) return `<div class="reading"><span class="v">${val(d, "battery") ?? "–"}<small>%</small></span><span class="sub">${icon("timer")}${runtimeText(val(d, "runtime"))}</span></div>`;
    return "";
  }
  function statusBlock(d) {
    const s = statusOf(d);
    if (!s) return "";
    const extra = has(d, "contact") && has(d, "lock") ? `Door ${val(d, "contact") || "–"}` :
      d.attributes?.last_ding ? `Last ring ${ago(d.attributes.last_ding).toLowerCase()}` :
      d.attributes?.last_motion ? `Last motion ${ago(d.attributes.last_motion).toLowerCase()}` : "";
    return `<div class="status ${s[1]}">${s[0]}${extra ? `<span class="s2">${extra}</span>` : ""}</div>`;
  }
  function metaBlock(d) {
    const skip = new Set(["temperature", "humidity", "battery", "battery_low", "leak", "smoke", "co", "heat", "contact", "lock", "valve",
      "switch", "motion", "presence", "vibration", "action", "tamper", "ups_status", "charging", "replace_battery", "overload",
      "output_voltage", "runtime", "alarm_panel", "ding", "light", "wind_dir", "brightness", "color_temp", "color"]);
    if (!has(d, "temperature")) skip.add("moisture");
    const chips = [];
    if (isUps(d) && val(d, "charging")) chips.push(`<span>${icon("bolt")}Charging</span>`);
    if (val(d, "replace_battery")) chips.push(`<span class="low">${icon("battery_alert")}Replace battery</span>`);
    for (const [k, c] of Object.entries(d.capabilities)) {
      if (skip.has(k) || c.value == null) continue;
      chips.push(`<span>${META_ICON[k] ? icon(META_ICON[k]) : ""}${LABEL[k] || c.label || k} ${fmt(c.value)}${c.unit ? " " + esc(c.unit) : ""}</span>`);
    }
    const b = val(d, "battery");
    if (!isUps(d)) {
      if (b != null) chips.push(`<span class="${b <= 25 ? "low" : ""}">${icon(batteryIcon(b))}${b}%</span>`);
      else if (val(d, "battery_low")) chips.push(`<span class="low">${icon("battery_alert")}Low</span>`);
    }
    return chips.length ? `<div class="meta">${chips.join("")}</div>` : "";
  }
  const batteryIcon = b => b == null ? "battery_full" : b <= 10 ? "battery_alert" : b <= 25 ? "battery_1_bar" : b <= 60 ? "battery_3_bar" : b <= 90 ? "battery_5_bar" : "battery_full";

  function controlsBlock(d, compact = false) {
    const c = d.capabilities, out = [];
    const seg = (capName, items) => `<div class="segmented">${items.map(([v, label, ic, on]) =>
      `<button data-cmd="${capName}" data-val='${JSON.stringify(v)}' class="${on ? "on" : ""}" aria-pressed="${!!on}">${on ? icon("check_circle", true) : icon(ic)}${label}</button>`).join("")}</div>`;
    const sw = (capName, on, label) => `<div class="switch-row"><span>${label}</span>
      <button class="m3-switch ${on ? "on" : ""}" role="switch" aria-checked="${!!on}" data-cmd="${capName}" data-val='${JSON.stringify(!on)}' aria-label="${esc(label)} ${esc(d.name)}"></button></div>`;
    if (c.lock?.writable) out.push(seg("lock", [["lock", "Lock", "lock", val(d, "lock") === "locked"], ["unlock", "Unlock", "lock_open", val(d, "lock") === "unlocked"]]));
    if (c.valve?.writable) out.push(seg("valve", [["open", "Open", "water", val(d, "valve") === "open"], ["close", "Close", "valve", val(d, "valve") === "closed"]]));
    if (c.alarm_panel?.writable) { const s = val(d, "alarm_panel");
      out.push(seg("alarm_panel", [["disarm", "Off", "shield", s === "disarmed"], ["arm_home", "Home", "bedtime", s === "armed_home"], ["arm_away", "Away", "directions_walk", s === "armed_away"]])); }
    if (c.switch?.writable) out.push(sw("switch", val(d, "switch"), "Power"));
    if (c.light?.writable && isLight(d)) out.push(lightControls(d, view === "lights"));
    else if (c.light?.writable) out.push(sw("light", val(d, "light"), "Light"));
    const btns = [];
    if (d.attributes?.has_snapshot) btns.push(`<button class="tonal-btn" data-snapshot>${icon("photo_camera")}Snapshot</button>`);
    if (c.action?.writable) btns.push(...(c.action.options || []).map(o =>
      `<button class="tonal-btn" data-cmd="action" data-val='${JSON.stringify(o)}'>${icon({ mute: "notifications", siren: "campaign" }[o] || "science")}${o[0].toUpperCase() + o.slice(1)}</button>`));
    if (btns.length) out.push(`<div class="tonal-row">${btns.join("")}</div>`);
    return out.join("");
  }

  function slider(capName, value, min, max, ic, cls = "", disabled = false, label = v => `${v}%`) {
    const v = value ?? min, pct = ((v - min) / (max - min) * 100).toFixed(1);
    return `<div class="slider-row">${icon(ic)}<input type="range" class="m3-slider ${cls}" min="${min}" max="${max}" value="${v}" data-slider="${capName}"
      style="--pct:${pct}%" ${disabled ? "disabled" : ""} aria-label="${capName}"><output>${label(v)}</output></div>`;
  }
  const headSwitch = d => isLight(d) && d.capabilities.light?.writable ? `<button class="m3-switch ${val(d, "light") ? "on" : ""}" role="switch"
    aria-checked="${!!val(d, "light")}" data-cmd="light" data-val='${JSON.stringify(!val(d, "light"))}' aria-label="Toggle ${esc(d.name)}"></button>` : "";
  function lightControls(d, full) {
    const on = !!val(d, "light"), c = d.capabilities, a = d.attributes || {};
    const parts = [];   // the on/off switch lives in the card header (see headSwitch)
    if (c.brightness) parts.push(slider("brightness", val(d, "brightness"), 1, 100, "light_mode", "", !on));
    if (full && c.color_temp) parts.push(slider("color_temp", val(d, "color_temp"), a.ct_min || 2200, a.ct_max || 6500, "thermostat", "ct", !on, v => `${v}K`));
    if (full && c.color) parts.push(`<div class="colors">${PRESETS.map(h => `<button data-color="${h}" style="background:${h}" class="${(val(d, "color") || "").toLowerCase() === h ? "on" : ""}" aria-label="Color ${h}"></button>`).join("")}
      <label title="Custom color"><input type="color" data-colorpick value="${val(d, "color") || "#ffffff"}"></label></div>`);
    return parts.join("");
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
        <div class="titles"><div class="name" title="${esc(d.name)}">${esc(d.name)}</div><div class="src">${esc(srcLabel(d))}${d.online ? "" : " · Offline"}</div></div>
        <button class="card-edit" data-edit aria-label="Edit">${icon("edit")}</button>${headSwitch(d)}</div>
      ${readingBlock(d)}${statusBlock(d)}
      ${sc ? `<button class="spark-btn" data-hist="${sc}" aria-label="Show history">${sparkSvg(sparks.get(d.id + "|" + sc), sc)}</button>` : ""}
      ${metaBlock(d)}${controlsBlock(d)}
      <div class="updated">${updated ? ago(updated) : ""}</div></article>`;
  }

  function quickAction(d) {
    const c = d.capabilities;
    const btn = (capName, value, ic, label, on) => `<button class="quick ${on ? "on" : ""}" data-cmd="${capName}" data-val='${JSON.stringify(value)}' aria-label="${esc(label)}" title="${esc(label)}">${icon(ic, on)}</button>`;
    if (c.light?.writable || c.switch?.writable) { const k = c.light?.writable ? "light" : "switch", on = !!val(d, k);
      return `<button class="m3-switch sm ${on ? "on" : ""}" role="switch" aria-checked="${on}" data-cmd="${k}" data-val='${JSON.stringify(!on)}' aria-label="Toggle ${esc(d.name)}"></button>`; }
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
    if (has(d, "temperature")) { big = `${temp(val(d, "temperature"))}<small>°</small>`; small = [val(d, "humidity") != null ? `${Math.round(val(d, "humidity"))}% RH` : "", st ? st[0] : ""].filter(Boolean).join(" · "); }
    else if (has(d, "moisture")) { big = `${val(d, "moisture") == null ? "–" : Math.round(val(d, "moisture"))}<small>%</small>`; small = "Soil moisture"; }
    else if (isUps(d)) { big = `${val(d, "battery") ?? "–"}<small>%</small>`; small = `${st[0]} · ${runtimeText(val(d, "runtime"))}`; }
    else if (st) { big = `<span class="word">${esc(st[0])}</span>`; small = detailText(d); }
    const b = val(d, "battery");
    const lowBat = batteryLow(d) && !isUps(d) ? `<span class="mini-bat">${icon("battery_alert", true)}${b != null ? b + "%" : "Low"}</span>` : "";
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
    if (t === "power") return isUps(d);
    if (t === "lights") return isLight(d);
    const g = GROUPS[t]; return !!g && g.some(k => has(d, k) && (t !== "controls" || d.capabilities[k].writable));
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
        <td><span class="val ${st ? st[1] : ""}">${rd ? esc(rd) : ""}${rd && st ? " · " : ""}${st ? esc(st[0]) : ""}</span></td>
        <td class="muted">${esc(detailText(d))}</td>
        <td>${b != null ? levelBar(b) : bl != null ? `<span class="val ${bl ? "bad" : ""}">${bl ? "Low" : "OK"}</span>` : `<span class="muted">—</span>`}</td>
        <td class="muted">${ago(lastUpdated(d))}</td>
        <td class="c-ctl">${controlsBlock(d, true)}</td></tr>`;
    }).join("");
    return `<div class="table-wrap"><table class="dtable"><thead><tr>${th("name", "Device", "c-name")}${th("room", "Room")}${th("status", "Status")}${th("", "Details")}${th("battery", "Battery")}${th("updated", "Updated")}${th("", "Controls", "c-ctl")}</tr></thead><tbody>${rows}</tbody></table></div>`;
  }
  const levelBar = b => `<span class="level ${b <= 25 ? "low" : b <= 50 ? "mid" : ""}"><span class="bar"><i style="width:${Math.max(3, b)}%"></i></span><span>${b}%</span></span>`;

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
  function devicesPage() {
    $("#summary").innerHTML = "";
    $("#alerts").innerHTML = `<div class="devtools">
      <label class="search">${icon("search")}<input id="devsearch" type="search" placeholder="Search devices, rooms, brands" value="${esc(devQuery)}" autocomplete="off"></label>
      <div class="chipset" id="devfilters">${[["all", "All"], ["changed", "Tabs changed"], ["notab", "In no tab"], ["hidden", "Hidden"]].map(([k, l]) =>
        `<button type="button" class="fchip ${devFilter === k ? "on" : ""}" data-devfilter="${k}">${devFilter === k ? icon("check") : ""}${l}</button>`).join("")}</div></div>`;
    $("#rooms").innerHTML = `<div class="devlist"></div>`;
    renderDeviceList();
  }
  function renderDeviceList() {
    const box = $(".devlist"); if (!box) return;
    const q = devQuery.trim().toLowerCase();
    const all = [...devices.values()].filter(d => !q || `${d.name} ${d.room} ${srcLabel(d)} ${d.model}`.toLowerCase().includes(q))
      .filter(d => devFilter === "changed" ? Array.isArray(d.categories) : devFilter === "hidden" ? d.hidden : devFilter === "notab" ? !tabsOf(d).length : true)
      .sort((a, b) => !a.room - !b.room || (a.room || "").localeCompare(b.room || "") || a.name.localeCompare(b.name));
    const counts = { all: devices.size, changed: [...devices.values()].filter(d => Array.isArray(d.categories)).length,
      hidden: [...devices.values()].filter(d => d.hidden).length };
    box.innerHTML = `<p class="devnote">${icon("info")}<span>Tabs are picked automatically from what each device reports. Tap a tab to add or remove a device; changed devices are marked <b>Custom</b> and can be reset to automatic. Every device always appears on Home unless it's hidden. ${counts.changed ? `<b>${counts.changed}</b> changed.` : ""}${counts.hidden ? ` <b>${counts.hidden}</b> hidden.` : ""}</span></p>
      <div class="devhead"><span>Device</span><span>Room</span><span>Shows in tabs</span><span>Visible</span></div>` +
      (all.map(d => {
        const [ic, tone] = deviceIcon(d), tabs = tabsOf(d), custom = Array.isArray(d.categories), auto = autoTabs(d);
        return `<div class="devrow ${d.hidden ? "is-hidden" : ""}" data-id="${esc(d.id)}">
          <div class="dcell dname"><span class="avatar ${tone}">${icon(ic, !!tone)}</span><div><b>${esc(d.name)}</b><small>${esc([srcLabel(d), d.model].filter(Boolean).join(" · "))}</small></div>
            <button class="icon-btn" data-edit title="Rename, move or pin">${icon("edit")}</button></div>
          <div class="dcell droom ${d.room ? "" : "muted"}">${esc(d.room || "No room")}</div>
          <div class="dcell dtabs">${TABS.map(([t, l, ti]) => `<button type="button" class="tabchip ${tabs.includes(t) ? "on" : ""} ${!custom ? "auto" : auto.includes(t) !== tabs.includes(t) ? "changed" : ""}"
              data-tab="${t}" title="${tabs.includes(t) ? "Remove from" : "Add to"} ${l}">${icon(ti, tabs.includes(t))}${l}</button>`).join("")}
            ${custom ? `<button type="button" class="text-btn reset-tabs" data-reset-tabs title="Back to automatic: ${esc(auto.map(t => TABS.find(x => x[0] === t)[1]).join(", ") || "no tabs")}">${icon("restart_alt")}Custom · reset</button>`
              : `<span class="autotag">Auto</span>`}</div>
          <div class="dcell dvis"><button class="m3-switch sm ${d.hidden ? "" : "on"}" role="switch" aria-checked="${!d.hidden}" data-visible aria-label="Show ${esc(d.name)}"></button></div>
        </div>`; }).join("") || `<p class="empty">No devices match.</p>`);
  }
  async function patchDevice(id, body, msg) {
    try { const d = await api(`/api/devices/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(body) });
      devices.set(id, d); renderDeviceList(); renderBadges(); if (msg) snack(msg); }
    catch (err) { snack(err.message); }
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
  function powerPage(list) {
    const ups = list.filter(isUps);
    $("#summary").innerHTML = "";
    $("#alerts").innerHTML = ups.length ? `<div class="segmented ranges" id="power-ranges">${histRanges.filter(r => r !== "1h").map(r =>
      `<button type="button" data-prange="${r}" class="${r === powerRange ? "on" : ""}">${r}</button>`).join("")}</div>` : "";
    const stat = (ic, label, value) => `<div class="pstat">${icon(ic)}<div><small>${label}</small><b>${value}</b></div></div>`;
    const panels = ups.map(d => {
      const v = d.attributes?.nut_vars || {}, s = val(d, "ups_status");
      const tone = s === "low_battery" ? "bad" : s === "on_battery" ? "warn" : "";
      const [st] = statusOf(d);
      const hb = nutHist[d.id + "|battery"], hl = nutHist[d.id + "|load"];
      return `<section class="ups-panel ${tone}" data-id="${esc(d.id)}">
        <div class="ups-head"><div><h3>${esc(d.name)}</h3><small>${esc([d.model, d.room, d.attributes?.nut_host].filter(Boolean).join(" · "))}</small></div>
          <span class="pill ${tone}">${icon(s === "online" ? "power" : "power_off", true)}${st}${val(d, "charging") ? " · charging" : ""}</span></div>
        <div class="ups-body">
          <div class="gauge-wrap">${gauge(val(d, "battery"), tone)}<div class="gauge-label"><b>${val(d, "battery") ?? "–"}<small>%</small></b><span>${runtimeText(val(d, "runtime"))}</span></div></div>
          <div class="pstats">
            ${stat("speed", "Load", `${val(d, "load") ?? "–"}%`)}
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
    $("#rooms").innerHTML = ups.length || srv ? `<div class="power-wrap">${panels}${srv}</div>`
      : `<div class="empty-state">${icon("power")}<h3>No UPS yet</h3><p>Add your UniFi UPS under Settings → UPS (NUT). In UniFi Network, open the UPS → Settings and enable NUT Server.</p>
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
  function renderSettings() {
    $("#summary").innerHTML = ""; $("#alerts").innerHTML = "";
    if (!settingsData) { $("#rooms").innerHTML = `<p class="empty">Loading…</p>`; return; }
    if (settingsData.error) { $("#rooms").innerHTML = `<p class="empty">${esc(settingsData.error)}</p>`; return; }
    const pref = load("mode", "auto");
    const appearance = `<section class="set-card" id="appearance"><div class="set-head"><span class="avatar">${icon("palette")}</span><div><h3>Appearance</h3><small>Saved on this device</small></div></div>
      <div class="field-label">Theme</div><div class="segmented" id="mode-seg">${[["light", "Light", "light_mode"], ["dark", "Dark", "dark_mode"], ["auto", "Auto", "brightness_auto"]].map(([m, l, ic]) =>
        `<button type="button" data-mode="${m}" class="${pref === m ? "on" : ""}">${icon(ic)}${l}</button>`).join("")}</div>
      <div class="field-label">Color</div><div class="swatches" id="swatches">${Object.entries(SEEDS).map(([n, c]) =>
        `<button type="button" class="swatch ${root.dataset.seed === n ? "on" : ""}" data-seed="${n}" style="background:${c}" title="${n}">${root.dataset.seed === n ? icon("check_circle", true) : ""}</button>`).join("")}</div>
      <div class="field-label">Temperature</div><div class="segmented" id="unit-seg">${["F", "C"].map(u => `<button type="button" data-unit="${u}" class="${unit === u ? "on" : ""}">°${u}</button>`).join("")}</div>
      <div class="field-label">Card style</div><div class="segmented" id="style-seg"><button type="button" data-cstyle="standard" class="${cardStyle === "standard" ? "on" : ""}">${icon("dashboard")}Standard</button><button type="button" data-cstyle="minimal" class="${cardStyle === "minimal" ? "on" : ""}">${icon("view_agenda")}Minimal</button></div>
      <div class="field-label">Layout</div><div class="segmented" id="layout-seg"><button type="button" data-layout="cards" class="${mode === "cards" ? "on" : ""}">${icon("grid_view")}Cards</button><button type="button" data-layout="table" class="${mode === "table" ? "on" : ""}">${icon("table_rows")}Table</button></div>
    </section>`;
    const warn = settingsData.auth ? "" : `<div class="notice">${icon("key")}<span>Anyone on your network can open this page and change accounts. Set <code>DASH_TOKEN</code> in docker-compose.yml to require a password.</span></div>`;
    const cards = settingsData.sections.map(sec => {
      const st = sec.status || (sec.enabled ? "starting" : "off");
      const tone = /^ok/.test(st) ? "good" : /error|needs|refused|failed/i.test(st) ? "bad" : sec.enabled ? "" : "off";
      const push = sec.push_info ? `<p class="hint">${icon("info")}<span>Gateway settings: Server <b>${esc(location.hostname)}</b>, Port <b>${esc(location.port || (location.protocol === "https:" ? 443 : 80))}</b>, Path <b>/data/report</b>, Protocol <b>Ecowitt</b></span></p>` : "";
      const loginBtn = sec.login ? `<button type="button" class="tonal-btn" data-login="${sec.login}">${icon("login")}${sec.login === "hue" ? "Pair bridge" : "Sign in"}</button>` : "";
      return `<section class="set-card" data-section="${sec.id}">
        <div class="set-head"><span class="avatar">${icon(sec.icon)}</span><div><h3>${esc(sec.label)}</h3><span class="status-chip ${tone}">${esc(st)}</span></div>
          <button type="button" class="m3-switch ${sec.enabled ? "on" : ""}" data-enable role="switch" aria-checked="${sec.enabled}" aria-label="Enable ${esc(sec.label)}"></button></div>
        <p class="set-help">${esc(sec.help)}</p>${push}
        <div class="set-fields">${sec.fields.map(f => fieldHtml(sec, f)).join("")}</div>
        <div class="set-actions">${loginBtn}<button type="button" class="filled-btn" data-save>${icon("save")}Save</button></div>
      </section>`;
    }).join("");
    $("#rooms").innerHTML = `<div class="settings-wrap">${warn}${appearance}${cards}
      <p class="muted small">Accounts are saved to <code>${esc(settingsData.file)}</code> on the server and override the same values in config.yaml. Passwords are never sent back to the browser.</p></div>`;
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
    if (view === "battery") return batteryPage(list);
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
    $("#subtitle").textContent = (view === "all" ? `${appTitle} · ` : "") + now.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" }) +
      " · " + now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    $$("#nav [data-f]").forEach(b => b.classList.toggle("on", b.dataset.f === view));
    $(".more-btn").classList.toggle("on", !$(`#nav [data-f="${view}"][data-phone]`));
    $$("#more [data-f]").forEach(b => b.classList.toggle("on", b.dataset.f === view));
    const vm = $("#viewmode"); vm.innerHTML = icon(mode === "table" ? "grid_view" : "table_rows");
    vm.title = vm.ariaLabel = mode === "table" ? "Card view" : "Table view";
  }
  function go(v) {
    view = v; store("filter", v); document.body.classList.remove("quiet");
    if (v === "settings") { $("#rooms").innerHTML = ""; loadSettings(); }
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
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws${token ? "?token=" + encodeURIComponent(token) : ""}`);
    ws.onopen = () => $("#conn").classList.add("on");
    ws.onclose = e => { $("#conn").classList.remove("on"); if (e.code === 4401) return askToken(); setTimeout(connect, 3000); };
    ws.onmessage = e => {
      const m = JSON.parse(e.data);
      if (m.type === "layout") { layout = m.layout; if (!dragging) schedule(); return; }
      if (m.type === "snapshot") { if (m.layout) layout = m.layout; devices.clear(); m.devices.forEach(d => devices.set(d.id, d)); schedule(); loadSparks(); if (view === "power") loadPower(); }
      else if (m.type === "device") { devices.set(m.device.id, m.device); scheduleQuiet(); }
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
  function loginFlow(kind) {
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
  const fmtTime = (t, span) => new Date(t * 1000).toLocaleString([], span > 2 * 86400 ? { month: "short", day: "numeric" } : { hour: "numeric", minute: "2-digit" });
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
      const url = URL.createObjectURL(await r.blob());
      $("#snap-frame").innerHTML = `<img src="${url}" alt="Snapshot from ${esc(d?.name)}"><small>${new Date().toLocaleTimeString()}</small>`;
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
      if (b.classList.contains("on")) return;
      if (["lock", "valve", "action", "alarm_panel"].includes(capName)) {
        const ic = { lock: value === "lock" ? "lock" : "lock_open", valve: "valve", action: value === "siren" ? "campaign" : "detector_smoke", alarm_panel: "shield" }[capName];
        const msg = capName === "action" ? (value === "siren" ? "The siren will sound for 30 seconds." : "The alarm will sound briefly.") : "This controls a physical device in your home.";
        if (!(await confirmAction(`${VERB[value] || value} ${d.name}?`, msg, ic))) return;
      }
      b.disabled = true;
      try {
        await api(`/api/devices/${encodeURIComponent(id)}/command`, { method: "POST", body: JSON.stringify({ capability: capName, value }) });
        snack(["switch", "light"].includes(capName) ? `${d.name} ${capName === "light" ? "light " : ""}turned ${value ? "on" : "off"}` : `${VERB[value] || value} sent to ${d.name}`);
      } catch (err) { snack(`Couldn't reach ${d.name}: ${err.message}`); }
      finally { b.disabled = false; }
      return;
    }
    if (t.closest("[data-snapshot]")) return showSnapshot(t.closest("[data-id]").dataset.id);
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
    const df = t.closest("[data-devfilter]"); if (df) { devFilter = df.dataset.devfilter; devicesPage(); return; }
    if (t.closest("[data-new-view]")) { if ($("#more").open) $("#more").close(); return openViewEditor(null); }
    if (t.closest("[data-edit-view]") || t.closest("#editview")) return openViewEditor(customView());
    const nav = t.closest("[data-f]");
    if (nav) { if (nav.closest("#more")) $("#more").close(); go(nav.dataset.f); return; }
    if (t.closest("[data-open=more]")) { hydrateIcons($("#more")); $("#more").showModal(); return; }
    if (t.closest("[data-goto]")) return go(t.closest("[data-goto]").dataset.goto);
    // settings page
    const card = t.closest(".set-card[data-section]");
    if (card) {
      if (t.closest("[data-enable]")) { const sw = t.closest("[data-enable]"); sw.classList.toggle("on"); sw.ariaChecked = sw.classList.contains("on"); return; }
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
    catch (err) { snack(`Couldn't reach ${d?.name || "device"}: ${err.message}`); if (!quiet) throw err; }
  }
  document.addEventListener("input", e => {
    if (e.target.id === "devsearch") { devQuery = e.target.value; renderDeviceList(); return; }
    const r = e.target.closest("[data-slider]"); if (!r) return;
    const pct = (r.value - r.min) / (r.max - r.min) * 100; r.style.setProperty("--pct", pct + "%");
    r.nextElementSibling.textContent = r.dataset.slider === "color_temp" ? `${r.value}K` : `${r.value}%`;
    sliding = true;
  });
  document.addEventListener("change", e => {
    const r = e.target.closest("[data-slider]");
    if (r) { sliding = false; sendCmd(r.closest("[data-id]").dataset.id, r.dataset.slider, +r.value); return; }
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
      layout = { rooms: [], order: {}, pinned: [] }; await saveLayout(); schedule(); snack("Layout reset"); }
  });
  $("#viewmode").onclick = () => { mode = mode === "table" ? "cards" : "table"; store("viewmode", mode); document.body.classList.remove("quiet"); schedule(); };
  addEventListener("scroll", () => $(".appbar").classList.toggle("scrolled", scrollY > 24), { passive: true });

  hydrateIcons();
  document.body.dataset.cardStyle = cardStyle;
  applyMode(load("mode", "auto"));
  fetch("/api/config", { headers: token ? { Authorization: `Bearer ${token}` } : {} }).then(r => r.json()).then(c => {
    if (c.ui?.title) { appTitle = c.ui.title; document.title = appTitle; renderHeader(); } }).catch(() => {});
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
