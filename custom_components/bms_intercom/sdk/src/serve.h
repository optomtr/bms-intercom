/*
 * bms_talk --serve — постоянный помощник: вход на терминал один раз, голос открывается командой.
 *
 * Зачем: на HA Green NET_DVR_Init перебирает все сетевые адреса хоста («find 6 mac and 14 ip»,
 * ~3,3 с), Login_V40 — ещё ~2,2 с. Однократный режим платил это на каждый «Ответить», а на коде 11
 * (терминал в своём вызове) — ещё раз, новым процессом: голос включался через ~12 с. Здесь Init и
 * вход делаются при загрузке интеграции, а на «Ответить» остаются GetCurrentAudioCompress +
 * StartVoiceCom — доли секунды; код 11 можно повторять тем же процессом сразу.
 *
 * Протокол (пароль — только первой строкой stdin, как в однократном режиме; никогда не печатается):
 *   stdin, первая строка: JSON {"host","port","user","password"}.
 *   stdout: {"type":"ready","sdk":"…"} — вход выполнен; или {"type":"error",…} и выход 3.
 *   Дальше stdin — кадры: 1 байт тип + 4 байта длина (little-endian) + payload.
 *     S {"channel":1} → {"type":"started","codec":…} | {"type":"error","code":N,…} (процесс живёт);
 *     A <µ-law>       — звук в кольцевой буфер (пока голос открыт; иначе выбрасывается);
 *     E               → {"type":"stopped"} (StopVoiceCom, буфер очищен);
 *     P               → {"type":"pong","logged_in":bool,"voice":bool};
 *     Q или EOF       — StopVoiceCom/Logout/Cleanup, выход 0.
 *   Без запроса: {"type":"voice_lost","code":N,"message":…} — голос оборвался (сеть/терминал).
 *   Нарушен формат кадров — {"type":"error","code":-1,…} и выход 2 (дальше поток не разобрать).
 *
 * Подключается из bms_talk.c после общих частей (SDK, журнал, кольцевой буфер): одна единица
 * трансляции, всё static — как остальные заголовки здесь.
 */
#ifndef BMS_SERVE_H
#define BMS_SERVE_H

#include <poll.h>
#include <stdint.h>

#define SERVE_MAX_AUDIO 65536 /* кадр A больше этого — точно не кусок микрофона, а сбой потока */
#define SERVE_MAX_CMD 1024    /* payload S/E/P/Q */

static struct {
    talk_request rq;       /* нужен для повторного входа; пароль обнуляется при выходе */
    LONG uid, vh;
    int enc, alaw, fails;
    long block;            /* 1/153 при входе: больше не входим — повтор ведёт к блокировке */
    char block_msg[512];
    unsigned long long sent;
    struct timespec next;  /* когда слать следующий кадр (пока голос открыт) */
} sv = {.uid = -1, .vh = -1};

/* NET_DVR_SetExceptionCallBack_V30 — необязательна: без неё обрыв видно по ошибкам SDK. */
static BOOL (*set_exception_cb)(DWORD, void *, fExceptionCallBack, void *);

/* Из потока SDK (колбэк исключений) — только флаги. */
static int g_logged_in, g_voice_lost;
static LONG g_vh_now = -1;

static void on_exception(DWORD type, LONG uid, LONG handle, void *user)
{
    (void)user;
    switch (type) {
    case HIK_EXCEPTION_EXCHANGE:
    case HIK_EXCEPTION_RELOGIN:
        __atomic_store_n(&g_logged_in, 0, __ATOMIC_RELAXED);
        break;
    case HIK_RESUME_EXCHANGE:
    case HIK_RELOGIN_SUCCESS:
        __atomic_store_n(&g_logged_in, 1, __ATOMIC_RELAXED);
        break;
    case HIK_EXCEPTION_AUDIOEXCHANGE:
        /* Только про текущий сеанс: запоздалое исключение прошлого не должно рвать новый. */
        if (handle == __atomic_load_n(&g_vh_now, __ATOMIC_RELAXED))
            __atomic_store_n(&g_voice_lost, 1, __ATOMIC_RELAXED);
        break;
    default:
        break;
    }
    say("исключение SDK 0x%x (вход %d, сеанс %d)", type, uid, handle);
}

