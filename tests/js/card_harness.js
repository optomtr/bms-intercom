// Runs the popup card in a minimal fake DOM and exercises the states the
// real frontend goes through: no <home-assistant>, hass without states
// (boot / websocket reconnect), null entries, and a normal ringing call.
// Prints one JSON line with the results; test_card.py asserts on it.
"use strict";
const fs = require("fs");
const vm = require("vm");
const path = require("path");

const src = fs.readFileSync(
  path.join(__dirname, "..", "..", "custom_components", "bms_intercom",
            "frontend", "bms_intercom_card.js"), "utf8");

// Элемент помнит своё состояние: классы и потомков по селектору (один и тот
// же querySelector отдаёт один и тот же объект) — иначе не проверить, что
// кнопка «Микрофон» спрятана.
const created = [];
function fakeElement() {
  const classes = new Set();
  const kids = {};
  const el = {
    style: {}, dataset: {}, innerHTML: "", textContent: "", className: "",
    title: "", src: "", kids,
    classList: {
      add(c) { classes.add(c); },
      remove(c) { classes.delete(c); },
      toggle(c, force) {
        const on = force === undefined ? !classes.has(c) : !!force;
        if (on) classes.add(c); else classes.delete(c);
        return on;
      },
      contains(c) { return classes.has(c); },
    },
    querySelector(sel) { return kids[sel] || (kids[sel] = fakeElement()); },
    querySelectorAll() { return []; },
    addEventListener() {}, appendChild() {}, setAttribute() {},
    getAttribute() { return null; }, removeAttribute() {}, load() {},
    play() { return Promise.resolve(); }, pause() {},
  };
  created.push(el);
  return el;
}

let homeAssistant = null;
let gumCalls = 0;
const warnings = [];
const errors = [];
const sandbox = {
  window: { __BMS_INTERCOM_TEST__: {}, isSecureContext: true, customCards: [] },
  document: {
    querySelector(sel) { return sel === "home-assistant" ? homeAssistant : null; },
    createElement() { return fakeElement(); },
    body: { appendChild() {} },
  },
  customElements: { get() { return undefined; }, define() {} },
  HTMLElement: class {},
  Audio: function () { return fakeElement(); },
  setInterval() { return 1; },
  setTimeout() { return 1; }, clearTimeout() {},
  location: { protocol: "http:", hostname: "ha.local", port: "8123" },
  navigator: {
    mediaDevices: {
      getUserMedia() { gumCalls += 1; return new Promise(() => {}); },  // висит
    },
  },
  console: {
    info() {}, debug() {}, log() {},
    warn(...a) { warnings.push(String(a[0])); },
    error(...a) { errors.push(String(a[0])); },
  },
};
sandbox.window.document = sandbox.document;
vm.createContext(sandbox);
vm.runInContext(src, sandbox);
const api = sandbox.window.__BMS_INTERCOM_TEST__.api;

const out = {};
function attempt(name, fn) {
  try { out[name] = { ok: true, value: fn() }; }
  catch (e) { out[name] = { ok: false, error: String(e) }; }
}

// 1. frontend not mounted yet
homeAssistant = null;
attempt("no_home_assistant", () => { api.tick(); return "ok"; });
// 2. hass object without states — the reported crash
homeAssistant = { hass: {} };
attempt("hass_without_states", () => { api.tick(); return "ok"; });
attempt("group_without_states", () => Object.keys(api.groupIntercoms({})).length);
// 3. states explicitly null
homeAssistant = { hass: { states: null } };
attempt("states_null", () => { api.tick(); return "ok"; });
// 4. a null entry inside states
attempt("null_entry", () =>
  Object.keys(api.groupIntercoms({ states: { "x.y": null } })).length);
