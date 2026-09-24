"""Фейковый помощник bms_talk: тот же протокол stdin/stdout, что у настоящего.

Запуск: python fake_bms_talk.py <record> [--selftest]
- `<record>.start` — первая строка stdin (JSON входа), как её получил помощник;
- `<record>.audio` — все байты звука после неё;
- `<record>.stopped` — появляется, когда stdin закрыли (конец разговора).
Пароль "wrong…" → ответ error code 1, как HCNetSDK на неверный пароль.
В stderr помощник пишет пароль нарочно: интеграция не должна пронести его
в свой журнал.
"""
import json
import sys

record = sys.argv[1]
if "--selftest" in sys.argv:
    print(json.dumps({"type": "selftest", "sdk": "6.1.8.101-fake"}), flush=True)
    sys.exit(0)

line = sys.stdin.buffer.readline()
with open(record + ".start", "wb") as fh:
    fh.write(line)
start = json.loads(line)
print(f"login {start['user']}:{start['password']}@{start['host']}", file=sys.stderr, flush=True)
if start["password"].startswith("wrong"):
    print(json.dumps({"type": "error", "code": 1,
                      "message": "NET_DVR_Login_V40 failed"}), flush=True)
    sys.exit(1)
print(json.dumps({"type": "started", "codec": "G.711ulaw"}), flush=True)
with open(record + ".audio", "wb") as fh:
    while True:
        chunk = sys.stdin.buffer.read1(4096)
        if not chunk:
            break
        fh.write(chunk)
        fh.flush()
open(record + ".stopped", "w").close()