static void ring_clear(void)
{
    pthread_mutex_lock(&g_mu);
    g_head = g_len = 0;
    pthread_mutex_unlock(&g_mu);
}

/* Ошибки, после которых вход, скорее всего, потерян: сеть, таймаут, недействительный сеанс. */
static int login_lost(DWORD e)
{
    switch (e) {
    case 7: case 8: case 9: case 10: case 44: case 47: case 73: case 102: case 154:
        return 1;
    default:
        return 0;
    }
}

/* 0 — вход выполнен. Иначе код SDK в *code и готовый текст в msg; attempts/lock_s — для ответа. */
static int serve_login(long *code, char *msg, size_t cap, int *attempts, long *lock_s)
{
    NET_DVR_USER_LOGIN_INFO li;
    NET_DVR_DEVICEINFO_V40 dev;

    *attempts = -1;
    *lock_s = -1;
    if (sv.block) { /* неверный пароль уже был — повтор только приблизил бы блокировку (153) */
        *code = sv.block;
        snprintf(msg, cap, "%s", sv.block_msg);
        return -1;
    }
    memset(&li, 0, sizeof li);
    memset(&dev, 0, sizeof dev);
    memcpy(li.sDeviceAddress, sv.rq.host, strlen(sv.rq.host));
    li.wPort = (WORD)sv.rq.port;
    memcpy(li.sUserName, sv.rq.user, strlen(sv.rq.user));
    memcpy(li.sPassword, sv.rq.password, strlen(sv.rq.password));
    li.bUseAsynLogin = 0;
    li.byLoginMode = 0;
    say("вход на %s:%d как %s (постоянный режим)", sv.rq.host, sv.rq.port, sv.rq.user);
    sv.uid = sdk.Login_V40(&li, &dev);
    explicit_bzero(li.sPassword, sizeof li.sPassword);
    if (sv.uid >= 0) {
        __atomic_store_n(&g_logged_in, 1, __ATOMIC_RELAXED);
        say("вход выполнен");
        return 0;
    }
    DWORD e = sdk.GetLastError();
    *code = e ? (long)e : -2;
    snprintf(msg, cap, "%s", hik_error_text(e));
    if (dev.bySupportLock && (e == 1 || e == 153)) {
        *attempts = dev.byRetryLoginTime;
        snprintf(msg + strlen(msg), cap - strlen(msg), "; осталось попыток входа: %d", *attempts);
        if (e == 153) {
            *lock_s = (long)dev.dwSurplusLockTime;
            snprintf(msg + strlen(msg), cap - strlen(msg), "; до разблокировки: %ld с", *lock_s);
        }
    }
    if (e == 1 || e == 153) {
        sv.block = (long)e;
        snprintf(sv.block_msg, sizeof sv.block_msg, "%s; вход больше не повторяю", msg);
    }
    return -1;
}

/* Вход заново (сеанс потерян). 0 — ок; иначе ответ error уже выдан. */
static int serve_relogin(void)
{
    char msg[512];
    long code;
    int attempts;
    long lock_s;
    if (sv.uid >= 0) { sdk.Logout(sv.uid); sv.uid = -1; }
    if (serve_login(&code, msg, sizeof msg, &attempts, &lock_s) == 0) return 0;
    emit_error(code, msg, attempts, lock_s);
    return -1;
}

static void serve_voice_down(void)
{
    if (sv.vh >= 0) sdk.StopVoiceCom(sv.vh);
    say("голос закрыт: кадров отправлено=%llu, байт принято=%llu, выброшено байт=%llu", sv.sent,
        __atomic_load_n(&g_rx_dev, __ATOMIC_RELAXED), g_dropped);
    sv.vh = -1;
    __atomic_store_n(&g_vh_now, -1, __ATOMIC_RELAXED);
    ring_clear();
}

