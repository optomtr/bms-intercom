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

function fakeElement() {
  const el = {
    style: {}, dataset: {}, innerHTML: "", textContent: "", className: "",
    title: "", src: "",
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    querySelector() { return fakeElement(); },
    querySelectorAll() { return []; },
    addEventListener() {}, appendChild() {}, setAttribute() {},
    getAttribute() { return null; }, removeAttribute() {}, load() {},
    play() { return Promise.resolve(); }, pause() {},
  };
  return el;
}

let homeAssistant = null;
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
  location: { protocol: "http:", hostname: "ha.local", port: "8123" },
  navigator: {},
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

out.warnings = warnings.length;
out.errors = errors.length;
process.stdout.write(JSON.stringify(out));
