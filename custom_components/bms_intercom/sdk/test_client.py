#!/usr/bin/env python3
"""Проверка помощника bms_talk владельцем: 3 с «бип-бип» на терминал через HCNetSDK.

Запускается ВНУТРИ образа Home Assistant (его Python на Alpine) — так же, как будет работать
интеграция: копия папки помощника во временный каталог, chmod +x (HACS теряет бит исполнения),
запуск через загрузчик glibc. Только стандартная библиотека Python.

Пароль спрашивается скрытым вводом и передаётся помощнику ТОЛЬКО первой строкой stdin
(не в argv и не в окружении — их видно другим процессам). Нигде не сохраняется и не печатается.

Обычно запускается из test_in_ha.sh:  ./test_in_ha.sh talk
"""
import argparse
import getpass
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# platform.machine() → (папка помощника, имя загрузчика glibc)
ARCHES = {"aarch64": ("aarch64", "ld-linux-aarch64.so.1"), "x86_64": ("amd64", "ld-linux-x86-64.so.2")}
FRAME = 160  # 20 мс G.711 при 8 кГц — такими кадрами помощник шлёт в SDK


def lin2ulaw(pcm):
    """16-битный PCM → байт G.711 µ-law (классический g711.c, как в проверенном probe.py)."""
    seg_end = (0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF)
    pcm >>= 2
    if pcm < 0:
        pcm, mask = -pcm, 0x7F
    else:
        mask = 0xFF
    pcm = min(pcm, 8159) + (0x84 >> 2)
    seg = next((i for i, end in enumerate(seg_end) if pcm <= end), 8)
    if seg >= 8:
        return 0x7F ^ mask
    return ((seg << 4) | ((pcm >> (seg + 1)) & 0x0F)) ^ mask


def make_beeps(seconds, rate=8000, freq=1000.0, on_ms=250, off_ms=250, amp=12000):
    """«Бип-бип»: 250 мс тон 1 кГц / 250 мс тишина — именно его владелец уже слышал на терминале."""
    period = rate * (on_ms + off_ms) // 1000
    on = rate * on_ms // 1000
    return bytes(
        lin2ulaw(int(amp * math.sin(2 * math.pi * freq * n / rate)) if (n % period) < on else 0)
        for n in range(int(rate * seconds))
    )


def prepare(src):
    """Копия помощника во временный каталог + chmod, как будет делать интеграция."""
    machine = platform.machine()
    if machine not in ARCHES:
        sys.exit(f"[x] Неподдерживаемая архитектура: {machine}")
    folder, ldso = ARCHES[machine]
    src = src or os.path.join(HERE, folder)
    tmp = tempfile.mkdtemp(prefix="bms_talk_")
    dst = os.path.join(tmp, folder)
    shutil.copytree(src, dst)
    for name in (ldso, "bms_talk"):
        os.chmod(os.path.join(dst, name), 0o755)
    return tmp, [os.path.join(dst, ldso), "--library-path", dst, os.path.join(dst, "bms_talk")]


def main():
    ap = argparse.ArgumentParser(description="3 с бип на терминал Hikvision через помощник bms_talk")
    ap.add_argument("--src", help="папка помощника (aarch64/ или amd64/); по умолчанию — рядом")
    ap.add_argument("--host", default="192.168.70.121")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--user", default="admin")
    ap.add_argument("--channel", type=int, default=1)
    ap.add_argument("--seconds", type=float, default=3.0)
    a = ap.parse_args()

    if not sys.stdin.isatty():
        sys.exit("[x] Нужен терминал для скрытого ввода пароля (docker run -it ...)")
    password = getpass.getpass(f"Пароль {a.user}@{a.host} (ввод не отображается): ")
    if not password:
        sys.exit("[x] Пустой пароль")

    tmp, cmd = prepare(a.src)
    summary = {"итог": None}
    p = None
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        def pump_stderr():
            # журнал помощника — на экран; строку «итог:» запоминаем для сводки
            for raw in p.stderr:
                line = raw.decode("utf-8", "replace").rstrip()
                print(f"    {line}", file=sys.stderr, flush=True)
                if "итог:" in line:
                    summary["итог"] = line

        threading.Thread(target=pump_stderr, daemon=True).start()
        # Сторож: если помощник завис на входе — не держим владельца вечно.
        watchdog = threading.Timer(30.0, p.kill)
        watchdog.start()

        req = {"host": a.host, "port": a.port, "user": a.user, "password": password, "channel": a.channel}
        p.stdin.write((json.dumps(req) + "\n").encode())
        p.stdin.flush()
        del req, password

        first = p.stdout.readline().decode("utf-8", "replace").strip()
        watchdog.cancel()
        try:
            msg = json.loads(first)
        except ValueError:
            print(f"[x] Помощник не ответил JSON-строкой: {first!r}")
            p.kill()
            return 2
        if msg.get("type") != "started":
            p.wait(timeout=10)
            print("\n======== ИТОГ ========")
            print(f"Разговор НЕ начат: код {msg.get('code')} — {msg.get('message')}")
            if msg.get("code") == 1:
                print("ВНИМАНИЕ: после нескольких неверных паролей терминал блокирует пользователя.")
            return 3

        print(f"[+] Разговор начат, кодек терминала: {msg.get('codec')}")
        audio = make_beeps(a.seconds)
        frames = 0
        t_next = time.monotonic()
        try:
            for off in range(0, len(audio), FRAME):
                p.stdin.write(audio[off:off + FRAME])
                p.stdin.flush()
                frames += 1
                t_next += FRAME / 8000.0
                delay = t_next - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
        except BrokenPipeError:
            print("[!] Помощник закрыл вход раньше времени")
        print(f"[i] Отдано помощнику кадров: {frames} ({frames * FRAME} байт µ-law), закрываю stdin")
        try:
            p.stdin.close()
        except BrokenPipeError:
            pass
        rc = p.wait(timeout=15)
        time.sleep(0.2)  # дочитать последнюю строку журнала

        print("\n======== ИТОГ ========")
        print(f"Кодек терминала:  {msg.get('codec')}")
        print(f"Отдано кадров:    {frames}")
        print(f"Выход помощника:  {rc} ({'норма' if rc == 0 else 'связь потеряна' if rc == 4 else 'ошибка'})")
        m = re.search(r"кадров отправлено=(\d+), байт принято=(\d+)", summary["итог"] or "")
        if m:
            print(f"Отправлено в SDK: {m.group(1)} кадров")
            print(f"Пришло с терминала: {m.group(2)} байт (звук с микрофона терминала)")
        print("Слышен ли «бип-бип» на терминале — проверьте ушами.")
        return 0 if rc == 0 else 4
    except KeyboardInterrupt:
        print("\n[!] Прервано")
        return 130
    finally:
        if p is not None and p.poll() is None:
            p.terminate()  # SIGTERM: помощник сам сделает StopVoiceCom/Logout/Cleanup
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