static void serve_voice_lost(DWORD code, const char *why)
{
    char esc[256], line[512];
    serve_voice_down();
    json_escape(esc, sizeof esc, why);
    snprintf(line, sizeof line, "{\"type\":\"voice_lost\",\"code\":%u,\"message\":\"%s\"}\n", code, esc);
    emit_line(line);
    say("голос оборвался: %s (код %u)", why, code);
}

static void serve_start(const char *payload, size_t len)
{
    char line[256];
    int ch, relogged = 0;
    const char *perr = parse_voice(payload, len, &ch);
    if (perr) { emit_error(-1, perr, -1, -1); return; }
    if (sv.vh >= 0) { /* уже открыт — повторный S ничего не ломает */
        snprintf(line, sizeof line, "{\"type\":\"started\",\"codec\":\"%s\"}\n", hik_codec_name(sv.enc));
        emit_line(line);
        return;
    }
    /* Колбэк сказал «связь пропала» (или входа нет) — войти заново до StartVoiceCom, один раз. */
    if (sv.uid < 0 || !__atomic_load_n(&g_logged_in, __ATOMIC_RELAXED)) {
        say("вход потерян — вхожу заново перед открытием голоса");
        if (serve_relogin()) return;
        relogged = 1;
    }
    for (;;) {
        NET_DVR_COMPRESSION_AUDIO ac;
        memset(&ac, 0, sizeof ac);
        sv.enc = 1;
        if (sdk.GetCurrentAudioCompress(sv.uid, &ac)) sv.enc = ac.byAudioEncType;
        else say("кодек не получен (код %u) — считаю G.711 µ-law, как у DS-K1T341AM", sdk.GetLastError());
        sv.vh = sdk.StartVoiceCom_MR_V30(sv.uid, (DWORD)ch, on_voice, NULL);
        if (sv.vh >= 0) break;
        DWORD e = sdk.GetLastError();
        if (!relogged && login_lost(e)) {
            say("StartVoiceCom: код %u — вход, похоже, потерян; вхожу заново", e);
            if (serve_relogin()) return;
            relogged = 1;
            continue;
        }
        emit_error((long)e, hik_error_text(e), -1, -1); /* 11/31 — терминал в своём вызове: повторит клиент */
        return;
    }
    sv.alaw = (sv.enc == 2);
    if (sv.alaw)
        for (int i = 0; i < 256; i++) g_u2a[i] = linear_to_alaw(ulaw_to_linear((unsigned char)i));
    if (sv.enc != 1 && sv.enc != 2) say("кодек терминала %s — мост умеет только G.711, шлю µ-law как есть", hik_codec_name(sv.enc));
    ring_clear();
    sv.fails = 0;
    sv.sent = 0;
    __atomic_store_n(&g_voice_lost, 0, __ATOMIC_RELAXED);
    __atomic_store_n(&g_vh_now, sv.vh, __ATOMIC_RELAXED);
    clock_gettime(CLOCK_MONOTONIC, &sv.next);
    ts_add(&sv.next, FRAME_NS);
    snprintf(line, sizeof line, "{\"type\":\"started\",\"codec\":\"%s\"}\n", hik_codec_name(sv.enc));
    emit_line(line);
    say("голос открыт, кодек %s", hik_codec_name(sv.enc));
}

