/* ping-scheduler — a single page, no framework and no build step.
 *
 * The server returns the whole configuration on every write, so the client's
 * state is always the last thing the server said: nothing has to be reconciled
 * by hand and there is no reload after a change.
 */

const $ = (sel) => document.querySelector(sel);

const weekEl = $("#week");
const runsEl = $("#runs");
const bannerEl = $("#banner");
const metaEl = $("#meta");
const toastEl = $("#toast");
const globalToggle = $("#global-toggle");
const globalLabel = $("#global-label");
const pingBtn = $("#ping-now");
const historyHint = $("#history-hint");

let state = null;
let toastTimer = null;

// ---- Utilities ----

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      const data = await res.json();
      if (data && data.detail) detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    } catch { /* response had no JSON body */ }
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

function toast(message, isError = false) {
  toastEl.textContent = message;
  toastEl.classList.toggle("ko", isError);
  toastEl.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toastEl.hidden = true; }, 3200);
}

// Times are typed as plain text rather than into an <input type="time">: that
// control renders in the browser's locale, so it asks half the world for an
// am/pm the schedule never uses, and on a phone it opens a picker where two
// keystrokes would do. This takes what people actually type — "8", "830", "0830", "8:30",
// "8.30", "8h30" — reads all of it as 24-hour, and returns the "HH:MM" the API
// expects, or null if it is not a time at all.
function parseTime(raw) {
  const text = String(raw).trim().toLowerCase()
    .replace(/[.,;h\s]+/g, ":")   // 8.30, 8h30 and "8 30" all mean 8:30
    .replace(/:+/g, ":")
    .replace(/^:|:$/g, "");

  let hour, minute;
  const parts = /^(\d{1,2}):(\d{1,2})$/.exec(text);
  if (parts) {
    [, hour, minute] = parts;
  } else if (/^\d{1,4}$/.test(text)) {
    // Bare digits: one or two are an hour ("8" is 08:00), three or four are
    // hour and minutes ("830" is 08:30).
    const digits = text.length === 3 ? `0${text}` : text;
    [hour, minute] = digits.length <= 2 ? [digits, "0"] : [digits.slice(0, 2), digits.slice(2)];
  } else {
    return null;
  }

  const h = Number(hour);
  const m = Number(minute);
  if (h > 23 || m > 59) return null;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

// Undefined locale means "whatever the viewer's browser uses"; hour12 is forced
// off so the display matches the 24-hour times the schedule is written in.
//
// Times render in the SCHEDULER's zone, not the viewer's. The schedule is
// defined in that zone and the backend attributes each run to a weekday using
// it, so rendering in the viewer's zone would print a time that contradicts the
// zone label beside it — and a day name that contradicts its own timestamp.
// The server reports the zone it actually resolved, so it is always valid here.
function fmtDateTime(iso) {
  const opts = {
    day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  };
  if (state && state.timezone) opts.timeZone = state.timezone;
  return new Date(iso).toLocaleString(undefined, opts);
}

// getDay() returns 0=Sunday; the backend uses 0=Monday.
function todayIndex() {
  return (new Date().getDay() + 6) % 7;
}

// ---- Rendering ----

function render() {
  if (!state) return;

  globalToggle.checked = state.global_enabled;
  globalLabel.textContent = state.global_enabled ? "Active" : "Paused";

  const jobs = state.scheduled_jobs;
  const next = state.next_runs && state.next_runs[0];
  metaEl.textContent = state.global_enabled
    ? `${jobs} ping${jobs === 1 ? "" : "s"} scheduled` +
      (next ? ` · next ${fmtDateTime(next.at)}` : "") +
      ` · ${state.timezone}`
    : `Scheduler paused · ${state.timezone}`;

  // A missing token does not stop the app from starting, but every ping will
  // fail. That deserves a permanent notice, not a toast that disappears.
  if (!state.token_configured) {
    bannerEl.hidden = false;
    bannerEl.textContent =
      "CLAUDE_CODE_OAUTH_TOKEN is not configured in the container: pings will fail.";
  } else {
    bannerEl.hidden = true;
  }

  renderWeek();
}

function renderWeek() {
  const today = todayIndex();
  weekEl.replaceChildren(...state.days.map((day) => {
    const card = document.createElement("article");
    card.className = "day" + (day.enabled ? "" : " off") + (day.weekday === today ? " today" : "");

    // -- header: name + per-day switch
    const head = document.createElement("div");
    head.className = "day-head";

    const name = document.createElement("span");
    name.className = "day-name";
    name.textContent = day.name;

    const sw = document.createElement("label");
    sw.className = "day-switch";
    sw.title = `Enable or disable ${day.name}`;
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = day.enabled;
    cb.setAttribute("aria-label", `Enable ${day.name}`);
    cb.addEventListener("change", () => setDay(day.weekday, cb.checked));
    const track = document.createElement("span");
    track.className = "track";
    sw.append(cb, track);
    head.append(name, sw);

    // -- time chips
    const chips = document.createElement("ul");
    chips.className = "chips";
    if (day.times.length === 0) {
      const empty = document.createElement("li");
      empty.className = "empty";
      empty.textContent = "No pings";
      chips.append(empty);
    } else {
      chips.append(...day.times.map((t) => {
        const li = document.createElement("li");
        li.className = "chip";
        li.append(document.createTextNode(t));
        const del = document.createElement("button");
        del.type = "button";
        del.textContent = "×";
        del.title = `Remove ${t}`;
        del.setAttribute("aria-label", `Remove ${t} from ${day.name}`);
        del.addEventListener("click", () => removeTime(day.weekday, t));
        li.append(del);
        return li;
      }));
    }

    // -- footer: add a time
    const foot = document.createElement("div");
    foot.className = "day-foot";
    const form = document.createElement("form");
    const input = document.createElement("input");
    input.type = "text";
    input.inputMode = "numeric";
    input.autocomplete = "off";
    input.placeholder = "08:02";
    input.maxLength = 5;
    input.title = "24-hour time — 8, 830 and 8:30 all mean 08:30";
    input.setAttribute("aria-label", `New 24-hour time for ${day.name}, as HH:MM`);
    // Normalise as soon as the field loses focus, so what was typed and what
    // will be scheduled are visibly the same thing.
    input.addEventListener("blur", () => {
      const value = parseTime(input.value);
      if (value) input.value = value;
    });
    const add = document.createElement("button");
    add.type = "submit";
    add.textContent = "+";
    add.title = `Add a time to ${day.name}`;
    form.append(input, add);
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      if (!input.value.trim()) return;
      const value = parseTime(input.value);
      if (!value) {
        toast("Use a 24-hour time: 8, 830 or 8:30", true);
        input.select();
        return;
      }
      addTime(day.weekday, value).then(() => { input.value = ""; });
    });
    foot.append(form);

    card.append(head, chips, foot);
    return card;
  }));
}

