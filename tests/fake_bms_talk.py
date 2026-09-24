"""Фейковый помощник bms_talk: тот же протокол stdin/stdout, что у настоящего.

Запуск: python fake_bms_talk.py <record> [--selftest | --serve] [флаги]
Постоянный режим (--serve, 0.3.8; протокол — sdk/src/serve.h): первая строка
stdin — JSON входа → {"type":"ready"}; дальше кадры «тип + длина LE32 + данные»:
S → started | error, A — звук, E → stopped, P → pong, Q/EOF — выход.
- `<record>.runs` — сколько раз помощника запускали (не selftest);
- `<record>.start` — первая строка stdin (JSON входа), как её получил помощник;
- `<record>.events` — по строке на событие по порядку: `login`, `S {...}`, `E`,
  `P`, `Q`, `EOF`. Тест дописывает в тот же файл команды ISAPI панели
  (`isapi reject`) — так видно, ушли ли reject/hangUp раньше первого S;
- `<record>.audio` — байты звука (кадры A, пока голос открыт);
- `<record>.pid` — pid процесса (тест «помощник упал» его убивает).
Пароль "wrong…" → error code 1 и выход 3, как HCNetSDK на неверный пароль.
`--busy N` → первые N кадров S отвечают error code 11 — как живой DS-K1T341AM,
пока он в своём режиме вызова; процесс при этом живёт.
`--ready-delay С` → вход «идёт» столько секунд (HA Green: ~5,5 с).
`--voice-lost-after N` → после N байт звука голос «обрывается» (voice_lost).
`--hang` → на P молчит навсегда, как зависший SDK (снять можно только сигналом).
В stderr помощник пишет пароль нарочно: интеграция не должна пронести его
в свой журнал.
"""
import json
import os
import struct
import sys
import time

record = sys.argv[1]
if "--selftest" in sys.argv:
    print(json.dumps({"type": "selftest", "sdk": "6.1.8.101-fake"}), flush=True)
    sys.exit(0)


def flag(name, default, kind=int):
    return kind(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default


def event(text):
    with open(record + ".events", "a", encoding="utf-8") as fh:
        fh.write(text + "\n")


def reply(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


busy = flag("--busy", 0)
lost_after = flag("--voice-lost-after", 0)
try:
    with open(record + ".runs") as fh:
        run = int(fh.read()) + 1
except FileNotFoundError:
    run = 1
with open(record + ".runs", "w") as fh:
    fh.write(str(run))
with open(record + ".pid", "w") as fh:
    fh.write(str(os.getpid()))

inp = sys.stdin.buffer
line = inp.readline()
with open(record + ".start", "wb") as fh:
    fh.write(line)
start = json.loads(line)
print(f"login {start['user']}:{start['password']}@{start['host']}", file=sys.stderr, flush=True)
event("login")
if start["password"].startswith("wrong"):
    reply({"type": "error", "code": 1, "message": "NET_DVR_Login_V40 failed"})
    sys.exit(3)
time.sleep(flag("--ready-delay", 0.0, float))
reply({"type": "ready", "sdk": "6.1.8.101-fake"})


def read_exact(n):
    data = b""
    while len(data) < n:
        chunk = inp.read(n - len(data))
        if not chunk:
            return None
        data += chunk
    return data


voice = False
heard = 0
while True:
    hdr = read_exact(5)
    payload = None if hdr is None else read_exact(struct.unpack("<I", hdr[1:])[0])
    if payload is None:
        event("EOF")
        break
    kind = chr(hdr[0])
    if kind == "S":
        event("S " + payload.decode())
        if busy > 0:
            busy -= 1
            reply({"type": "error", "code": 11, "message": "NET_DVR_StartVoiceCom_MR_V30 failed"})
        else:
            voice = True
            reply({"type": "started", "codec": "G.711ulaw"})
    elif kind == "A":
        if voice:
            with open(record + ".audio", "ab") as fh:
                fh.write(payload)
            heard += len(payload)
            if lost_after and heard >= lost_after:
                voice = False
                reply({"type": "voice_lost", "code": 8, "message": "связь с терминалом потеряна"})
    elif kind == "E":
        event("E")
        voice = False
        reply({"type": "stopped"})
    elif kind == "P":
        event("P")
        if "--hang" in sys.argv:
            time.sleep(3600)
        reply({"type": "pong", "logged_in": True, "voice": voice})
    elif kind == "Q":
        event("Q")
        break