/* Кадр каждые 20 мс по абсолютному времени, пока голос открыт (как главный цикл run_talk). */
static void serve_tick(void)
{
    struct timespec now;
    unsigned char frame[FRAME_BYTES];
    int eof;

    if (__atomic_exchange_n(&g_voice_lost, 0, __ATOMIC_RELAXED)) {
        serve_voice_lost(0, "SDK сообщил об обрыве голосового сеанса");
        return;
    }
    clock_gettime(CLOCK_MONOTONIC, &now);
    if (ts_diff(&now, &sv.next) < 0) return;
    if (ts_diff(&now, &sv.next) > RESYNC_NS) sv.next = now; /* машина «заснула» — не догоняем пачкой */
    ts_add(&sv.next, FRAME_NS);
    if (!ring_pop_frame(frame, &eof)) return; /* тишина от клиента — ничего не шлём */
    if (sv.alaw)
        for (int i = 0; i < FRAME_BYTES; i++) frame[i] = g_u2a[frame[i]];
    if (sdk.VoiceComSendData(sv.vh, (char *)frame, FRAME_BYTES)) { sv.sent++; sv.fails = 0; return; }
    DWORD e = sdk.GetLastError();
    if (++sv.fails == 1) say("VoiceComSendData: код %u", e);
    if (sv.fails >= MAX_SEND_FAILS) serve_voice_lost(e, "связь с терминалом потеряна");
}

/* Разбор кадров stdin. Возврат: 0 — дальше, -1 — Q (норма), 2 — поток сломан. */
static struct {
    unsigned char hdr[5];
    size_t hdr_n;
    int type;
    uint32_t len, got;
    char cmd[SERVE_MAX_CMD];
} fp;

static int serve_dispatch(void)
{
    char line[160];
    switch (fp.type) {
    case 'S':
        serve_start(fp.cmd, fp.len);
        return 0;
    case 'A':
        return 0; /* звук уже ушёл в буфер по мере прихода */
    case 'E':
        if (sv.vh >= 0) serve_voice_down();
        emit_line("{\"type\":\"stopped\"}\n");
        return 0;
    case 'P':
        snprintf(line, sizeof line, "{\"type\":\"pong\",\"logged_in\":%s,\"voice\":%s}\n",
                 sv.uid >= 0 && __atomic_load_n(&g_logged_in, __ATOMIC_RELAXED) ? "true" : "false",
                 sv.vh >= 0 ? "true" : "false");
        emit_line(line);
        return 0;
    default: /* 'Q' */
        return -1;
    }
}

static int serve_feed(const unsigned char *d, size_t n)
{
    while (n > 0) {
        if (fp.hdr_n < sizeof fp.hdr) {
            size_t k = sizeof fp.hdr - fp.hdr_n;
            if (k > n) k = n;
            memcpy(fp.hdr + fp.hdr_n, d, k);
            fp.hdr_n += k;
            d += k;
            n -= k;
            if (fp.hdr_n < sizeof fp.hdr) return 0;
            fp.type = fp.hdr[0];
            fp.len = (uint32_t)fp.hdr[1] | (uint32_t)fp.hdr[2] << 8 | (uint32_t)fp.hdr[3] << 16 | (uint32_t)fp.hdr[4] << 24;
            fp.got = 0;
            if (!strchr("SAEPQ", fp.type) || fp.type == 0 ||
                fp.len > (uint32_t)(fp.type == 'A' ? SERVE_MAX_AUDIO : SERVE_MAX_CMD - 1)) {
                emit_error(-1, "поток команд сломан (неизвестный кадр или длина)", -1, -1);
                return 2;
            }
        } else {
            size_t k = fp.len - fp.got;
            if (k > n) k = n;
            if (fp.type == 'A') {
                if (sv.vh >= 0) ring_push(d, k); /* голос закрыт — звук некуда слать */
            } else {
                memcpy(fp.cmd + fp.got, d, k);
            }
            fp.got += (uint32_t)k;
            d += k;
            n -= k;
        }
        if (fp.hdr_n == sizeof fp.hdr && fp.got == fp.len) {
            int r = serve_dispatch();
            fp.hdr_n = 0;
            if (r) return r;
        }
    }
    return 0;
}

