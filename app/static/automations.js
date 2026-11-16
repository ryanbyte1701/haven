/* Automations page: a table of "when / if / then" rules plus a plain-language editor.
   Registers itself through the small page API that app.js exposes as window.Haven. */
(() => {
  const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
  const DAY_LABEL = { mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun" };
  const BOOL_CAPS = new Set(["light", "switch", "leak", "motion", "presence", "smoke", "co", "heat", "battery_low", "vibration",
    "tamper", "ding", "charging", "replace_battery", "overload", "watering"]);
  const KNOWN_VALUES = {
    contact: ["open", "closed"], lock: ["locked", "unlocked"], valve: ["open", "closed"],
    ups_status: ["online", "on_battery", "low_battery"], alarm_panel: ["disarmed", "armed_home", "armed_away", "triggered"],
    vacuum_state: ["cleaning", "docked", "returning", "paused", "charging", "error"],
    charger_status: ["available", "plugged_in", "charging", "done", "fault"],
  };
  const CAP_LABEL = { contact: "door/window", temperature: "temperature", humidity: "humidity", light: "light", switch: "power",
    leak: "leak", motion: "motion", presence: "presence", lock: "lock", valve: "valve", battery: "battery", ups_status: "UPS status",
    alarm_panel: "alarm", co2: "CO₂", pm25: "PM2.5", moisture: "soil moisture", brightness: "brightness", color_temp: "color temperature" };
  const OPS = { changes: "changes", becomes: "becomes", above: "goes above", below: "drops below" };
  const COND_OPS = { equals: "is", above: "is above", below: "is below" };

  let H, root = null, items = [], runs = [], timeInfo = null, dlg = null, draft = null, pollTimer = null;

  const pretty = s => String(s ?? "").replace(/_/g, " ");
  const capName = c => CAP_LABEL[c] || pretty(c);
  const dev = id => H.devices.get(id);
  const devName = id => dev(id)?.name || id;
  const isBoolCap = (d, c) => BOOL_CAPS.has(c) || typeof d?.capabilities?.[c]?.value === "boolean";
  const showVal = (v, c) => v === true || v === "true" ? (c === "leak" ? "detected" : "on") : v === false || v === "false" ? (c === "leak" ? "dry" : "off") : pretty(v);
  const unitOf = (id, c) => { const u = dev(id)?.capabilities?.[c]?.unit; return u ? ` ${u}` : ""; };

  function daysText(days) {
    if (!days || !days.length || days.length === 7) return "every day";
    const s = DAYS.filter(d => days.includes(d)).join(",");
    if (s === "mon,tue,wed,thu,fri") return "weekdays";
    if (s === "sat,sun") return "weekends";
    return DAYS.filter(d => days.includes(d)).map(d => DAY_LABEL[d]).join(", ");
  }
  function whenText(t) {
    if (t.type === "device") {
      const v = t.op === "changes" ? "" : ` ${showVal(t.value, t.capability)}${["above", "below"].includes(t.op) ? unitOf(t.device_id, t.capability) : ""}`;
      return `${devName(t.device_id)} ${capName(t.capability)} ${OPS[t.op] || t.op}${v}`;
    }
    if (t.type === "time") return `At ${t.at}, ${daysText(t.days)}`;
    if (t.type === "interval") return `Every ${t.minutes} min`;
    return "—";
  }
  function ifText(conds) {
    if (!conds?.length) return "";
    return conds.map(c => c.type === "time" ? `between ${c.after || "00:00"} and ${c.before || "24:00"}`
      : c.type === "days" ? `on ${daysText(c.days)}`
      : `${devName(c.device_id)} ${capName(c.capability)} ${COND_OPS[c.op] || "is"} ${showVal(c.value, c.capability)}`).join(" and ");
  }
  function thenText(acts) {
    return (acts || []).map(a => a.type === "command" ? `${devName(a.device_id)}: ${capName(a.capability)} → ${showVal(a.value, a.capability)}`
      : a.type === "notify" ? `Notify “${a.message}”` : a.type === "delay" ? `Wait ${a.seconds}s`
      : a.type === "webhook" ? `Call ${a.method} ${(() => { try { return new URL(a.url).host; } catch { return "webhook"; } })()}` : a.type).join(" · ");
  }

  // ------------------------------------------------------------ page
  async function load() {
    try { [items, runs, timeInfo] = await Promise.all([H.api("/api/automations"), H.api("/api/automations/log"), H.api("/api/time").catch(() => null)]); }
    catch (e) { items = []; runs = []; H.snack(`Couldn't load automations: ${e.message}`); }
    draw();
  }
  function render(el) {
    root = el;
    root._html = null;
    root.innerHTML = `<div class="auto-wrap"><p class="muted">Loading…</p></div>`;
    load();
    clearInterval(pollTimer);
    pollTimer = setInterval(() => { if (!root?.isConnected || root.dataset.plugin !== "automations") return clearInterval(pollTimer); refreshLog(); }, 10000);
  }
  async function refreshLog() {
    try { runs = await H.api("/api/automations/log"); items = await H.api("/api/automations"); draw(); } catch {}
  }
  function draw() {
    if (!root?.isConnected) return;
    const { icon, esc, ago } = H;
    const clock = timeInfo ? `<span class="auto-clock ${timeInfo.ok ? "" : "warn"}" title="Haven's clock (NTP corrected)">${icon("timer")}${esc(timeInfo.timezone)} · ${
      timeInfo.ok ? `clock offset ${timeInfo.offset_ms >= 0 ? "+" : "−"}${Math.abs(Math.round(timeInfo.offset_ms))} ms` : "NTP not synced"}</span>` : "";
    const rows = items.map(a => `<tr data-aid="${esc(a.id)}" class="${a.enabled ? "" : "off"}">
        <td data-label="On"><button class="m3-switch sm ${a.enabled ? "on" : ""}" role="switch" aria-checked="${a.enabled}" data-auto-toggle aria-label="Turn ${esc(a.name)} on or off"></button></td>
        <td data-label="Name" class="a-name"><b>${esc(a.name)}</b></td>
        <td data-label="When">${esc(whenText(a.trigger))}</td>
        <td data-label="Only if" class="${a.conditions?.length ? "" : "muted"}">${esc(ifText(a.conditions) || "Always")}</td>
        <td data-label="Then">${esc(thenText(a.actions))}</td>
        <td data-label="Last run" class="muted">${a.last_run ? esc(ago(a.last_run)) : "Never"}${a.run_count ? ` · ${a.run_count}×` : ""}</td>
        <td class="a-actions"><button class="icon-btn" data-auto-run title="Run now (skips the conditions)">${icon("bolt")}</button>
          <button class="icon-btn" data-auto-edit title="Edit">${icon("edit")}</button>
          <button class="icon-btn" data-auto-del title="Delete">${icon("delete")}</button></td></tr>`).join("");
    const table = items.length ? `<div class="table-wrap"><table class="dtable auto-table"><thead><tr><th>On</th><th>Name</th><th>When</th><th>Only if</th><th>Then</th><th>Last run</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`
      : `<div class="empty-state">${icon("bolt")}<h3>No automations yet</h3><p>For example: when the front door opens after 10 pm, notify me and turn on the porch light.</p></div>`;
    const log = runs.slice(0, 25).map(r => `<li class="${r.ok ? (r.skipped ? "skip" : "ok") : "bad"}">${icon(r.ok ? (r.skipped ? "info" : "check_circle") : "warning", true)}
        <div><b>${esc(r.name)}</b><small>${esc(r.reason)}${r.skipped ? ` · skipped: ${esc(r.skipped)}` : r.steps?.length ? ` · ${esc(r.steps.join(" · "))}` : ""}</small></div>
        <span class="muted">${esc(ago(r.ts))}</span></li>`).join("");
    const html = `<div class="auto-wrap">
      <div class="auto-top"><p class="muted">Rules that run on their own: <b>when</b> something happens, <b>if</b> the conditions hold, <b>then</b> do the steps.</p>
        ${clock}<button class="filled-btn" data-auto-new>${icon("add")}Add automation</button></div>
      ${table}
      <h3 class="auto-h">Recent runs</h3>
      ${log ? `<ul class="auto-log">${log}</ul>` : `<p class="muted small">Nothing has run yet.</p>`}
    </div>`;
    if (root._html === html) return;
    root._html = html; root.innerHTML = html;
  }

  // ------------------------------------------------------------ editor
  const blank = () => ({ name: "", enabled: true, cooldown: 0, trigger: { type: "device", device_id: "", capability: "", op: "becomes", value: "" }, conditions: [], actions: [] });
  function deviceOptions(sel, filter = () => true) {
    const list = [...H.devices.values()].filter(d => !d.hidden && filter(d))
      .sort((a, b) => (a.room || "~").localeCompare(b.room || "~") || a.name.localeCompare(b.name));
    return `<option value="">Choose a device…</option>` + list.map(d => `<option value="${H.esc(d.id)}" ${d.id === sel ? "selected" : ""}>${H.esc(d.name)}${d.room ? ` · ${H.esc(d.room)}` : ""}</option>`).join("");
  }
  function capOptions(did, sel, writableOnly = false) {
    const caps = Object.entries(dev(did)?.capabilities || {}).filter(([, c]) => !writableOnly || c.writable);
    return `<option value="">…</option>` + caps.map(([k]) => `<option value="${H.esc(k)}" ${k === sel ? "selected" : ""}>${H.esc(capName(k))}</option>`).join("");
  }
  /** value input that adapts to the capability: on/off, a list of options, a number, or free text */
  function valueInput(did, c, v, forCommand, key) {
    const d = dev(did), cp = d?.capabilities?.[c], esc = H.esc;
    if (!c) return `<input class="a-in" data-k="${key}" disabled placeholder="value">`;
    if (forCommand && cp?.options?.length)
      return `<select class="a-in" data-k="${key}">${cp.options.map(o => `<option value="${esc(o)}" ${String(v) === o ? "selected" : ""}>${esc(pretty(o))}</option>`).join("")}</select>`;
    if (isBoolCap(d, c)) {
      const on = v === true || v === "true", lbl = c === "leak" ? ["detected", "dry"] : ["on", "off"];
      return `<select class="a-in" data-k="${key}" data-bool><option value="true" ${on ? "selected" : ""}>${lbl[0]}</option><option value="false" ${!on ? "selected" : ""}>${lbl[1]}</option></select>`;
    }
    const known = KNOWN_VALUES[c];
    if (known && !forCommand) return `<select class="a-in" data-k="${key}">${known.map(o => `<option value="${o}" ${String(v) === o ? "selected" : ""}>${pretty(o)}</option>`).join("")}</select>`;
    const num = typeof cp?.value === "number" || cp?.min != null;
    return `<input class="a-in" data-k="${key}" ${num ? `type="number" step="any"${cp?.min != null ? ` min="${cp.min}"` : ""}${cp?.max != null ? ` max="${cp.max}"` : ""}` : ""}
      value="${esc(v ?? "")}" placeholder="${esc(cp?.value ?? "value")}${num && cp?.unit ? " " + esc(cp.unit) : ""}">`;
  }
  function dayChips(days, key) {
    return `<div class="a-days" data-k="${key}">${DAYS.map(d => `<button type="button" class="fchip ${days?.includes(d) ? "on" : ""}" data-day="${d}">${DAY_LABEL[d]}</button>`).join("")}
      <button type="button" class="text-btn sm" data-days-preset="mon,tue,wed,thu,fri">Weekdays</button><button type="button" class="text-btn sm" data-days-preset="sat,sun">Weekends</button></div>`;
  }
  function whenBlock() {
    const t = draft.trigger, esc = H.esc;
    const seg = `<div class="segmented a-seg" data-seg="trigger">${[["device", "A device changes", "sensors"], ["time", "At a time", "timer"], ["interval", "Repeatedly", "restart_alt"]].map(([k, l, ic]) =>
      `<button type="button" data-type="${k}" class="${t.type === k ? "on" : ""}">${H.icon(ic)}${l}</button>`).join("")}</div>`;
    let body = "";
    if (t.type === "device") body = `<div class="a-row">${`<select class="a-in grow" data-k="t.device_id">${deviceOptions(t.device_id)}</select>`}
        <select class="a-in" data-k="t.capability">${capOptions(t.device_id, t.capability)}</select>
        <select class="a-in" data-k="t.op">${Object.entries(OPS).map(([k, l]) => `<option value="${k}" ${t.op === k ? "selected" : ""}>${l}</option>`).join("")}</select>
        ${t.op === "changes" ? "" : valueInput(t.device_id, t.capability, t.value, false, "t.value")}</div>`;
    else if (t.type === "time") body = `<div class="a-row"><span>At</span><input class="a-in" type="time" data-k="t.at" value="${esc(t.at || "07:00")}"></div>${dayChips(t.days, "t.days")}
        <p class="muted small">Leave every day unselected to run daily.</p>`;
    else body = `<div class="a-row"><span>Every</span><input class="a-in" type="number" min="1" data-k="t.minutes" value="${esc(t.minutes || 15)}"><span>minutes</span></div>`;
    return `<section class="a-sec"><h4>When</h4>${seg}${body}</section>`;
  }
  function condRow(c, i) {
    const esc = H.esc;
    const type = `<select class="a-in" data-k="c.${i}.type"><option value="time" ${c.type === "time" ? "selected" : ""}>Time of day</option>
      <option value="days" ${c.type === "days" ? "selected" : ""}>Day of week</option><option value="device" ${c.type === "device" ? "selected" : ""}>A device is…</option></select>`;
    let f = "";
    if (c.type === "time") f = `<span>between</span><input class="a-in" type="time" data-k="c.${i}.after" value="${esc(c.after || "22:00")}"><span>and</span><input class="a-in" type="time" data-k="c.${i}.before" value="${esc(c.before || "06:00")}">`;
    else if (c.type === "days") f = dayChips(c.days, `c.${i}.days`);
    else f = `<select class="a-in grow" data-k="c.${i}.device_id">${deviceOptions(c.device_id)}</select><select class="a-in" data-k="c.${i}.capability">${capOptions(c.device_id, c.capability)}</select>
        <select class="a-in" data-k="c.${i}.op">${Object.entries(COND_OPS).map(([k, l]) => `<option value="${k}" ${c.op === k ? "selected" : ""}>${l}</option>`).join("")}</select>${valueInput(c.device_id, c.capability, c.value, false, `c.${i}.value`)}`;
    return `<div class="a-item">${type}<div class="a-row">${f}</div><button type="button" class="icon-btn a-del" data-del="c.${i}" title="Remove">${H.icon("delete")}</button></div>`;
  }
  function actRow(a, i) {
    const esc = H.esc;
    const type = `<select class="a-in" data-k="a.${i}.type">${[["command", "Control a device"], ["notify", "Send a notification"], ["delay", "Wait"], ["webhook", "Call a webhook"]].map(([k, l]) =>
      `<option value="${k}" ${a.type === k ? "selected" : ""}>${l}</option>`).join("")}</select>`;
    let f = "";
    if (a.type === "command") f = `<select class="a-in grow" data-k="a.${i}.device_id">${deviceOptions(a.device_id, d => Object.values(d.capabilities).some(c => c.writable))}</select>
        <select class="a-in" data-k="a.${i}.capability">${capOptions(a.device_id, a.capability, true)}</select>${valueInput(a.device_id, a.capability, a.value, true, `a.${i}.value`)}`;
    else if (a.type === "notify") f = `<input class="a-in grow" data-k="a.${i}.message" value="${esc(a.message || "")}" placeholder="Message, e.g. Front door opened">`;
    else if (a.type === "delay") f = `<input class="a-in" type="number" min="0" max="3600" data-k="a.${i}.seconds" value="${esc(a.seconds ?? 30)}"><span>seconds</span>`;
    else f = `<select class="a-in" data-k="a.${i}.method"><option ${a.method !== "GET" ? "selected" : ""}>POST</option><option ${a.method === "GET" ? "selected" : ""}>GET</option></select>
        <input class="a-in grow" data-k="a.${i}.url" value="${esc(a.url || "")}" placeholder="https://…">`;
    return `<div class="a-item">${type}<div class="a-row">${f}</div><button type="button" class="icon-btn a-del" data-del="a.${i}" title="Remove">${H.icon("delete")}</button></div>`;
  }
  function drawEditor() {
    const esc = H.esc;
    dlg.querySelector(".a-body").innerHTML = `
      <label class="textfield"><input data-k="name" value="${esc(draft.name)}" placeholder=" " maxlength="80"><span>Name</span></label>
      ${whenBlock()}
      <section class="a-sec"><h4>Only if <small>(optional)</small></h4>${draft.conditions.map(condRow).join("")}
        <button type="button" class="text-btn" data-add="cond">${H.icon("add")}Add condition</button></section>
      <section class="a-sec"><h4>Then</h4>${draft.actions.map(actRow).join("")}
        <button type="button" class="text-btn" data-add="act">${H.icon("add")}Add step</button></section>
      <details class="adv"><summary>Advanced</summary><div class="a-row"><span>Don't run again for</span>
        <input class="a-in" type="number" min="0" data-k="cooldown" value="${esc(draft.cooldown || 0)}"><span>seconds after it runs</span></div></details>
      <p class="a-err" hidden></p>`;
  }
  function setPath(path, value) {
    const [head, i, k] = path.split(".");
    if (path === "name" || path === "cooldown") { draft[path] = path === "cooldown" ? Number(value) || 0 : value; return; }
    if (head === "t") { draft.trigger[i] = value; return; }
    const list = head === "c" ? draft.conditions : draft.actions;
    list[+i][k] = value;
  }
  function coerce(el) {
    if (el.dataset.bool !== undefined) return el.value === "true";
    if (el.type === "number") return el.value === "" ? "" : Number(el.value);
    return el.value;
  }
  function openEditor(existing) {
    if (!dlg) buildDialog();
    draft = existing ? JSON.parse(JSON.stringify(existing)) : blank();
    if (!existing && !draft.actions.length) draft.actions.push({ type: "notify", message: "" });
    dlg.querySelector("h2").textContent = existing ? "Edit automation" : "New automation";
    drawEditor(); dlg.showModal();
  }
  function buildDialog() {
    dlg = document.createElement("dialog");
    dlg.className = "m3-dialog wide auto-dialog";
    dlg.innerHTML = `<form method="dialog"><h2>New automation</h2><div class="a-body"></div>
      <div class="dlg-actions"><button class="text-btn" value="cancel" formnovalidate>Cancel</button><button class="text-btn" value="save" data-auto-save>Save</button></div></form>`;
    document.body.appendChild(dlg);
    dlg.addEventListener("change", e => {
      const el = e.target.closest("[data-k]"); if (!el || !el.matches("input, select")) return;
      const k = el.dataset.k; setPath(k, coerce(el));
      // changing a device or type resets dependent fields
      if (/device_id$/.test(k)) { setPath(k.replace("device_id", "capability"), ""); setPath(k.replace("device_id", "value"), ""); }
      if (/capability$/.test(k)) setPath(k.replace("capability", "value"), "");
      if (/^[ca]\.\d+\.type$/.test(k)) { const [h, i] = k.split("."); const list = h === "c" ? draft.conditions : draft.actions; list[+i] = { type: el.value }; }
      if (k === "t.op" || /device_id$|capability$|type$/.test(k)) drawEditor();
    });
    dlg.addEventListener("input", e => { const el = e.target.closest("input[data-k]"); if (el && !["time", "number"].includes(el.type)) setPath(el.dataset.k, el.value); });
    dlg.addEventListener("click", async e => {
      const t = e.target;
      const seg = t.closest('[data-seg="trigger"] [data-type]');
      if (seg) { const type = seg.dataset.type;
        draft.trigger = type === "device" ? { type, device_id: "", capability: "", op: "becomes", value: "" } : type === "time" ? { type, at: "07:00", days: [] } : { type, minutes: 15 };
        return drawEditor(); }
      const day = t.closest("[data-day]");
      if (day) { const key = day.closest("[data-k]").dataset.k; const cur = key === "t.days" ? draft.trigger.days || [] : draft.conditions[+key.split(".")[1]].days || [];
        const next = cur.includes(day.dataset.day) ? cur.filter(x => x !== day.dataset.day) : [...cur, day.dataset.day];
        setPath(key, next); return drawEditor(); }
      const pre = t.closest("[data-days-preset]");
      if (pre) { setPath(pre.closest("[data-k]").dataset.k, pre.dataset.daysPreset.split(",")); return drawEditor(); }
      const add = t.closest("[data-add]");
      if (add) { if (add.dataset.add === "cond") draft.conditions.push({ type: "time", after: "22:00", before: "06:00" });
        else draft.actions.push({ type: "command", device_id: "", capability: "", value: "" }); return drawEditor(); }
      const del = t.closest("[data-del]");
      if (del) { const [h, i] = del.dataset.del.split("."); (h === "c" ? draft.conditions : draft.actions).splice(+i, 1); return drawEditor(); }
      if (t.closest("[data-auto-save]")) {
        e.preventDefault();
        // pull any text typed but not yet "changed"
        dlg.querySelectorAll("input[data-k], select[data-k]").forEach(el => setPath(el.dataset.k, coerce(el)));
        draft.conditions.forEach(c => { if (c.type === "time") { c.after ||= "22:00"; c.before ||= "06:00"; } });
        if (draft.trigger.type === "time") draft.trigger.at ||= "07:00";
        if (!draft.name.trim()) draft.name = whenText(draft.trigger).slice(0, 60) || "Automation";
        const err = dlg.querySelector(".a-err");
        try {
          const saved = await H.api(draft.id ? `/api/automations/${encodeURIComponent(draft.id)}` : "/api/automations",
            { method: draft.id ? "PUT" : "POST", body: JSON.stringify(draft) });
          dlg.close(); H.snack(`Saved “${saved.name}”`); load();
        } catch (er) { err.textContent = er.message; err.hidden = false; }
      }
    });
  }

  // ------------------------------------------------------------ page actions
  document.addEventListener("click", async e => {
    const t = e.target;
    if (t.closest("[data-auto-new]")) return openEditor(null);
    const row = t.closest("tr[data-aid]"); if (!row) return;
    const a = items.find(x => x.id === row.dataset.aid); if (!a) return;
    if (t.closest("[data-auto-edit]")) return openEditor(a);
    if (t.closest("[data-auto-toggle]")) {
      try { await H.api(`/api/automations/${encodeURIComponent(a.id)}`, { method: "PUT", body: JSON.stringify({ ...a, enabled: !a.enabled }) });
        H.snack(`${a.name} ${a.enabled ? "paused" : "turned on"}`); load(); } catch (er) { H.snack(er.message); }
      return;
    }
    if (t.closest("[data-auto-run]")) {
      try { const r = await H.api(`/api/automations/${encodeURIComponent(a.id)}/run`, { method: "POST" });
        H.snack(r.started && r.steps == null ? `Running “${a.name}”…` : r.ok ? `Ran “${a.name}”` : `“${a.name}” failed: ${(r.steps || []).slice(-1)[0] || r.reason || "error"}`);
        refreshLog(); setTimeout(refreshLog, 2000); } catch (er) { H.snack(er.message); }
      return;
    }
    if (t.closest("[data-auto-del]") && await H.confirmAction(`Delete “${a.name}”?`, "This automation stops running and is removed.", "delete")) {
      try { await H.api(`/api/automations/${encodeURIComponent(a.id)}`, { method: "DELETE" }); H.snack("Automation deleted"); load(); } catch (er) { H.snack(er.message); }
    }
  });

  // ------------------------------------------------------------ notifications (any page)
  function listen() {
    if (H.onMessage) {   // share the dashboard's WebSocket instead of opening a second one
      H.onMessage(m => { if (m.type !== "notification") return;
        H.snack(`${m.title ? m.title + ": " : ""}${m.message}`);
        if (root?.isConnected && root.dataset.plugin === "automations") refreshLog(); });
      return;
    }
    let token = ""; try { token = localStorage.getItem("token") || ""; } catch {}
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws${token ? "?token=" + encodeURIComponent(token) : ""}`);
    ws.onmessage = ev => {
      let m; try { m = JSON.parse(ev.data); } catch { return; }
      if (m.type !== "notification") return;
      H.snack(`${m.title ? m.title + ": " : ""}${m.message}`);
      if (root?.isConnected && root.dataset.plugin === "automations") refreshLog();
    };
    ws.onclose = e => { if (e.code !== 4401) setTimeout(listen, 5000); };
  }

  function init() {
    H = window.Haven;
    H.registerPage("automations", { label: "Automations", icon: "bolt", title: "Automations", render, onDevices() {} });
    listen();
  }
  if (window.Haven) init(); else document.addEventListener("haven:ready", init, { once: true });
})();
