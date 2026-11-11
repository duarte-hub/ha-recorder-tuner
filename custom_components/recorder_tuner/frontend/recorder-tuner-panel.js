/* Recorder Tuner panel – no build step, plain web component. */

const NOISY_DEVICE_CLASSES = new Set([
  "voltage", "current", "power_factor", "apparent_power", "reactive_power",
  "signal_strength", "sound_pressure", "frequency", "data_rate", "irradiance",
]);
const NOISY_PATTERNS = [
  [/packets_(in|out)/, "Network packet counter"],
  [/_pressure_(some|full)/, "Kernel pressure stat"],
  [/network_throughput|network_(in|out)_/, "Network throughput/counter"],
  [/_load_(1|5|15)_min/, "Load average"],
  [/memory_(free|use)$/, "Raw memory figure (keep the % sensor)"],
  [/(_rssi|_linkquality|_lqi)$/, "Radio signal diagnostic"],
  [/(_uptime|_last_seen)$/, "Uptime/last-seen diagnostic"],
  [/_(cpu|dram|package)_power$/, "CPU/RAM power diagnostic"],
];
const PROTECT_DEVICE_CLASSES = new Set(["energy", "gas", "water", "monetary"]);

const HINT_DOMAINS = new Set(["update", "image", "event", "button", "scene", "conversation", "tts", "stt", "wake_word", "ai_task", "notify", "remote", "camera"]);
const BIG_DOMAINS = new Set(["sensor", "binary_sensor", "light", "switch", "climate", "cover", "media_player", "input_number", "input_boolean", "number", "select"]);
const PRESENCE_DOMAINS = new Set(["person", "device_tracker", "zone", "alarm_control_panel", "lock"]);

