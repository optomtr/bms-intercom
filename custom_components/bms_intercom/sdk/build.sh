#!/usr/bin/env bash
# Воспроизводимая сборка помощника bms_talk и его рантайма для aarch64/ и amd64/.
#
# Почему так: HA Core — Alpine (musl), а HCNetSDK собран под glibc. Поэтому рядом с помощником
# везём ВСЁ его окружение: загрузчик glibc, libc/libstdc++ и прочее из Debian bookworm, сам SDK
# и OpenAL. Интеграция запускает: DIR/ld-linux-*.so.* --library-path DIR DIR/bms_talk
#
# Требует только docker (colima) и git. Сеть нужна при первом запуске: образ Debian, .deb-пакеты
# и исходник SDK (pergolafabio/Hikvision-Addons на закреплённом коммите). Всё собирается в
# arm64-контейнере: aarch64 — родным gcc, amd64 — кросс-компилятором (эмуляция x86 не нужна).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# Коммит Hikvision-Addons, из которого взяты библиотеки SDK:
#   aarch64 — HCNetSDK V6.1.8.101 (именно он проверен на DS-K1T341AM), amd64 — V6.1.6.3.
SDK_PIN=25230919dacb33800aa84ba90beafb422ee97ee2
SDK_REPO=https://github.com/pergolafabio/Hikvision-Addons.git
# Кэш — под $HOME: colima по умолчанию монтирует в VM только домашний каталог.
CACHE="${BMS_TALK_CACHE:-$HOME/.cache/bms-talk}"
SDK_SRC="$CACHE/hikaddons"
IMAGE=bms-talk-builder:bookworm

if ! docker info >/dev/null 2>&1; then
  echo "Docker не отвечает — запускаю colima..."
  colima start
fi

# ---- 1. исходник SDK на закреплённом коммите
if [ "$(git -C "$SDK_SRC" rev-parse HEAD 2>/dev/null || true)" != "$SDK_PIN" ]; then
  echo "== скачиваю Hikvision-Addons@$SDK_PIN"
  rm -rf "$SDK_SRC"
  mkdir -p "$SDK_SRC"
  git -C "$SDK_SRC" init -q
  git -C "$SDK_SRC" remote add origin "$SDK_REPO"
  git -C "$SDK_SRC" fetch -q --depth 1 origin "$SDK_PIN"
  git -C "$SDK_SRC" checkout -q FETCH_HEAD
fi

