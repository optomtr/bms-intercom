#!/usr/bin/env bash
# Проверка помощника bms_talk ВНУТРИ настоящего образа Home Assistant (Alpine/musl, arm64) —
# ровно там, где его будет запускать интеграция.
#
#   ./test_in_ha.sh          — без терминала: зависимости через загрузчик glibc, (а) --selftest,
#                              (б) start на 127.0.0.1:8000 → ожидаем {"type":"error","code":7}.
#                              Если docker умеет x86 (эмуляция) — то же для amd64 в Alpine amd64.
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

if [ "${1:-}" = "talk" ]; then
  shift
  # -it обязателен: иначе getpass не сможет скрыть ввод пароля.
  exec docker run --rm -it --platform linux/arm64 --entrypoint python3 \
    -v "$HERE:/sdk-src:ro" "$HA_IMAGE" /sdk-src/test_client.py --src /sdk-src/aarch64 "$@"
fi

# Проверки (а)(б) в одном контейнере. ARCHDIR/LDSO — какую папку и каким загрузчиком гонять.
run_checks() {
  local platform=$1 image=$2 archdir=$3 ldso=$4
  echo "######## $archdir в $image ($platform)"
  docker run --rm -i --platform "$platform" --entrypoint sh -e ARCHDIR="$archdir" -e LDSO="$ldso" \
    -v "$HERE:/sdk-src:ro" "$image" -s <<'EOF'
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

rm -rf "$T"
exit $fail
EOF
}

rc=0
run_checks linux/arm64 "$HA_IMAGE" aarch64 ld-linux-aarch64.so.1 || rc=1
if [ "$(docker run --rm --platform linux/amd64 "$AMD64_IMAGE" uname -m 2>/dev/null || true)" = "x86_64" ]; then
  run_checks linux/amd64 "$AMD64_IMAGE" amd64 ld-linux-x86-64.so.2 || rc=1
else
  echo "######## amd64: docker не умеет x86-64 здесь — пропускаю (проверены только file/readelf в build.sh)"
fi
[ $rc -eq 0 ] && echo "== ВСЁ ОК" || echo "== ЕСТЬ ПРОВАЛЫ"
exit $rc