const WINDOWS = [[6, "6 h"], [24, "24 h"], [72, "3 days"], [168, "7 days"]];

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const fmtInt = (n) => Math.round(n).toLocaleString();
const fmtBytes = (b) => {
  if (!b) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${b.toFixed(i ? 1 : 0)} ${u[i]}`;
};

class RecorderTunerPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hours = 24;
    this._threshold = 1000;
    this._filter = "suggested";
    this._data = null;
    this._ticked = new Set();
    this._tickedDomains = new Set();
    this._tab = "entities";
    this._loading = false;
    this._error = null;
    this._notice = null;
    this._confirmSave = false;
    this._confirm = null;
    this._busy = false;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    const mb = this.shadowRoot.querySelector("ha-menu-button");
    if (mb) { mb.hass = hass; mb.narrow = this._narrow; }
    if (first) { this._render(); this._load(); }
  }

  set narrow(n) {
    this._narrow = n;
    const mb = this.shadowRoot.querySelector("ha-menu-button");
    if (mb) mb.narrow = n;
  }

  // ------------------------------------------------------------------ data

  async _load() {
    this._loading = true;
    this._error = null;
    this._confirmSave = false;
    this._render();
    try {
      this._data = await this._hass.callWS({ type: "recorder_tuner/analyze", hours: this._hours, limit: 500 });
      this._resetTicks(true);
    } catch (e) {
      this._error = e.message || String(e);
    }
    this._loading = false;
    this._render();
  }

  _classify(e) {
    const id = e.entity_id;
    const domain = id.split(".")[0];
    if (e.energy) return { suggest: false, protect: true, reason: "Used by the Energy dashboard" };
    if (["total", "total_increasing"].includes(e.state_class) || PROTECT_DEVICE_CLASSES.has(e.device_class))
      return { suggest: false, protect: true, reason: "Meter/total – needed for long-term statistics" };
    if (["person", "device_tracker", "zone", "alarm_control_panel", "lock"].includes(domain))
      return { suggest: false, protect: true, reason: "Presence/security history" };
    if (["automation", "script"].includes(domain))
      return { suggest: false, reason: "Automation/script run history – a chatty one may be worth fixing instead" };
    const t = this._threshold;
    if (e.per_day < t) return { suggest: false, reason: "Below threshold" };
    if (NOISY_DEVICE_CLASSES.has(e.device_class))
      return { suggest: true, reason: `Electrical/diagnostic detail (${e.device_class.replace(/_/g, " ")})` };
    for (const [re, why] of NOISY_PATTERNS) if (re.test(id)) return { suggest: true, reason: why };
    if (e.device_class === "power") {
      return e.per_day >= t * 20
        ? { suggest: false, reason: "Real power – usually worth keeping; consider throttling at source" }
        : { suggest: false, reason: "Real power – history usually useful" };
    }
    if (e.per_day >= t * 5) return { suggest: true, reason: "Very chatty" };
    return { suggest: false, reason: "Chatty – review" };
  }

  _status(e) {
    if (e.in_yaml) return e.recorded ? ["pending", "Excluded after restart"] : ["excluded", "Excluded"];
    if (!e.recorded)
      return this._data?.yaml?.domains?.includes(e.entity_id.split(".")[0]) ? ["other", "Excluded by domain"] : ["other", "Excluded by glob/domain rule"];
    return ["recorded", "Recorded"];
  }

  _resetTicks(withSuggestions) {
    this._ticked = new Set();
    this._keepDays = this._currentKeepDays();
    this._tickedDomains = new Set((this._data?.domains || []).filter((d) => d.in_yaml).map((d) => d.domain));
    for (const e of this._data?.entities || []) {
      if (e.in_yaml) this._ticked.add(e.entity_id);
      else if (withSuggestions && e.recorded && this._classify(e).suggest) this._ticked.add(e.entity_id);
    }
  }

  _currentKeepDays() {
    const d = this._data;
    return d?.yaml?.purge_keep_days ?? d?.keep_days_running ?? 10;
  }

  _changes() {
    const add = [], remove = [];
    for (const e of this._data?.entities || []) {
      if (!e.in_yaml && !e.recorded) continue; // excluded by a rule we don't manage
      const want = this._ticked.has(e.entity_id);
      if (want && !e.in_yaml) add.push(e.entity_id);
      if (!want && e.in_yaml) remove.push(e.entity_id);
    }
    const addDomains = [], removeDomains = [];
    for (const d of this._data?.domains || []) {
      const want = this._tickedDomains.has(d.domain);
      if (want && !d.in_yaml) addDomains.push(d.domain);
      if (!want && d.in_yaml) removeDomains.push(d.domain);
    }
    const cur = this._currentKeepDays();
    const keepDays = this._keepDays && this._keepDays !== cur ? this._keepDays : null;
    return { add, remove, addDomains, removeDomains, keepDays };
  }

  _domainInfo(d) {
    if (d.energy) return { lock: true, reason: `Contains ${d.energy} Energy dashboard entit${d.energy === 1 ? "y" : "ies"} – can't be excluded as a whole` };
    if (PRESENCE_DOMAINS.has(d.domain)) return { reason: "Presence/security history – usually worth keeping" };
    if (BIG_DOMAINS.has(d.domain)) return { reason: `Excludes history and statistics for all ${d.entities} ${d.domain} entities – prefer excluding individual entities` };
    if (HINT_DOMAINS.has(d.domain)) return { reason: "Commonly excluded – history rarely useful" };
    return { reason: "" };
  }

  _domainStatus(d) {
    if (d.in_yaml) return d.recorded ? ["pending", "Excluded after restart"] : ["excluded", "Excluded"];
    if (!d.sampled) return ["recorded", "No entities"];
    if (d.recorded === 0) return ["other", "Excluded by other rules"];
    if (d.recorded < d.sampled) return ["other", `Partly excluded (${d.sampled - d.recorded} of ${d.sampled})`];
    return ["recorded", "Recorded"];
  }

  async _save() {
    const { add, remove, addDomains, removeDomains, keepDays } = this._changes();
    this._busy = true; this._render();
    try {
      const r = await this._hass.callWS({ type: "recorder_tuner/save", add, remove, add_domains: addDomains, remove_domains: removeDomains, ...(keepDays ? { keep_days: keepDays } : {}) });
      this._notice = r.changed
        ? { kind: "ok", saved: r, text: `Saved ${r.file}: entities +${r.added.length} / −${r.removed.length}, domains +${r.added_domains.length} / −${r.removed_domains.length}${r.keep_days ? `, history ${r.keep_days[0] ?? "default"} → ${r.keep_days[1]} days` : ""}. Backup: ${r.backup.split("/").pop()}. Restart Home Assistant to apply.` }
        : { kind: "info", text: "Nothing to change." };
      this._confirmSave = false;
      this._busy = false;
      await this._load();
    } catch (e) {
      this._notice = { kind: "error", text: e.message || String(e) };
      this._busy = false;
      this._render();
    }
  }

  _excludedIds() {
    return (this._data?.entities || []).filter((e) => e.in_yaml).map((e) => e.entity_id);
  }

  // when: "now" | "after_restart"
  async _purge(when) {
    this._busy = true; this._confirm = null; this._render();
    try {
      const r = await this._hass.callWS({ type: "recorder_tuner/purge", when });
      this._notice = r.restarting
        ? { kind: "ok", text: "Restarting… Once Home Assistant is back, stored history of every entity that is no longer recorded (by entity, domain or glob rule) is purged and the database repacked; a notification confirms it. Reload this page in a minute or two." }
        : { kind: "ok", text: `Purge queued for ${r.queued} no-longer-recorded entities, then a repack (SQLite VACUUM). The file shrinks when the recorder finishes, which can take several minutes on a large database.` };
    } catch (e) {
      this._notice = { kind: "error", text: e.message || String(e) };
    }
    this._busy = false;
    this._render();
  }

  async _restart() {
    this._busy = true; this._confirm = null; this._render();
    try {
      await this._hass.callService("homeassistant", "restart", {});
      this._notice = { kind: "info", text: "Restarting… reload this page in a minute or two." };
    } catch (e) {
      this._notice = { kind: "error", text: e.message || String(e) };
    }
    this._busy = false;
    this._render();
  }

  _actionButton(act, label, confirmLabel, primary = false) {
    if (this._confirm === act)
      return `<button class="primary" data-act="${act}-go" ${this._busy ? "disabled" : ""}>${confirmLabel}</button><button data-act="confirm-cancel">Cancel</button>`;
    return `<button class="${primary ? "primary" : ""}" data-act="${act}" ${this._busy ? "disabled" : ""}>${label}</button>`;
  }

  // ------------------------------------------------------------------ render

  _render() {
    const d = this._data;
    const { add, remove, addDomains, removeDomains, keepDays } = this._changes();
    const nChanges = add.length + remove.length + addDomains.length + removeDomains.length + (keepDays ? 1 : 0);
    const curKeep = this._currentKeepDays();
    const rows = (d?.entities || []).map((e) => ({ e, c: this._classify(e), s: this._status(e) }));
    let shown = rows;
    if (this._filter === "suggested") shown = rows.filter((r) => r.c.suggest || r.e.in_yaml || this._ticked.has(r.e.entity_id));
    else if (this._filter === "chatty") shown = rows.filter((r) => r.e.per_day >= this._threshold || r.e.in_yaml);
    const total = d?.total_per_day || 0;
    const tickedDom = this._tickedDomains;
    const tickedPerDay =
      (d?.domains || []).filter((x) => tickedDom.has(x.domain) && x.recorded).reduce((s, x) => s + x.per_day, 0) +
      rows.filter((r) => this._ticked.has(r.e.entity_id) && r.e.recorded && !tickedDom.has(r.e.entity_id.split(".")[0])).reduce((s, r) => s + r.e.per_day, 0);
    const pendingExcluded = d?.purgeable || 0;
    const domainRows = [...(d?.domains || [])].sort((a, b) => b.per_day - a.per_day);

    this.shadowRoot.innerHTML = `
      <style>${STYLE}</style>
      <div class="toolbar">
        <ha-menu-button></ha-menu-button>
        <div class="title">Recorder Tuner</div>
      </div>
      <div class="content">
        ${this._error ? `<div class="notice error">${esc(this._error)}</div>` : ""}
        ${this._notice ? `<div class="notice ${this._notice.kind}">${esc(this._notice.text)}
            ${this._notice.saved ? `<div class="actions">
                ${this._actionButton("restart-purge", "Restart & purge excluded", "Confirm restart + purge", true)}
                ${this._actionButton("restart", "Restart only", "Confirm restart")}
              </div>` : ""}
            <button class="x" data-act="dismiss" title="Dismiss">×</button></div>` : ""}
        ${d && !d.yaml.ok ? `<div class="notice error">Can't edit the recorder config: ${esc(d.yaml.error)}</div>` : ""}

        <div class="cards">
          <div class="stat"><div class="k">Database</div><div class="v">${fmtBytes(d?.db_size)}</div><div class="s">${esc(d?.db_engine || "")}</div></div>
          <div class="stat"><div class="k">State rows / day</div><div class="v">${d ? fmtInt(total) : "—"}</div><div class="s">${d ? `measured over ${d.hours_covered} h${d.hours_covered < d.hours_requested * 0.9 ? " (DB holds less – extrapolated)" : ""}` : ""}</div></div>
          <div class="stat"><div class="k">Ticked would save</div><div class="v">${d ? fmtInt(tickedPerDay) : "—"}</div><div class="s">${total ? `${((tickedPerDay / total) * 100).toFixed(1)}% of writes` : ""}</div></div>
          <div class="stat"><div class="k">Excluded in YAML</div><div class="v">${d?.yaml?.entities?.length ?? 0}<span class="unit"> entities</span> ${d?.yaml?.domains?.length ?? 0}<span class="unit"> domains</span></div><div class="s">${esc(d?.yaml?.file?.split("/").pop() || "")}${d?.yaml?.entity_globs?.length ? ` · ${d.yaml.entity_globs.length} glob(s)` : ""} · ${pendingExcluded} not recorded</div></div>
        </div>

        <div class="tabs">
          <button class="tab ${this._tab === "entities" ? "active" : ""}" data-tab="entities">Entities</button>
          <button class="tab ${this._tab === "domains" ? "active" : ""}" data-tab="domains">Domains</button>
        </div>

        <div class="controls">
          <label>Window
            <select data-ctl="hours">${WINDOWS.map(([h, l]) => `<option value="${h}" ${h === this._hours ? "selected" : ""}>${l}</option>`).join("")}</select>
          </label>
          <label>Suggest above
            <input data-ctl="threshold" type="number" min="50" step="50" value="${this._threshold}"> rows/day
          </label>
          <label class="${this._tab === "domains" ? "hide" : ""}">Show
            <select data-ctl="filter">
              <option value="suggested" ${this._filter === "suggested" ? "selected" : ""}>Suggested + excluded</option>
              <option value="chatty" ${this._filter === "chatty" ? "selected" : ""}>All above threshold</option>
              <option value="all" ${this._filter === "all" ? "selected" : ""}>Top ${rows.length}</option>
            </select>
          </label>
          <label title="recorder: purge_keep_days – how many days of detailed state history to keep. Long-term hourly statistics are kept regardless.">Keep history
            <input data-ctl="keepdays" type="number" min="1" max="365" step="1" value="${this._keepDays ?? curKeep}"> days
            ${keepDays ? `<span class="hint">${keepDays < curKeep ? `≈ ${Math.round((1 - keepDays / curKeep) * 100)}% less state history` : `≈ ${Math.round((keepDays / curKeep - 1) * 100)}% more state history`}</span>` : ""}
          </label>
          <span class="grow"></span>
          <button data-act="reload" ${this._loading ? "disabled" : ""}>${this._loading ? "Analysing…" : "Re-analyse"}</button>
          <button data-act="suggest">Tick suggestions</button>
          <button data-act="reset">Reset to saved</button>
        </div>

        <div class="savebar">
          <span>${nChanges ? `<b>Pending:</b> ${[
            add.length && `exclude ${add.length} entit${add.length === 1 ? "y" : "ies"}`,
            remove.length && `stop excluding ${remove.length} entit${remove.length === 1 ? "y" : "ies"}`,
            addDomains.length && `exclude domain${addDomains.length === 1 ? "" : "s"} ${addDomains.join(", ")}`,
            removeDomains.length && `stop excluding ${removeDomains.join(", ")}`,
            keepDays && `keep history ${curKeep} → ${keepDays} days`,
          ].filter(Boolean).join(" · ")}` : "No pending changes"}</span>
          <span class="grow"></span>
          ${pendingExcluded && !this._notice?.saved ? `
            <span title="recorder.purge_entities for the excluded entities, then recorder.purge with repack: true (compacts the file)">
              ${this._actionButton("purge", `Purge now (${pendingExcluded})`, `Confirm purge of ${pendingExcluded}`)}
              ${this._actionButton("restart-purge", "Restart & purge", "Confirm restart + purge")}
            </span>` : ""}
          ${this._confirmSave
            ? `<button class="primary" data-act="save-confirm" ${this._busy ? "disabled" : ""}>Confirm – write ${esc(d?.yaml?.file?.split("/").pop() || "configuration.yaml")}</button><button data-act="save-cancel">Cancel</button>`
            : `<button class="primary" data-act="save" ${nChanges ? "" : "disabled"} ${d?.yaml?.ok ? "" : "disabled"}>Save to configuration.yaml</button>`}
        </div>

        ${this._tab === "domains" ? this._domainTable(domainRows, total) : `<table>
          <thead><tr>
            <th class="cb"></th><th>Entity</th><th class="num">Rows/day</th><th class="num">Share</th>
            <th>Class</th><th>Status</th><th>Why</th>
          </tr></thead>
          <tbody>
            ${this._loading && !d ? `<tr><td colspan="7" class="empty">Querying the recorder…</td></tr>` : ""}
            ${d && !shown.length ? `<tr><td colspan="7" class="empty">Nothing matches – try "All above threshold" or lower the threshold.</td></tr>` : ""}
            ${shown.map(({ e, c, s }) => {
              const ticked = this._ticked.has(e.entity_id);
              const locked = !e.in_yaml && !e.recorded;
              const change = !locked && ticked !== e.in_yaml ? (ticked ? "adding" : "removing") : "";
              const share = total ? (e.per_day / total) * 100 : 0;
              return `<tr class="${change} ${c.protect ? "protect" : ""}">
                <td class="cb"><input type="checkbox" data-id="${esc(e.entity_id)}" ${ticked || locked ? "checked" : ""} ${locked ? "disabled" : ""}></td>
                <td><div class="name">${esc(e.name || e.entity_id)}</div><div class="id">${esc(e.entity_id)}${e.platform ? ` · ${esc(e.platform)}` : ""}</div></td>
                <td class="num">${fmtInt(e.per_day)}</td>
                <td class="num"><div class="bar"><span style="width:${Math.min(100, share * 4)}%"></span></div>${share.toFixed(1)}%</td>
                <td class="muted">${esc([e.device_class, e.unit].filter(Boolean).join(" · "))}</td>
                <td><span class="pill ${s[0]}">${s[1]}</span></td>
                <td class="muted">${c.suggest ? "<b>Suggested:</b> " : ""}${esc(c.reason)}</td>
              </tr>`;
            }).join("")}
          </tbody>
        </table>`}
        <p class="foot">Exclusions only stop <i>new</i> rows and need a full restart. <b>Restart &amp; purge</b> restarts, then deletes the excluded entities’ old rows and repacks (VACUUMs) the database so the file actually shrinks. A plain purge without repack frees space inside the file but doesn’t make it smaller. A timestamped backup of the edited file is kept next to it (last 5).</p>
      </div>`;

    const mb = this.shadowRoot.querySelector("ha-menu-button");
    if (mb) { mb.hass = this._hass; mb.narrow = this._narrow; }
    this._bind();
  }

  _domainTable(domainRows, total) {
    return `<table>
      <thead><tr>
        <th class="cb"></th><th>Domain</th><th class="num">Entities</th><th class="num">Rows/day</th><th class="num">Share</th>
        <th>Status</th><th>Note</th>
      </tr></thead>
      <tbody>
        ${!domainRows.length ? `<tr><td colspan="7" class="empty">${this._loading ? "Querying the recorder…" : "No domains"}</td></tr>` : ""}
        ${domainRows.map((x) => {
          const info = this._domainInfo(x);
          const st = this._domainStatus(x);
          const ticked = this._tickedDomains.has(x.domain);
          const locked = info.lock && !x.in_yaml;
          const change = ticked !== x.in_yaml ? (ticked ? "adding" : "removing") : "";
          const share = total ? (x.per_day / total) * 100 : 0;
          return `<tr class="${change} ${info.lock ? "protect" : ""}">
            <td class="cb"><input type="checkbox" data-domain="${esc(x.domain)}" ${ticked ? "checked" : ""} ${locked ? "disabled" : ""}></td>
            <td><div class="name">${esc(x.domain)}</div></td>
            <td class="num">${fmtInt(x.entities)}</td>
            <td class="num">${fmtInt(x.per_day)}</td>
            <td class="num"><div class="bar"><span style="width:${Math.min(100, share)}%"></span></div>${share.toFixed(1)}%</td>
            <td><span class="pill ${st[0]}">${st[1]}</span></td>
            <td class="muted">${esc(info.reason)}</td>
          </tr>`;
        }).join("")}
      </tbody>
    </table>`;
  }

  _bind() {
    const root = this.shadowRoot;
    root.querySelectorAll("input[type=checkbox][data-id]").forEach((cb) =>
      cb.addEventListener("change", () => {
        cb.checked ? this._ticked.add(cb.dataset.id) : this._ticked.delete(cb.dataset.id);
        this._confirmSave = false;
        this._render();
      }));
    root.querySelectorAll("input[type=checkbox][data-domain]").forEach((cb) =>
      cb.addEventListener("change", () => {
        cb.checked ? this._tickedDomains.add(cb.dataset.domain) : this._tickedDomains.delete(cb.dataset.domain);
        this._confirmSave = false;
        this._render();
      }));
    root.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => { this._tab = b.dataset.tab; this._render(); }));
    root.querySelector("[data-ctl=hours]")?.addEventListener("change", (ev) => { this._hours = Number(ev.target.value); this._load(); });
    root.querySelector("[data-ctl=threshold]")?.addEventListener("change", (ev) => {
      this._threshold = Math.max(50, Number(ev.target.value) || 1000);
      this._render();
    });
    root.querySelector("[data-ctl=keepdays]")?.addEventListener("change", (ev) => {
      const v = Math.round(Number(ev.target.value));
      this._keepDays = v >= 1 && v <= 365 ? v : this._currentKeepDays();
      this._confirmSave = false;
      this._render();
    });
    root.querySelector("[data-ctl=filter]")?.addEventListener("change", (ev) => { this._filter = ev.target.value; this._render(); });
    root.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => {
      switch (b.dataset.act) {
        case "reload": this._notice = null; this._load(); break;
        case "suggest": { const keep = this._tickedDomains; this._resetTicks(true); this._tickedDomains = keep; this._render(); break; }
        case "reset": this._resetTicks(false); this._confirmSave = false; this._render(); break;
        case "save": this._confirmSave = true; this._render(); break;
        case "save-cancel": this._confirmSave = false; this._render(); break;
        case "save-confirm": this._save(); break;
        case "purge": case "restart": case "restart-purge": this._confirm = b.dataset.act; this._render(); break;
        case "confirm-cancel": this._confirm = null; this._render(); break;
        case "purge-go": this._purge("now"); break;
        case "restart-purge-go": this._purge("after_restart"); break;
        case "restart-go": this._restart(); break;
        case "dismiss": this._notice = null; this._render(); break;
      }
    }));
  }
}

const STYLE = `
  :host { display:block; background:var(--primary-background-color); color:var(--primary-text-color); min-height:100vh; font-family:var(--paper-font-body1_-_font-family, Roboto, sans-serif); }
  .toolbar { display:flex; align-items:center; height:var(--header-height,56px); padding:0 12px; background:var(--app-header-background-color, var(--primary-color)); color:var(--app-header-text-color, #fff); border-bottom:var(--app-header-border-bottom, none); }
  .title { font-size:20px; margin-left:8px; }
  .content { padding:16px; max-width:1400px; margin:0 auto; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin-bottom:16px; }
  .stat { background:var(--card-background-color); border-radius:var(--ha-card-border-radius,12px); padding:12px 16px; box-shadow:var(--ha-card-box-shadow,none); border:1px solid var(--divider-color); }
  .stat .k { font-size:12px; color:var(--secondary-text-color); text-transform:uppercase; letter-spacing:.04em; }
  .stat .v { font-size:26px; font-weight:500; margin-top:4px; }
  .stat .s { font-size:12px; color:var(--secondary-text-color); min-height:1em; }
  .controls, .savebar { display:flex; flex-wrap:wrap; align-items:center; gap:12px; margin-bottom:12px; }
  .savebar { position:sticky; top:0; z-index:2; background:var(--card-background-color); border:1px solid var(--divider-color); border-radius:12px; padding:10px 14px; }
  .grow { flex:1; }
  label { font-size:14px; color:var(--secondary-text-color); display:flex; align-items:center; gap:6px; }
  select, input[type=number] { background:var(--card-background-color); color:var(--primary-text-color); border:1px solid var(--divider-color); border-radius:6px; padding:5px 8px; font:inherit; }
  input[type=number] { width:90px; }
  button { background:transparent; color:var(--primary-color); border:1px solid var(--primary-color); border-radius:18px; padding:6px 14px; font:inherit; font-size:14px; cursor:pointer; }
  button.primary { background:var(--primary-color); color:var(--text-primary-color,#fff); }
  button:disabled { opacity:.45; cursor:default; }
  table { width:100%; border-collapse:collapse; background:var(--card-background-color); border-radius:12px; overflow:hidden; border:1px solid var(--divider-color); }
  th, td { padding:8px 10px; border-bottom:1px solid var(--divider-color); text-align:left; vertical-align:middle; font-size:14px; }
  th { font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:var(--secondary-text-color); font-weight:500; }
  td.num, th.num { text-align:right; white-space:nowrap; }
  .cb { width:28px; }
  input[type=checkbox] { width:18px; height:18px; accent-color:var(--primary-color); cursor:pointer; }
  .name { font-weight:500; }
  .id { font-size:12px; color:var(--secondary-text-color); font-family:var(--code-font-family, monospace); word-break:break-all; }
  .muted { color:var(--secondary-text-color); font-size:13px; }
  tr.adding { background:rgba(var(--rgb-primary-color, 3,169,244), .10); }
  tr.removing { background:rgba(var(--rgb-warning-color, 255,152,0), .12); }
  tr.protect .name::after { content:" 🔒"; font-size:12px; }
  .bar { display:inline-block; width:50px; height:6px; background:var(--divider-color); border-radius:3px; margin-right:6px; vertical-align:middle; overflow:hidden; }
  .bar span { display:block; height:100%; background:var(--primary-color); }
  .pill { display:inline-block; border-radius:10px; padding:2px 8px; font-size:12px; white-space:nowrap; }
  .pill.recorded { background:var(--divider-color); }
  .pill.excluded { background:rgba(var(--rgb-success-color, 76,175,80), .2); }
  .pill.pending { background:rgba(var(--rgb-warning-color, 255,152,0), .25); }
  .pill.other { background:rgba(var(--rgb-info-color, 3,155,229), .2); }
  .notice { position:relative; padding:12px 40px 12px 14px; border-radius:10px; margin-bottom:12px; border:1px solid var(--divider-color); background:var(--card-background-color); }
  .notice.error { border-color:var(--error-color,#db4437); color:var(--error-color,#db4437); }
  .notice.ok { border-color:var(--success-color,#43a047); }
  .notice .actions { margin-top:8px; }
  .notice .x { position:absolute; top:6px; right:6px; border:none; font-size:18px; padding:2px 8px; color:inherit; }
  .tabs { display:flex; gap:4px; margin-bottom:12px; border-bottom:1px solid var(--divider-color); }
  .tab { border:none; border-radius:0; border-bottom:2px solid transparent; color:var(--secondary-text-color); padding:8px 16px; }
  .tab.active { color:var(--primary-color); border-bottom-color:var(--primary-color); }
  .hide { display:none; }
  .hint { font-size:12px; color:var(--primary-color); }
  input[data-ctl=keepdays] { width:60px; }
  .unit { font-size:13px; color:var(--secondary-text-color); font-weight:400; }
  .empty { text-align:center; color:var(--secondary-text-color); padding:24px; }
  .foot { color:var(--secondary-text-color); font-size:13px; margin-top:12px; }
  code { font-family:var(--code-font-family, monospace); }
  @media (max-width: 800px) { th:nth-child(5), td:nth-child(5), th:nth-child(7), td:nth-child(7) { display:none; } }
`;

if (!customElements.get("recorder-tuner-panel")) customElements.define("recorder-tuner-panel", RecorderTunerPanel);