static int serve_loop(void)
{
    unsigned char buf[4096];
    struct pollfd pfd = {.fd = 0, .events = POLLIN};

    while (!g_stop) {
        struct timespec now, tmo, *tp = NULL;
        if (sv.vh >= 0) { /* голос открыт — просыпаемся к следующему кадру; иначе ждём stdin */
            clock_gettime(CLOCK_MONOTONIC, &now);
            long d = ts_diff(&sv.next, &now);
            if (d < 0) d = 0;
            tmo.tv_sec = d / 1000000000L;
            tmo.tv_nsec = d % 1000000000L;
            tp = &tmo;
        }
        int r = ppoll(&pfd, 1, tp, NULL);
        if (r < 0 && errno != EINTR) { say("ppoll: %s", strerror(errno)); return 2; }
        if (r > 0) {
            ssize_t got = read(0, buf, sizeof buf);
            if (got == 0) { say("stdin закрыт — выхожу"); return 0; }
            if (got < 0 && errno != EINTR) { say("stdin: %s", strerror(errno)); return 2; }
            if (got > 0) {
                int f = serve_feed(buf, (size_t)got);
                if (f < 0) { say("команда Q — выхожу"); return 0; }
                if (f) return f;
            }
        }
        if (sv.vh >= 0) serve_tick();
    }
    return 0;
}

static int run_serve(const char *dir, const char *log_dir)
{
    char line[2048], msg[512], ver[32];
    long code;
    int attempts, rc;
    long lock_s;

    ssize_t n = read_line(line, sizeof line);
    const char *perr = n < 0 ? "нет первой строки с параметрами (JSON) или она длиннее 2 КБ"
                             : parse_request(line, (size_t)n, &sv.rq);
    explicit_bzero(line, sizeof line); /* в строке был пароль */
    if (perr) { explicit_bzero(&sv.rq, sizeof sv.rq); emit_error(-1, perr, -1, -1); return 2; }

    if (sdk_load(dir)) { explicit_bzero(&sv.rq, sizeof sv.rq); emit_error(-2, g_err, -1, -1); return 2; }
    if (sdk_init(dir, log_dir)) {
        DWORD e = sdk.GetLastError();
        explicit_bzero(&sv.rq, sizeof sv.rq);
        snprintf(msg, sizeof msg, "NET_DVR_Init не удался: %s", hik_error_text(e));
        emit_error(e ? (long)e : -2, msg, -1, -1);
        return 2;
    }
    /* В отличие от однократного режима — переподключение SDK включено: вход живёт часами, и после
       перезагрузки терминала или сбоя сети SDK входит сам (тем же, уже проверенным паролем).
       Сменили пароль на терминале — поменяйте его и в интеграции: перезагрузка перезапустит помощника. */
    sdk.SetReconnect(10000, 1);
    *(void **)&set_exception_cb = dlsym(sdk.h, "NET_DVR_SetExceptionCallBack_V30");
    if (!set_exception_cb) say("в HCNetSDK нет NET_DVR_SetExceptionCallBack_V30 — обрыв узнаю по ошибкам");
    else if (!set_exception_cb(0, NULL, on_exception, NULL)) say("колбэк исключений SDK не принят: код %u", sdk.GetLastError());

    if (serve_login(&code, msg, sizeof msg, &attempts, &lock_s)) {
        emit_error(code, msg, attempts, lock_s);
        explicit_bzero(&sv.rq, sizeof sv.rq);
        sdk.Cleanup();
        return 3;
    }
    sdk_version(ver, sizeof ver);
    snprintf(msg, sizeof msg, "{\"type\":\"ready\",\"sdk\":\"%s\"}\n", ver);
    emit_line(msg);

    rc = serve_loop();

    if (sv.vh >= 0) serve_voice_down();
    if (sv.uid >= 0) sdk.Logout(sv.uid);
    sdk.Cleanup();
    explicit_bzero(&sv.rq, sizeof sv.rq);
    say("постоянный помощник завершён (выход %d)", rc);
    return rc;
}

#endif
