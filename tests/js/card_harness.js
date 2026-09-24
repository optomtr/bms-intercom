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
// Таймеры запоминаем (не запускаем): тест громкой связи сам «тикает» детектором.
const intervals = [];
// Фальшивые WebRTC/WebAudio — только для теста громкой связи (п. 12).
class MediaStream {
  constructor() { this.tracks = []; }
  addTrack(t) { this.tracks.push(t); }
  getTracks() { return this.tracks; }
  getAudioTracks() { return this.tracks.filter((t) => t.kind === "audio"); }
}
let lastPc = null;
class RTCPeerConnection {
  constructor() { lastPc = this; this.localDescription = { sdp: "v=0" }; }
  addTransceiver() {} close() {}
  createOffer() { return Promise.resolve({}); }
  setLocalDescription() { return Promise.resolve(); }
}
let ctx = null, micLevel = 0;
class Node {
  constructor(kind, extra) { this.kind = kind; this.out = []; Object.assign(this, extra); }
  connect(n) { this.out.push(n); return n; }
  disconnect(n) { this.out = n ? this.out.filter((x) => x !== n) : []; }
}
class FakeAC {
  constructor() { ctx = this; this.state = "running"; this.sampleRate = 48000; this.currentTime = 0;
                  this.destination = new Node("dest"); this.nodes = []; }
  resume() { return Promise.resolve(); }
  close() { this.state = "closed"; return Promise.resolve(); }
  add(n) { this.nodes.push(n); return n; }
  createMediaStreamSource(stream) { return this.add(new Node("src", { stream })); }
  createScriptProcessor() { return this.add(new Node("proc")); }
  createGain() {
    return this.add(new Node("gain", { gain: { value: 1, cancelScheduledValues() {},
      setValueAtTime(v) { this.value = v; }, setTargetAtTime(v) { this.value = v; } } }));
  }
  createAnalyser() {
    return this.add(new Node("an", { fftSize: 2048, getFloatTimeDomainData(b) { b.fill(micLevel); } }));
  }
}
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
  setInterval(fn, ms) { intervals.push({ fn, ms }); return intervals.length; },
  clearInterval(id) { if (intervals[id - 1]) intervals[id - 1].cleared = true; },
  setTimeout() { return 1; }, clearTimeout() {},
  MediaStream, RTCPeerConnection,
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

// 11. Громкая связь: детектор речи оператора → гейн входящего (чистая функция).
function duckRun(s, rms, ms, mic = true) {
  let g;
  for (let t = 0; t < ms; t += 20) g = api.duckStep(s, rms, mic, 20);
  return g;
}
attempt("duck_speech", () => {
  const s = api.duckInit();
  const quiet = duckRun(s, 0.002, 1000);
  const s1 = api.duckStep(s, 0.1, true, 20); // один кадр — ещё не речь (атака)
  const talk = duckRun(s, 0.1, 300);
  const held = duckRun(s, 0.002, 300);       // пауза короче удержания
  const back = duckRun(s, 0.002, 100);       // 400 мс тишины — вернулся
  return { quiet, s1, talk, held, back };
});
attempt("duck_mic_off", () => {
  const s = api.duckInit();
  const off = duckRun(s, 0.3, 500, false);
  duckRun(s, 0.3, 200);
  return { off, offMid: api.duckStep(s, 0.3, false, 20) }; // выключил посреди речи
});
attempt("duck_noisy_room", () => {
  const s = api.duckInit();
  const noise = duckRun(s, 0.03, 8000); // ровный шум громче base — не «вечная речь»
  return { noise, voice: duckRun(s, 0.2, 100) }; // речь над шумом всё равно глушит
});

// Чужие предупреждения п. 12 (нет WebAudio — это ожидаемо) не в счёт п. 6.
out.warnings = warnings.length;
out.errors = errors.length;