function renderHistory(runs) {
  historyHint.textContent = runs.length ? `last ${runs.length}` : "";

  if (!runs.length) {
    const empty = document.createElement("li");
    empty.className = "empty";
    empty.textContent = "No ping has run yet.";
    runsEl.replaceChildren(empty);
    return;
  }

  runsEl.replaceChildren(...runs.map((run) => {
    const li = document.createElement("li");
    li.className = "run " + (run.success ? "ok" : "ko");

    const wrap = document.createElement("div");
    wrap.className = "run-wrap";

    const icon = document.createElement("span");
    icon.className = "run-icon";
    icon.textContent = run.success ? "●" : "▲";
    icon.setAttribute("role", "img");
    icon.setAttribute("aria-label", run.success ? "Succeeded" : "Failed");

    const when = document.createElement("span");
    when.className = "run-when";
    when.textContent = fmtDateTime(run.started_at);

    const day = document.createElement("span");
    day.className = "run-day";
    day.textContent = run.day_name;

    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = run.trigger === "manual" ? "manual" : "auto";

    const detail = document.createElement("span");
    detail.className = "run-detail";
    const code = run.exit_code === null ? "no exit code" : `exit ${run.exit_code}`;
    detail.textContent = `${code} · ${(run.duration_ms / 1000).toFixed(1)}s`;

    wrap.append(icon, when, day, tag, detail);

    if (run.output && run.output !== "(no output)") {
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "run-toggle";
      toggle.textContent = "show output";
      const pre = document.createElement("pre");
      pre.className = "run-out";
      pre.textContent = run.output;
      pre.hidden = true;
      toggle.addEventListener("click", () => {
        pre.hidden = !pre.hidden;
        toggle.textContent = pre.hidden ? "show output" : "hide output";
      });
      wrap.append(toggle);
      li.append(wrap, pre);
    } else {
      li.append(wrap);
    }
    return li;
  }));
}

// ---- Actions ----

async function withBanner(fn, okMessage) {
  try {
    const result = await fn();
    if (okMessage) toast(okMessage);
    return result;
  } catch (e) {
    toast(e.message || "The operation failed", true);
    // If the write failed, what is on screen may not match the server.
    await loadConfig();
    return null;
  }
}

async function loadConfig() {
  state = await api("GET", "/api/config");
  render();
}

async function loadHistory() {
  renderHistory(await api("GET", "/api/history?limit=25"));
}

function apply(next) {
  if (next) { state = next; render(); }
}

const setGlobal = (enabled) =>
  withBanner(() => api("PUT", "/api/config/global", { enabled }).then(apply));

const setDay = (weekday, enabled) =>
  withBanner(() => api("PUT", `/api/config/days/${weekday}`, { enabled }).then(apply));

const addTime = (weekday, time) =>
  withBanner(() => api("POST", `/api/config/days/${weekday}/times`, { time }).then(apply));

const removeTime = (weekday, time) =>
  withBanner(() => api("DELETE", `/api/config/days/${weekday}/times/${time}`).then(apply));

globalToggle.addEventListener("change", () => setGlobal(globalToggle.checked));

pingBtn.addEventListener("click", async () => {
  pingBtn.disabled = true;
  pingBtn.textContent = "Running";
  try {
    const run = await api("POST", "/api/ping");
    toast(run.success ? "Ping succeeded" : `Ping failed (exit ${run.exit_code})`, !run.success);
    await Promise.all([loadHistory(), loadConfig()]);
  } catch (e) {
    toast(e.message || "Could not fire the ping", true);
  } finally {
    pingBtn.disabled = false;
    pingBtn.textContent = "Ping now";
  }
});

// ---- Startup ----

(async function init() {
  try {
    await Promise.all([loadConfig(), loadHistory()]);
  } catch (e) {
    bannerEl.hidden = false;
    bannerEl.textContent = `Could not load the configuration: ${e.message}`;
  }
  // The history refreshes on its own so scheduled pings appear without a
  // reload. The configuration never changes by itself, so it is left alone.
  setInterval(() => loadHistory().catch(() => {}), 30000);
})();