# ---- 2. образ-сборщик: компиляторы + .deb рантайма обеих архитектур (кэшируется docker'ом)
echo "== образ-сборщик $IMAGE"
docker build -q --platform linux/arm64 -t "$IMAGE" - >/dev/null <<'EOF'
FROM debian:bookworm
RUN dpkg --add-architecture amd64 && apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev binutils file \
      gcc-x86-64-linux-gnu libc6-dev-amd64-cross \
 && mkdir -p /debs/aarch64 /debs/amd64 \
 && cd /debs/aarch64 && apt-get download libc6:arm64 libstdc++6:arm64 libgcc-s1:arm64 libuuid1:arm64 \
      libopenal1:arm64 libsndio7.0:arm64 libasound2:arm64 libbsd0:arm64 libmd0:arm64 \
 && cd /debs/amd64 && apt-get download libc6:amd64 libstdc++6:amd64 libgcc-s1:amd64 libuuid1:amd64 \
 && rm -rf /var/lib/apt/lists/*
EOF

# ---- 3. сборка внутри контейнера
echo "== сборка"
docker run --rm -i --platform linux/arm64 -e SDK_PIN="$SDK_PIN" \
  -v "$HERE:/out" -v "$SDK_SRC/hikvision-doorbell:/sdk:ro" "$IMAGE" \
  bash -euo pipefail -s <<'INSIDE'
CFLAGS="-O2 -std=gnu11 -Wall -Wextra -Werror -fstack-protector-strong -D_FORTIFY_SOURCE=2 -fPIE"
LDFLAGS="-pie -s -Wl,-z,relro,-z,now -ldl -lpthread"

# Общее для обеих архитектур: ядро SDK и голосовые компоненты.
# НЕ кладём: libPlayCtrl/libSuperRender/libAudioRender (видео, GL/X11), libHCPreview/PlayBack/
# Display/Alarm/Industry/GeneralCfgMgr, StreamTransClient/SystemTransform/analyzedata/NPQos —
# ни вход, ни голос их не используют (в журнале проверочного разговора грузился только VoiceTalk).
SDK_CORE="libhcnetsdk.so libHCCore.so libhpr.so libz.so"
SDK_COM="libHCCoreDevCfg.so libHCVoiceTalk.so libAudioIntercom.so"
GLIBC="libc.so.6 libm.so.6 libdl.so.2 libpthread.so.0 librt.so.1 libstdc++.so.6 libgcc_s.so.1 libuuid.so.1"

# Путь внутри распакованных .deb, с разворотом символьных ссылок относительно корня распаковки
# (абсолютная ссылка иначе указала бы на файл самого контейнера-сборщика).
resolve() {
  local root=$1 p=$2 t i=0
  while [ -L "$p" ]; do
    t=$(readlink "$p")
    case $t in /*) p="$root$t" ;; *) p="$(dirname "$p")/$t" ;; esac
    i=$((i + 1)); [ $i -lt 16 ] || return 1
  done
  [ -f "$p" ] && echo "$p"
}

for ARCH in aarch64 amd64; do
  case $ARCH in
    aarch64) CC=gcc; TRIPLE=aarch64-linux-gnu; LDSO=ld-linux-aarch64.so.1; FILEPAT="ARM aarch64"
             SSL="libcrypto.so.1.1 libssl.so.1.1"; ICONV=libiconv.so.2
             EXTRA_RT="libopenal.so.1 libsndio.so.7.0 libasound.so.2 libbsd.so.0 libmd.so.0"; EXTRA_SDK="" ;;
    amd64)   CC=x86_64-linux-gnu-gcc; TRIPLE=x86_64-linux-gnu; LDSO=ld-linux-x86-64.so.2; FILEPAT="x86-64"
             # libcrypto.so — копия 1.0.0 под «голым» именем, как у апстрима (SDK 6.1.6.3 может
             # искать именно его); OpenAL — собственная сборка Hikvision без внешних зависимостей.
             SSL="libcrypto.so.1.0.0 libcrypto.so libssl.so"; ICONV=libiconv2.so
             EXTRA_RT=""; EXTRA_SDK="libopenal.so.1" ;;
  esac
  OUT=/out/$ARCH ROOT=/tmp/rt-$ARCH
  echo "---- $ARCH"
  rm -rf "$OUT" "$ROOT"; mkdir -p "$OUT/HCNetSDKCom" "$ROOT"
  for d in /debs/$ARCH/*.deb; do dpkg-deb -x "$d" "$ROOT"; done

  # помощник
  $CC $CFLAGS -o "$OUT/bms_talk" /out/src/bms_talk.c $LDFLAGS

  # рантайм glibc и OpenAL из Debian bookworm
  for so in $LDSO $GLIBC $EXTRA_RT; do
    src=""
    for dir in "$ROOT/lib/$TRIPLE" "$ROOT/usr/lib/$TRIPLE"; do
      [ -e "$dir/$so" ] && src=$(resolve "$ROOT" "$dir/$so") && break
    done
    [ -n "$src" ] || { echo "НЕТ $so в пакетах $ARCH"; exit 1; }
    cp "$src" "$OUT/$so"
  done

  # SDK
  for so in $SDK_CORE $SSL $EXTRA_SDK; do cp "/sdk/lib-$ARCH/$so" "$OUT/$so"; done
  for so in $SDK_COM $ICONV; do cp "/sdk/lib-$ARCH/HCNetSDKCom/$so" "$OUT/HCNetSDKCom/$so"; done
  chmod 0755 "$OUT/bms_talk" "$OUT/$LDSO"
  find "$OUT" -name '*.so*' ! -name "$LDSO" -exec chmod 0644 {} +

  # проверка 1: все файлы — ELF нужной архитектуры
  bad=$(find "$OUT" -type f ! -name MANIFEST.txt -exec file {} + | grep -v "$FILEPAT" || true)
  [ -z "$bad" ] || { echo "НЕ та архитектура:"; echo "$bad"; exit 1; }

  # проверка 2: каждая NEEDED-зависимость лежит рядом (в DIR или в каталоге самой .so — RPATH $ORIGIN)
  miss=0
  for f in "$OUT"/bms_talk "$OUT"/*.so* "$OUT"/HCNetSDKCom/*.so*; do
    for need in $(readelf -d "$f" | sed -n 's/.*(NEEDED).*\[\(.*\)\]/\1/p'); do
      [ -e "$OUT/$need" ] || [ -e "$(dirname "$f")/$need" ] || { echo "  $f: нет $need"; miss=1; }
    done
  done
  [ $miss -eq 0 ] || { echo "Не хватает зависимостей ($ARCH)"; exit 1; }

  # опись: откуда что и контрольные суммы — чтобы пересборку можно было сверить
  {
    echo "# bms_talk $ARCH — собрано build.sh; не править руками"
    echo "# HCNetSDK: pergolafabio/Hikvision-Addons@$SDK_PIN hikvision-doorbell/lib-$ARCH"
    for d in /debs/$ARCH/*.deb; do echo "# deb: $(dpkg-deb -f "$d" Package Version | paste -sd' ')"; done
    echo "# gcc: $($CC -dumpfullversion)"
    (cd "$OUT" && find . -type f ! -name MANIFEST.txt | sort | xargs sha256sum)
  } > "$OUT/MANIFEST.txt"
  echo "  $(du -sh "$OUT" | cut -f1)  $(find "$OUT" -type f | wc -l) файлов"
done
INSIDE
echo "== готово: $HERE/aarch64 и $HERE/amd64"
