#!/usr/bin/env bash
# Проверка помощника bms_talk ВНУТРИ настоящего образа Home Assistant (Alpine/musl, arm64) —
# ровно там, где его будет запускать интеграция.
#
#   ./test_in_ha.sh          — без терминала: зависимости через загрузчик glibc, (а) --selftest,
#                              (б) start на 127.0.0.1:8000 → ожидаем {"type":"error","code":7},
#                              (в) то же в постоянном режиме --serve (ready не придёт → error 7),
#                              (г) --serve с поддельным SDK (если есть $BMS_TALK_CACHE/mock/mock_sdk.c):
#                                  ready → S(11) → S(11) → S → started → A → E → stopped → P → Q,
#                                  перевход на коде 7 и по колбэку «пульс пропал», неверный пароль.
#                              Если docker умеет x86 (эмуляция) — (а)–(в) для amd64 в Alpine amd64.
#   ./test_in_ha.sh talk     — (в) для владельца: пароль спросит test_client.py (ввод скрыт),
#                              3 с «бип-бип» на терминал 192.168.70.121:8000. Параметры можно
#                              переопределить: ./test_in_ha.sh talk --host 192.168.70.121 --user admin
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HA_IMAGE="${HA_IMAGE:-ghcr.io/home-assistant/home-assistant:stable}"
# amd64-проверка — в Alpine по digest: так не перетирается локальный тег alpine:latest (arm64).
AMD64_IMAGE="${AMD64_IMAGE:-alpine@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6}"

if ! docker info >/dev/null 2>&1; then
  echo "Docker не отвечает — запускаю colima..."
  colima start
fi

# Поддельный libhcnetsdk.so (исходник — в кэше сборки, рядом с Hikvision-Addons): собираем в том же
# образе-сборщике, что build.sh. Нет исходника или образа — проверка (г) пропускается.
CACHE="${BMS_TALK_CACHE:-$HOME/.cache/bms-talk}"
MOCK_DIR=""
if [ -f "$CACHE/mock/mock_sdk.c" ] && docker image inspect bms-talk-builder:bookworm >/dev/null 2>&1; then
  if docker run --rm --platform linux/arm64 -v "$HERE/src:/src:ro" -v "$CACHE/mock:/m" bms-talk-builder:bookworm \
       gcc -shared -fPIC -O2 -I/src -o /m/libhcnetsdk.so /m/mock_sdk.c -lpthread; then
    MOCK_DIR="$CACHE/mock"
  else
    echo "!! поддельный SDK не собрался — (г) пропускаю"
  fi
fi

if [ "${1:-}" = "talk" ]; then
  shift
  # -it обязателен: иначе getpass не сможет скрыть ввод пароля.
  exec docker run --rm -it --platform linux/arm64 --entrypoint python3 \
    -v "$HERE:/sdk-src:ro" "$HA_IMAGE" /sdk-src/test_client.py --src /sdk-src/aarch64 "$@"
fi

# Проверки (а)(б) в одном контейнере. ARCHDIR/LDSO — какую папку и каким загрузчиком гонять.
run_checks() {
  local platform=$1 image=$2 archdir=$3 ldso=$4 mock=${5:-}
  local mount=()
  [ -n "$mock" ] && mount=(-v "$mock:/m:ro")
  echo "######## $archdir в $image ($platform)"
  docker run --rm -i --platform "$platform" --entrypoint sh -e ARCHDIR="$archdir" -e LDSO="$ldso" \
    -v "$HERE:/sdk-src:ro" ${mount[@]+"${mount[@]}"} "$image" -s <<'EOF'
set -eu
# Как будет делать интеграция: копия во временный каталог + chmod (HACS теряет бит исполнения).
T=$(mktemp -d); cp -r "/sdk-src/$ARCHDIR" "$T/"; D="$T/$ARCHDIR"
chmod +x "$D/$LDSO" "$D/bms_talk"
L="$D/$LDSO"
cd "$T"
fail=0

echo "== зависимости каждой .so через загрузчик glibc (--list)"
for f in "$D"/bms_talk "$D"/*.so* "$D"/HCNetSDKCom/*.so*; do
  case "$f" in */ld-linux-*) continue ;; esac
  out=$("$L" --library-path "$D" --list "$f" 2>&1) || true
  if echo "$out" | grep -q "not found"; then echo "  НЕ НАЙДЕНО ($f):"; echo "$out" | grep "not found"; fail=1; fi
  # musl-библиотеки Alpine из /lib, /usr/lib glibc-программе подсовывать нельзя
  if echo "$out" | grep -qE "=> /(usr/)?lib/"; then echo "  ВЗЯТО ИЗ СИСТЕМЫ ($f):"; echo "$out" | grep -E "=> /(usr/)?lib/"; fail=1; fi
done
[ $fail -eq 0 ] && echo "  ок: всё находится внутри $ARCHDIR/"