// 12. Громкая связь в сборе: звук панели WebRTC → WebAudio, без удвоения.
const flush = async () => { for (let i = 0; i < 3; i++) await new Promise((r) => setImmediate(r)); };
function streamCall(id) {
  homeAssistant = { hass: {
    states: {
      [`binary_sensor.${id}_vyzov`]: { entity_id: `binary_sensor.${id}_vyzov`, state: "on",
        attributes: { intercom_id: id, intercom_name: "Терминал", intercom_role: "call",
                      call_state: "answered", talk_supported: "yes" } },
      [`camera.${id}_video`]: { entity_id: `camera.${id}_video`, state: "idle",
        attributes: { intercom_id: id, intercom_role: "camera", supported_features: 2 } },
    },
    callService() {},
    connection: { sendMessagePromise() { return Promise.resolve({}); },
                  subscribeMessage() { return Promise.resolve(() => {}); } },
  } };
}
async function answerWithPanelAudio(id) {
  streamCall(id);
  api.tick(); await flush();
  lastPc.ontrack({ track: { kind: "audio", stop() {} } });
  const video = overlayEl().kids["video.bms-video"];
  const beforeMic = video.muted;
  api.callRole("answer"); await flush();
  return { video, beforeMic };
}
async function duckWired() {
  sandbox.window.AudioContext = FakeAC;
  sandbox.navigator.mediaDevices.getUserMedia = () => Promise.resolve({ getTracks: () => [{ stop() {} }] });
  const w0 = warnings.length;
  const { video, beforeMic } = await answerWithPanelAudio("e6");
  const chain = () => {
    const src = ctx.nodes.find((n) => n.kind === "src" && n.stream === video.srcObject);
    const poll = intervals.filter((i) => i.ms === 20 && !i.cleared).pop();
    return { src, gain: src && src.out[0], poll };
  };
  let c = chain();
  const step = (level, ms) => {
    micLevel = level;
    for (let t = 0; t < ms; t += 20) { ctx.currentTime += 0.02; c.poll.fn(); }
    return c.gain.gain.value;
  };
  const r = { beforeMic, routedMuted: video.muted,
              toSpeaker: !!c.gain && c.gain.out.includes(ctx.destination),
              speech: step(0.1, 100), after: step(0, 500) };
  await api.toggleMic(); // микрофон выкл → звук обратно в <video>
  r.micOff = { muted: video.muted, ctx: ctx.state, src: c.src.out.length, timer: !!c.poll.cleared };
  await api.toggleMic(); await flush();
  c = chain();
  step(0.1, 100);
  homeAssistant.hass.states = {}; api.tick(); // поп-ап закрылся
  r.closed = { ctx: ctx.state, src: c.src.out.length, gain: c.gain.out.length,
               timer: !!c.poll.cleared, muted: video.muted };
  r.warnings = warnings.length - w0;
  // Браузер без WebAudio — звук панели играет <video>, как раньше.
  sandbox.window.AudioContext = undefined;
  const plain = await answerWithPanelAudio("e7");
  r.noWebAudioMuted = plain.video.muted;
  return r;
}