// 5. a real ringing intercom still gets grouped
const ringing = {
  "binary_sensor.domofon_vyzov": { entity_id: "binary_sensor.domofon_vyzov", state: "on",
    attributes: { intercom_id: "e1", intercom_name: "Домофон", intercom_role: "call",
                  call_state: "ringing" } },
  "camera.domofon_video": { entity_id: "camera.domofon_video", state: "idle",
    attributes: { intercom_id: "e1", intercom_role: "camera" } },   // no token yet
};
attempt("ringing_group", () => {
  const g = api.groupIntercoms({ states: ringing });
  return g.e1 ? g.e1.callState : null;
});
homeAssistant = { hass: { states: ringing } };
attempt("ringing_tick_without_camera_token", () => { api.tick(); return "ok"; });
// 6. safeTick never throws, even on a hostile hass
homeAssistant = { get hass() { throw new Error("boom"); } };
attempt("safe_tick_swallows", () => { api.safeTick(); api.safeTick(); return "ok"; });

// 7. Показанный тост ловит клики (ссылка «Открыть по HTTPS» нажимается).
const overlayEl = () => created.find((e) => e.id === "bms-intercom-overlay");
attempt("toast_show_takes_clicks", () =>
  /\.bms-toast\.show\s*\{[^}]*pointer-events:\s*auto/.test(overlayEl().innerHTML));

// 8. Терминал без two-way audio (talk_supported=no): в разговоре кнопки
// «Микрофон» нет, «Ответить» не трогает микрофон и talk_start, тост — один раз.
function talking(id, talk, extra) {
  const sent = [];
  const more = extra || {};
  const hass = {
    states: {
      [`binary_sensor.${id}_vyzov`]: { entity_id: `binary_sensor.${id}_vyzov`, state: "on",
        attributes: { intercom_id: id, intercom_name: "Терминал", intercom_role: "call",
                      call_state: "answered", talk_supported: talk, ...more } },
      [`button.${id}_answer`]: { entity_id: `button.${id}_answer`, state: "unknown",
        attributes: { intercom_id: id, intercom_role: "answer", talk_supported: talk, ...more } },
    },
    callService() {},
    connection: { sendMessagePromise(m) { sent.push(m.type); return new Promise(() => {}); } },
  };
  homeAssistant = { hass };
  return sent;
}
const micHidden = () => overlayEl().kids[".bms-mic"].classList.contains("bms-hidden");
const toast = () => overlayEl().kids[".bms-toast"];
attempt("talk_no", () => {
  const sent = talking("e2", "no");
  api.tick();
  const hidden = micHidden();
  const gum0 = gumCalls;
  api.callRole("answer");
  const first = toast().textContent;
  toast().textContent = "";
  api.callRole("answer");
  return { hidden, gum: gumCalls - gum0, talkStart: sent.includes("bms_intercom/talk_start"),
           first, second: toast().textContent };
});
// Контроль: модель с two-way audio — кнопка есть, микрофон захватывается.
attempt("talk_yes", () => {
  talking("e3", "yes");
  api.tick();
  const hidden = micHidden();
  const gum0 = gumCalls;
  api.callRole("answer");
  return { hidden, gum: gumCalls - gum0 };
});
// 9. Голоса нет, и интеграция говорит почему (talk_hint): тост с причиной, один раз.
attempt("talk_hint", () => {
  talking("e4", "no", { talk_via: "none", talk_hint: "голос по SDK недоступен на этой платформе" });
  api.tick();
  const hidden = micHidden();
  toast().textContent = "";
  api.callRole("answer");
  const first = toast().textContent;
  toast().textContent = "";
  api.callRole("answer");
  return { hidden, first, second: toast().textContent };
});
// 10. Терминал с голосом через HCNetSDK (talk_via=sdk) — «Микрофон» как у ISAPI.
attempt("talk_sdk", () => {
  talking("e5", "yes", { talk_via: "sdk" });
  api.tick();
  const hidden = micHidden();
  const gum0 = gumCalls;
  api.callRole("answer");
  return { hidden, gum: gumCalls - gum0 };
});

out.warnings = warnings.length;
out.errors = errors.length;
process.stdout.write(JSON.stringify(out));