echo "== (а) --selftest"
st=$("$L" --library-path "$D" "$D/bms_talk" --selftest 2>"$T/st.err") && rc=0 || rc=$?
sed 's/^/  stderr: /' "$T/st.err"
echo "  stdout: $st (выход $rc)"
if [ $rc -eq 0 ] && echo "$st" | grep -q '"ok":true'; then echo "  (а) ОК"; else echo "  (а) ПРОВАЛ"; fail=1; fi

echo "== (б) start на 127.0.0.1:8000 (там никого нет) — ждём код 7"
# Пароль здесь — заведомо не настоящий: терминал не участвует, соединение уйдёт в пустой порт.
req='{"host":"127.0.0.1","port":8000,"user":"admin","password":"not-a-real-password","channel":1}'
out=$(printf '%s\n' "$req" | "$L" --library-path "$D" "$D/bms_talk" 2>"$T/b.err") && rc=0 || rc=$?
sed 's/^/  stderr: /' "$T/b.err"
echo "  stdout: $out (выход $rc)"
lines=$(printf '%s\n' "$out" | grep -c . || true)
if [ $rc -eq 3 ] && [ "$lines" -eq 1 ] && echo "$out" | grep -q '"type":"error","code":7,'; then
  echo "  (б) ОК: одна JSON-строка, код 7, выход 3"
else
  echo "  (б) ПРОВАЛ (строк: $lines)"; fail=1
fi
if grep -q "not-a-real-password" "$T/b.err"; then echo "  ПРОВАЛ: пароль попал в журнал"; fail=1; fi

echo "== (в) --serve на 127.0.0.1:8000 — ready не придёт, ждём error 7"
req='{"host":"127.0.0.1","port":8000,"user":"admin","password":"not-a-real-password"}'
out=$(printf '%s\n' "$req" | "$L" --library-path "$D" "$D/bms_talk" --serve 2>"$T/c.err") && rc=0 || rc=$?
sed 's/^/  stderr: /' "$T/c.err"
echo "  stdout: $out (выход $rc)"
lines=$(printf '%s\n' "$out" | grep -c . || true)
if [ $rc -eq 3 ] && [ "$lines" -eq 1 ] && echo "$out" | grep -q '"type":"error","code":7,'; then
  echo "  (в) ОК: одна JSON-строка, код 7, выход 3"
else
  echo "  (в) ПРОВАЛ (строк: $lines)"; fail=1
fi
if grep -q "not-a-real-password" "$T/c.err"; then echo "  ПРОВАЛ: пароль попал в журнал"; fail=1; fi

if [ -f /m/libhcnetsdk.so ] && command -v python3 >/dev/null; then
  echo "== (г) --serve с поддельным SDK"
  M="$T/mock"; cp -r "$D" "$M"; cp /m/libhcnetsdk.so "$M/libhcnetsdk.so"
  cat > "$T/serve.py" <<'PY'
import json, os, re, select, struct, subprocess, sys, time
M, LDSO = sys.argv[1], sys.argv[2]
PW = "пар\"оль\\😀"          # тот, что ждёт поддельный Login_V40 (экраны JSON, суррогатная пара)
LOG = M + "/mock.log"
ok = True

def check(name, cond, info=""):
    global ok
    ok &= bool(cond)
    print(("  OK   " if cond else "  FAIL ") + name + (" :: " + info if info else ""), flush=True)