// 13. Автообновление устаревшей карточки (0.3.10). Каждый сценарий — свежий
// экземпляр карточки: своя версия читается один раз при загрузке файла.
const CARD_URL = "http://ha.local:8123/bms_intercom_static/bms_intercom_card.js?v=";
function memStorage() {
  const m = {};
  return { getItem(k) { return k in m ? m[k] : null; }, setItem(k, v) { m[k] = String(v); } };
}
function freshCard({ script = "", filename, protocol = "http:", storage, local, now = 1.7e12 } = {}) {
  const env = { now, reloads: 0, infos: [], warns: 0 };
  env.hass = { states: {}, callService() {},
    connection: { sendMessagePromise() { return new Promise(() => {}); },
                  subscribeMessage() { return Promise.resolve(() => {}); } } };
  const win = { __BMS_INTERCOM_TEST__: {}, isSecureContext: true, customCards: [] };
  for (const [name, st] of [["sessionStorage", storage], ["localStorage", local]]) {
    if (st === "throws") {
      Object.defineProperty(win, name, { get() { throw new Error("SecurityError"); } });
    } else if (st) {
      win[name] = st;
    }
  }
  const box = {
    __env: env, window: win,
    document: {
      currentScript: script ? { src: script } : null,
      querySelector(sel) { return sel === "home-assistant" ? { hass: env.hass } : null; },
      createElement() { return fakeElement(); },
      body: { appendChild() {} },
    },
    customElements: { get() { return undefined; }, define() {} },
    HTMLElement: class {}, Audio: function () { return fakeElement(); },
    setInterval() { return 0; }, clearInterval() {}, setTimeout() { return 1; }, clearTimeout() {},
    MediaStream, RTCPeerConnection,
    location: { protocol, hostname: "ha.local", port: "8123", pathname: "/", search: "", hash: "",
                reload() { env.reloads += 1; } },
    navigator: { mediaDevices: { getUserMedia() { return new Promise(() => {}); } } },
    console: { info(...a) { env.infos.push(String(a[0])); }, debug() {}, log() {},
               warn() { env.warns += 1; }, error() { env.warns += 1; } },
  };
  win.document = box.document;
  vm.createContext(box);
  vm.runInContext("Date.now = function () { return __env.now; };", box);
  vm.runInContext(src, box, filename ? { filename } : undefined);
  env.api = win.__BMS_INTERCOM_TEST__.api;
  // Состояния домофона r1: версия карточки в HA, фаза вызова, «Просмотр».
  env.set = (ver, callState = "idle", view = false) => {
    const a = { intercom_id: "r1", intercom_name: "Домофон" };
    if (ver) a.intercom_card_version = ver;
    env.hass.states = {
      "binary_sensor.r1_vyzov": { entity_id: "binary_sensor.r1_vyzov",
        state: callState === "idle" ? "off" : "on",
        attributes: { ...a, intercom_role: "call", call_state: callState } },
      "switch.r1_view": { entity_id: "switch.r1_view", state: view ? "on" : "off",
        attributes: { ...a, intercom_role: "view" } },
    };
  };
  // Цикл поп-апа каждые 400 мс, как в браузере.
  env.run = (ms) => { for (let t = 0; t < ms; t += 400) { env.now += 400; env.api.safeTick(); } return env.reloads; };
  env.reloadLines = () => env.infos.filter((l) => l.includes("перезагружаю")).length;
  env.manualLines = () => env.infos.filter((l) => l.includes("обновите страницу вручную")).length;
  return env;
}
attempt("stale_reload_once", () => {
  const c = freshCard({ script: CARD_URL + "old111", storage: memStorage() });
  c.set("new222");
  c.api.safeTick();                    // первый цикл простоя — отсчёт пошёл
  const early = c.run(4400);           // 4,4 с — рано
  const at5 = c.run(1200);             // перевалило за 5 с
  const later = c.run(60000);          // страница «не ушла» — второй попытки нет
  return { version: c.api.CARD_VERSION, early, at5, later, lines: c.reloadLines(), warns: c.warns };
});
attempt("stale_busy_never", () => {
  const r = {};
  for (const [name, st, view] of [["ringing", "ringing", false], ["answered", "answered", false],
                                  ["view", "idle", true]]) {
    const c = freshCard({ script: CARD_URL + "old111", storage: memStorage() });
    c.set("new222", st, view);
    r[name] = c.run(60000);
    if (name === "ringing") {
      c.set("new222");                 // звонок кончился: простой считается заново
      r.afterCallEarly = c.run(4400);
      r.afterCall = c.run(1200);
    }
  }
  return r;
});
attempt("stale_same_version", () => {
  const c = freshCard({ script: CARD_URL + "same333", storage: memStorage() });
  c.set("same333");
  return c.run(60000);
});
attempt("stale_repeat_guard", () => {
  const store = memStorage();
  const first = freshCard({ script: CARD_URL + "old111", storage: store });
  first.set("new222");
  const r = { first: first.run(6000) };
  // Перезагрузка отдала всё тот же старый файл (кэш/прокси): новая страница,
  // та же вкладка (sessionStorage), та же целевая версия — не крутимся.
  const again = freshCard({ script: CARD_URL + "old111", storage: store, now: first.now + 30000 });
  again.set("new222");
  r.again = again.run(60000);
  const late = freshCard({ script: CARD_URL + "old111", storage: store, now: first.now + 11 * 60000 });
  late.set("new222");
  r.after10min = late.run(6000);
  const next = freshCard({ script: CARD_URL + "old111", storage: store, now: late.now + 60000 });
  next.set("new444");                  // ещё одно обновление — новая цель
  r.newTarget = next.run(6000);
  return r;
});
attempt("stale_session_off_local_on", () => {
  // sessionStorage закрыт — метка в localStorage; повтор той же цели в 10 мин — нет.
  const local = memStorage();
  const first = freshCard({ script: CARD_URL + "old111", storage: "throws", local });
  first.set("new222");
  const r = { first: first.run(6000), lines: first.reloadLines() };
  const again = freshCard({ script: CARD_URL + "old111", storage: "throws", local,
                            now: first.now + 30000 });
  again.set("new222");
  r.again = again.run(60000);
  return r;
});
attempt("stale_no_storage", () => {
  // Оба хранилища закрыты: страж в памяти reload не переживёт — не перезагружаем.
  const c = freshCard({ script: CARD_URL + "old111", storage: "throws", local: "throws" });
  c.set("new222");
  return { reloads: c.run(60000), manual: c.manualLines(), warns: c.warns };
});
attempt("stale_unknown_version", () => {
  const plain = freshCard({ storage: memStorage() });            // ни currentScript, ни ?v= в стеке
  plain.set("new222");
  const noV = freshCard({ script: "http://ha.local:8123/bms_intercom_static/bms_intercom_card.js",
                          storage: memStorage() });
  noV.set("new222");
  const kiosk = freshCard({ script: CARD_URL + "old111", protocol: "file:", storage: memStorage() });
  kiosk.set("new222");
  return { plain: plain.run(60000), plainVersion: plain.api.CARD_VERSION,
           noV: noV.run(60000), kiosk: kiosk.run(60000) };
});
attempt("stale_module_url_from_stack", () => {
  // ES-модуль: currentScript = null, свой URL — только в стеке исполнения.
  const c = freshCard({ filename: CARD_URL + "mod777", storage: memStorage() });
  c.set("new222");
  return { version: c.api.CARD_VERSION, reloads: c.run(6000) };
});
attempt("stale_attr_missing", () => {
  // Старая интеграция без атрибута — карточка не трогает страницу.
  const c = freshCard({ script: CARD_URL + "old111", storage: memStorage() });
  c.set("");
  return c.run(60000);
});

(async () => {
  try { out.duck_wired = { ok: true, value: await duckWired() }; }
  catch (e) { out.duck_wired = { ok: false, error: String((e && e.stack) || e) }; }
  process.stdout.write(JSON.stringify(out));
})();