class Helper:
    def __init__(self, password=PW, **env):
        e = dict(os.environ, MOCK_LOG=LOG, **env)
        self.p = subprocess.Popen([M + "/" + LDSO, "--library-path", M, M + "/bms_talk", "--serve"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=e)
        self.buf = b""
        req = {"host": "10.0.0.1", "port": 8000, "user": "admin", "password": password}
        self.p.stdin.write(json.dumps(req).encode() + b"\n"); self.p.stdin.flush()

    def send(self, kind, payload=b""):
        self.p.stdin.write(kind + struct.pack("<I", len(payload)) + payload); self.p.stdin.flush()

    def line(self, timeout=8.0):
        end = time.monotonic() + timeout
        while b"\n" not in self.buf:
            left = end - time.monotonic()
            if left <= 0 or not select.select([self.p.stdout], [], [], left)[0]:
                return None
            chunk = os.read(self.p.stdout.fileno(), 4096)
            if not chunk:
                return None
            self.buf += chunk
        raw, self.buf = self.buf.split(b"\n", 1)
        return json.loads(raw)

    def ask(self, kind, payload=b""):
        self.send(kind, payload)
        return self.line()

    def finish(self, timeout=5):
        try:
            rc = self.p.wait(timeout)
        except subprocess.TimeoutExpired:
            self.p.kill(); rc = "висел"
        return rc, self.p.stderr.read().decode("utf-8", "replace"), open(LOG).read()

S = json.dumps({"channel": 1}).encode()

# 1. занят дважды (код 11) → тот же процесс открывает голос; звук; закрыть; снова открыть; Q
h = Helper(MOCK_START_ERRS="11,11")
t0 = time.monotonic(); r = h.line(); t_ready = time.monotonic() - t0
check("1 ready", r and r.get("type") == "ready" and r.get("sdk"), f"{r} за {t_ready:.2f} с")
r1, r2 = h.ask(b"S", S), h.ask(b"S", S)
check("1 S → 11, S → 11 (процесс жив)", r1.get("code") == 11 and r2.get("code") == 11 and h.p.poll() is None, f"{r1} {r2}")
t0 = time.monotonic(); r = h.ask(b"S", S); t_start = time.monotonic() - t0
check("1 третий S → started", r == {"type": "started", "codec": "G.711ulaw"}, f"{r} за {t_start * 1000:.0f} мс")
nxt = time.monotonic()
for _ in range(50):                                   # 1 с звука в реальном времени
    h.send(b"A", b"\xff" * 160); nxt += 0.02
    time.sleep(max(0, nxt - time.monotonic()))
time.sleep(0.1)
check("1 E → stopped", h.ask(b"E") == {"type": "stopped"})
r = h.ask(b"P")
check("1 P → pong", r == {"type": "pong", "logged_in": True, "voice": False}, str(r))
h.send(b"A", b"\xff" * 1600)                          # голос закрыт — звук выбрасывается
check("1 второй разговор тем же процессом", h.ask(b"S", S).get("type") == "started" and h.ask(b"E") == {"type": "stopped"})
h.send(b"Q")
rc, err, log = h.finish()
ts = [float(x) for x in re.findall(r"send h=7 t=([\d.]+)", log)]
gaps = [b - a for a, b in zip(ts, ts[1:])]
avg = sum(gaps) / len(gaps) if gaps else 0
check("1 Q → выход 0, вход один раз, Logout/Cleanup", rc == 0 and log.count("login ") == 1 and "pw_ok=1" in log
      and "logout 0" in log and "cleanup" in log, f"rc={rc} входов={log.count('login ')}")
check("1 звук: ~50 кадров по 20 мс, после E не шлётся", 45 <= len(ts) <= 51 and 19 < avg < 21.5, f"кадров={len(ts)} шаг={avg:.1f} мс")
check("1 пароль не в журнале", PW not in err)

# 2. StartVoiceCom → 7 (сеанс потерян) → перевход один раз → started
h = Helper(MOCK_START_ERRS="7")
h.line(); r = h.ask(b"S", S); h.send(b"Q"); rc, err, log = h.finish()
check("2 код 7 → перевход → started", r.get("type") == "started" and log.count("login ") == 2 and rc == 0,
      f"{r} входов={log.count('login ')}")

# 3. колбэк SDK «пульс пропал» (0x8000) → перевход до StartVoiceCom
h = Helper(MOCK_LOST="1")
h.line(); time.sleep(0.3); r = h.ask(b"S", S); h.send(b"Q"); rc, err, log = h.finish()
check("3 исключение 0x8000 → перевход → started", r.get("type") == "started" and log.count("login ") == 2
      and "исключение SDK 0x8000" in err, f"{r} входов={log.count('login ')}")

# 4. неверный пароль — error 1 и выход 3, без повторов
h = Helper(password="wrong-password")
r = h.line(); rc, err, log = h.finish()
check("4 неверный пароль", r and r.get("code") == 1 and r.get("attempts_left") == 2 and rc == 3
      and log.count("login ") == 1 and "wrong-password" not in err, f"{r} rc={rc}")

# 5. EOF посреди разговора — StopVoiceCom/Logout/Cleanup, выход 0
h = Helper()
h.line(); h.ask(b"S", S); h.send(b"A", b"\xff" * 800); time.sleep(0.2); h.p.stdin.close()
rc, err, log = h.finish()
check("5 EOF в разговоре", rc == 0 and "stop h=7" in log and "logout 0" in log and "cleanup" in log, f"rc={rc}")

# 6. мусор вместо кадра — error -1 и выход 2
h = Helper()
h.line(); r = h.ask(b"Z"); rc, err, log = h.finish()
check("6 сломанный поток", r and r.get("code") == -1 and rc == 2 and "cleanup" in log, f"{r} rc={rc}")

print("  (г) ОК" if ok else "  (г) ПРОВАЛ", flush=True)
sys.exit(0 if ok else 1)
PY
  python3 "$T/serve.py" "$M" "$LDSO" </dev/null || fail=1
else
  echo "== (г) пропущена: нет поддельного SDK или python3 в образе"
fi

rm -rf "$T"
exit $fail
EOF
}

rc=0
run_checks linux/arm64 "$HA_IMAGE" aarch64 ld-linux-aarch64.so.1 "$MOCK_DIR" || rc=1
if [ "$(docker run --rm --platform linux/amd64 "$AMD64_IMAGE" uname -m 2>/dev/null || true)" = "x86_64" ]; then
  run_checks linux/amd64 "$AMD64_IMAGE" amd64 ld-linux-x86-64.so.2 || rc=1
else
  echo "######## amd64: docker не умеет x86-64 здесь — пропускаю (проверены только file/readelf в build.sh)"
fi
[ $rc -eq 0 ] && echo "== ВСЁ ОК" || echo "== ЕСТЬ ПРОВАЛЫ"
exit $rc
