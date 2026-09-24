/*
 * bms_talk — помощник «голос оператора → терминал Hikvision» через HCNetSDK (приватный протокол, порт 8000).
 *
 * Зачем отдельная программа: HA Core работает в Alpine (musl), а HCNetSDK собран под glibc и
 * напрямую в Python HA не грузится. Поэтому интеграция везёт рядом свой загрузчик glibc и все .so
 * и запускает помощник так (каталог DIR — копия aarch64/ или amd64/ с chmod +x):
 *     DIR/ld-linux-aarch64.so.1 --library-path DIR DIR/bms_talk      (aarch64)
 *     DIR/ld-linux-x86-64.so.2  --library-path DIR DIR/bms_talk      (amd64)
 * С SDK не линкуемся — грузим libhcnetsdk.so через dlopen: так бинарник не зависит от пути установки.
 *
 * Протокол (stdin/stdout):
 *   1) stdin, первая строка — JSON {"host","port","user","password","channel"}. Пароль ТОЛЬКО так
 *      (argv и окружение видны другим процессам через /proc). Пароль никогда не печатается.
 *   2) Init → Login_V40 → GetCurrentAudioCompress → StartVoiceCom_MR_V30 → в stdout ОДНА строка:
 *      {"type":"started","codec":"G.711ulaw"} или {"type":"error","code":N,"message":"…"} и выход.
 *   3) Дальше stdin — сырой G.711 µ-law 8 кГц любыми кусками. Отправка — ровно 160 байт (20 мс)
 *      по монотонному таймеру; буфер ≤ 2 с, старое выбрасывается (задержка не копится).
 *   4) EOF stdin — досылаем остаток буфера и выходим; SIGTERM/SIGINT — выходим сразу.
 *      StopVoiceCom → Logout → Cleanup. Коды выхода: 0 — норма, 2 — ошибка запроса/SDK,
 *      3 — ошибка входа/старта (JSON error уже выдан), 4 — связь с терминалом потеряна в разговоре.
 *   Свои коды ошибок (не SDK): -1 неверный запрос, -2 SDK не загрузился, -3 остановлен до начала.
 *   --selftest — Init + проверка голосовых компонентов, JSON-строка с версией SDK, выход (без терминала).
 *   Журнал — коротко в stderr. --sdk-log DIR — подробный журнал самого SDK в каталог (для разбора).
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <limits.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "hik_sdk.h"
#include "jsonline.h"
#include "g711.h"

#define FRAME_BYTES 160           /* 20 мс G.711 при 8 кГц — именно такими кадрами терминал услышал бип */
#define FRAME_NS 20000000L
#define RING_BYTES 16000          /* 2 с: больше не держим, иначе оператор слышит себя с опозданием */
#define MAX_SEND_FAILS 25         /* 0,5 с подряд без отправки — считаем, что связь с терминалом потеряна */
#define RESYNC_NS 100000000L      /* отстали больше чем на 100 мс (машина «заснула») — не догоняем пачкой */

#if defined(__aarch64__)
#define ARCH_NAME "aarch64"
#elif defined(__x86_64__)
#define ARCH_NAME "amd64"
#else
#define ARCH_NAME "unknown"
#endif

static volatile sig_atomic_t g_stop; /* сигнал — выходим без досылки буфера */
static int g_out = -1;               /* настоящий stdout — только для JSON-строки протокола */

/* ------------------------------------------------------------------ журнал и ответ */
static void say(const char *fmt, ...)
{
    va_list ap;
    char buf[512];
    va_start(ap, fmt);
    vsnprintf(buf, sizeof buf, fmt, ap);
    va_end(ap);
    fprintf(stderr, "bms_talk: %s\n", buf);
}

static void emit_line(const char *s)
{
    size_t n = strlen(s), off = 0;
    while (off < n) {
        ssize_t w = write(g_out, s + off, n - off);
        if (w < 0 && errno == EINTR) continue;
        if (w <= 0) return; /* читатель ушёл — дальше некому отвечать */
        off += (size_t)w;
    }
}

/* attempts / lock_s < 0 — поле не выводим. */
static void emit_error(long code, const char *msg, int attempts, long lock_s)
{
    char esc[768], line[1024], extra[96] = "";
    json_escape(esc, sizeof esc, msg);
    if (attempts >= 0)
        snprintf(extra, sizeof extra, ",\"attempts_left\":%d", attempts);
    if (lock_s >= 0)
        snprintf(extra + strlen(extra), sizeof extra - strlen(extra), ",\"lock_seconds\":%ld", lock_s);
    snprintf(line, sizeof line, "{\"type\":\"error\",\"code\":%ld,\"message\":\"%s\"%s}\n", code, esc, extra);
    emit_line(line);
    say("ошибка %ld: %s", code, msg);
}

/* ------------------------------------------------------------------ SDK через dlopen */
static struct {
    void *h;
    BOOL (*SetSDKInitCfg)(int, void *);
    BOOL (*Init)(void);
    BOOL (*Cleanup)(void);
    BOOL (*SetLogToFile)(DWORD, char *, BOOL);
    BOOL (*SetConnectTime)(DWORD, DWORD);
    BOOL (*SetReconnect)(DWORD, BOOL);
    DWORD (*GetSDKBuildVersion)(void);
    DWORD (*GetLastError)(void);
    LONG (*Login_V40)(NET_DVR_USER_LOGIN_INFO *, NET_DVR_DEVICEINFO_V40 *);
    BOOL (*Logout)(LONG);
    BOOL (*GetCurrentAudioCompress)(LONG, NET_DVR_COMPRESSION_AUDIO *);
    LONG (*StartVoiceCom_MR_V30)(LONG, DWORD, fVoiceDataCallBack, void *);
    BOOL (*VoiceComSendData)(LONG, char *, DWORD);
    BOOL (*StopVoiceCom)(LONG);
} sdk;

static char g_err[640]; /* текст последней ошибки загрузки */

/* Склеить путь из трёх частей; -1 — не влез (честная ошибка лучше обрезанного пути). */
static int pjoin(char *out, size_t cap, const char *a, const char *b, const char *c)
{
    int n = snprintf(out, cap, "%s%s%s", a, b, c);
    return (n < 0 || (size_t)n >= cap) ? -1 : 0;
}

static int sdk_load(const char *dir)
{
    char path[PATH_MAX];
    if (pjoin(path, sizeof path, dir, "/libhcnetsdk.so", "")) {
        snprintf(g_err, sizeof g_err, "слишком длинный путь к SDK");
        return -1;
    }
    /* RTLD_NOW|RTLD_LOCAL — как ctypes в проверенном probe.py: все символы разрешаются сразу. */
    sdk.h = dlopen(path, RTLD_NOW | RTLD_LOCAL);
    if (!sdk.h) {
        snprintf(g_err, sizeof g_err, "не загрузился HCNetSDK: %s", dlerror());
        return -1;
    }
#define SYM(name)                                                                     \
    do {                                                                              \
        *(void **)&sdk.name = dlsym(sdk.h, "NET_DVR_" #name);                         \
        if (!sdk.name) {                                                              \
            snprintf(g_err, sizeof g_err, "в HCNetSDK нет функции NET_DVR_" #name);   \
            return -1;                                                                \
        }                                                                             \
    } while (0)
    SYM(SetSDKInitCfg); SYM(Init); SYM(Cleanup); SYM(SetLogToFile); SYM(SetConnectTime);
    SYM(SetReconnect); SYM(GetSDKBuildVersion); SYM(GetLastError); SYM(Login_V40); SYM(Logout);
    SYM(GetCurrentAudioCompress); SYM(StartVoiceCom_MR_V30); SYM(VoiceComSendData); SYM(StopVoiceCom);
#undef SYM
    return 0;
}

/* OpenSSL у сборок SDK называется по-разному: aarch64 — 1.1, amd64 — 1.0.0 / без версии. */
static void pick_lib(char *out, size_t cap, const char *dir, const char *const *names)
{
    out[0] = 0;
    for (; *names; names++) {
        if (pjoin(out, cap, dir, "/", *names) == 0 && access(out, R_OK) == 0) return;
    }
    out[0] = 0;
}

static int sdk_init(const char *dir, const char *log_dir)
{
    static NET_DVR_LOCAL_SDK_PATH sp; /* SDK может хранить указатель — держим статическими */
    static char crypto[PATH_MAX], ssl[PATH_MAX];
    static const char *const cryptos[] = {"libcrypto.so.1.1", "libcrypto.so.1.0.0", "libcrypto.so", NULL};
    static const char *const ssls[] = {"libssl.so.1.1", "libssl.so.1.0.0", "libssl.so", NULL};

    /* Пути — до NET_DVR_Init, иначе SDK ищет HCNetSDKCom/ и OpenSSL в текущем каталоге процесса. */
    if (pjoin(sp.sPath, sizeof sp.sPath, dir, "/", "")) return -1; /* main() ограничивает длину */
    if (!sdk.SetSDKInitCfg(NET_SDK_INIT_CFG_SDK_PATH, &sp)) say("SetSDKInitCfg(путь SDK) не принят");
    pick_lib(crypto, sizeof crypto, dir, cryptos);
    pick_lib(ssl, sizeof ssl, dir, ssls);
    if (crypto[0] && !sdk.SetSDKInitCfg(NET_SDK_INIT_CFG_LIBEAY_PATH, crypto)) say("SetSDKInitCfg(libcrypto) не принят");
    if (ssl[0] && !sdk.SetSDKInitCfg(NET_SDK_INIT_CFG_SSLEAY_PATH, ssl)) say("SetSDKInitCfg(libssl) не принят");

    if (!sdk.Init()) return -1;
    if (log_dir) {
        static char ld[PATH_MAX];
        if (pjoin(ld, sizeof ld, log_dir, "/", "") || !sdk.SetLogToFile(3, ld, 1)) say("журнал SDK не включился: код %u", sdk.GetLastError());
    }
    /* 3 с на соединение, одна попытка, без авто-переподключения: SDK не должен сам повторять вход
       (каждая неудачная попытка приближает блокировку пользователя на терминале). */
    sdk.SetConnectTime(3000, 1);
    sdk.SetReconnect(10000, 0);
    return 0;
}

static void sdk_version(char *out, size_t cap)
{
    DWORD b = sdk.GetSDKBuildVersion();
    snprintf(out, cap, "%u.%u.%u.%u", b >> 24, (b >> 16) & 0xFF, (b >> 8) & 0xFF, b & 0xFF);
}

static unsigned char g_u2a[256]; /* µ-law → A-law, заполняется, только если терминал в A-law */

/* ------------------------------------------------------------------ кольцевой буфер stdin → SDK */
static unsigned char g_ring[RING_BYTES];
static size_t g_head, g_len;
static int g_eof;
static unsigned long long g_dropped;
static pthread_mutex_t g_mu = PTHREAD_MUTEX_INITIALIZER;

static void ring_push(const unsigned char *d, size_t n)
{
    pthread_mutex_lock(&g_mu);
    if (n >= RING_BYTES) { /* кусок больше всего буфера — оставляем только его хвост */
        g_dropped += g_len + (n - RING_BYTES);
        d += n - RING_BYTES;
        n = RING_BYTES;
        g_head = g_len = 0;
    } else if (g_len + n > RING_BYTES) { /* выбрасываем самое старое */
        size_t drop = g_len + n - RING_BYTES;
        g_head = (g_head + drop) % RING_BYTES;
        g_len -= drop;
        g_dropped += drop;
    }
    size_t tail = (g_head + g_len) % RING_BYTES, first = RING_BYTES - tail;
    if (first > n) first = n;
    memcpy(g_ring + tail, d, first);
    memcpy(g_ring, d + first, n - first);
    g_len += n;
    pthread_mutex_unlock(&g_mu);
}

/* 1 — кадр взят; 0 — кадра нет (в *eof — закончился ли stdin). */
static int ring_pop_frame(unsigned char *out, int *eof)
{
    pthread_mutex_lock(&g_mu);
    *eof = g_eof;
    if (g_len < FRAME_BYTES) { pthread_mutex_unlock(&g_mu); return 0; }
    size_t first = RING_BYTES - g_head;
    if (first > FRAME_BYTES) first = FRAME_BYTES;
    memcpy(out, g_ring + g_head, first);
    memcpy(out + first, g_ring, FRAME_BYTES - first);
    g_head = (g_head + FRAME_BYTES) % RING_BYTES;
    g_len -= FRAME_BYTES;
    pthread_mutex_unlock(&g_mu);
    return 1;
}

static void *reader_main(void *arg)
{
    unsigned char buf[4096];
    (void)arg;
    for (;;) {
        ssize_t r = read(0, buf, sizeof buf);
        if (r > 0) { ring_push(buf, (size_t)r); continue; }
        if (r < 0 && errno == EINTR) continue;
        break; /* EOF или ошибка — голос закончился */
    }
    pthread_mutex_lock(&g_mu);
    g_eof = 1;
    pthread_mutex_unlock(&g_mu);
    return NULL;
}

/* Первая строка stdin — по байту, чтобы не «съесть» в буфер stdio начало звука после '\n'. */
static ssize_t read_line(char *buf, size_t cap)
{
    size_t n = 0;
    while (n + 1 < cap) {
        char ch;
        ssize_t r = read(0, &ch, 1);
        if (r < 0 && errno == EINTR) { if (g_stop) return -1; continue; }
        if (r <= 0) break;
        if (ch == '\n') { buf[n] = 0; return (ssize_t)n; }
        buf[n++] = ch;
    }
    buf[n] = 0;
    return (n > 0 && n + 1 < cap) ? (ssize_t)n : -1; /* EOF без '\n' — принимаем, переполнение — нет */
}

/* ------------------------------------------------------------------ входящий звук терминала */
static unsigned long long g_rx_dev, g_rx_cb;

static void on_voice(LONG handle, char *buf, DWORD size, BYTE flag, void *user)
{
    /* Поток SDK. Звук с терминала (byAudioFlag=1) только считаем: писать его в stdout нельзя —
       если читатель не заберёт, труба заполнится и заблокирует поток SDK. */
    (void)handle; (void)buf; (void)user;
    if (flag == 1) __atomic_add_fetch(&g_rx_dev, size, __ATOMIC_RELAXED);
    __atomic_add_fetch(&g_rx_cb, 1, __ATOMIC_RELAXED);
}

static void on_signal(int sig) { (void)sig; g_stop = 1; }

static void ts_add(struct timespec *t, long ns)
{
    t->tv_nsec += ns;
    while (t->tv_nsec >= 1000000000L) { t->tv_nsec -= 1000000000L; t->tv_sec++; }
}

static long ts_diff(const struct timespec *a, const struct timespec *b) /* a - b, нс */
{
    return (long)(a->tv_sec - b->tv_sec) * 1000000000L + (a->tv_nsec - b->tv_nsec);
}

/* ------------------------------------------------------------------ режимы */
static int run_selftest(const char *dir, const char *log_dir)
{
    static const char *const comps[] = {"libHCCoreDevCfg.so", "libHCVoiceTalk.so", "libAudioIntercom.so", NULL};
    char ver[32], esc[768], line[1024], path[PATH_MAX];

    if (sdk_load(dir)) { emit_error(-2, g_err, -1, -1); return 2; }
    if (sdk_init(dir, log_dir)) {
        DWORD e = sdk.GetLastError();
        emit_error(e ? (long)e : -2, "NET_DVR_Init не удался", -1, -1);
        return 2;
    }
    sdk_version(ver, sizeof ver);
    /* Голосовые компоненты SDK грузит лениво (уже после входа) — проверяем заранее, что все их
       зависимости (OpenAL и др.) находятся, чтобы «not found» не всплыл только в разговоре. */
    g_err[0] = 0;
    for (int i = 0; comps[i]; i++) {
        if (pjoin(path, sizeof path, dir, "/HCNetSDKCom/", comps[i]) || !dlopen(path, RTLD_NOW | RTLD_LOCAL)) {
            snprintf(g_err, sizeof g_err, "%s", dlerror());
            break;
        }
    }
    json_escape(esc, sizeof esc, g_err);
    snprintf(line, sizeof line, "{\"type\":\"selftest\",\"ok\":%s,\"sdk\":\"%s\",\"arch\":\"%s\"%s%s%s}\n",
             g_err[0] ? "false" : "true", ver, ARCH_NAME,
             g_err[0] ? ",\"error\":\"" : "", esc, g_err[0] ? "\"" : "");
    emit_line(line);
    say("selftest: HCNetSDK %s, %s", ver, g_err[0] ? g_err : "голосовые компоненты загружаются");
    sdk.Cleanup();
    return g_err[0] ? 1 : 0;
}

static int run_talk(const char *dir, const char *log_dir)
{
    char line[2048], msg[512];
    talk_request rq;
    NET_DVR_USER_LOGIN_INFO li;
    NET_DVR_DEVICEINFO_V40 dev;
    NET_DVR_COMPRESSION_AUDIO ac;
    LONG uid = -1, vh = -1;
    int rc = 0, alaw = 0, fails = 0;
    unsigned long long sent = 0;

    ssize_t n = read_line(line, sizeof line);
    const char *perr = n < 0 ? "нет первой строки с параметрами (JSON) или она длиннее 2 КБ"
                             : parse_request(line, (size_t)n, &rq);
    explicit_bzero(line, sizeof line); /* в строке был пароль */
    if (perr) { explicit_bzero(&rq, sizeof rq); emit_error(-1, perr, -1, -1); return 2; }

    if (sdk_load(dir)) { explicit_bzero(&rq, sizeof rq); emit_error(-2, g_err, -1, -1); return 2; }
    if (sdk_init(dir, log_dir)) {
        DWORD e = sdk.GetLastError();
        explicit_bzero(&rq, sizeof rq);
        snprintf(msg, sizeof msg, "NET_DVR_Init не удался: %s", hik_error_text(e));
        emit_error(e ? (long)e : -2, msg, -1, -1);
        return 2;
    }

    /* ---- вход */
    memset(&li, 0, sizeof li);
    memset(&dev, 0, sizeof dev);
    memcpy(li.sDeviceAddress, rq.host, strlen(rq.host));
    li.wPort = (WORD)rq.port;
    memcpy(li.sUserName, rq.user, strlen(rq.user));
    memcpy(li.sPassword, rq.password, strlen(rq.password));
    explicit_bzero(rq.password, sizeof rq.password);
    li.bUseAsynLogin = 0;
    li.byLoginMode = 0;
    say("вход на %s:%d как %s, канал %d", rq.host, rq.port, rq.user, rq.channel);
    uid = sdk.Login_V40(&li, &dev);
    explicit_bzero(li.sPassword, sizeof li.sPassword);
    if (uid < 0) {
        DWORD e = sdk.GetLastError();
        int attempts = -1;
        long lock_s = -1;
        snprintf(msg, sizeof msg, "%s", hik_error_text(e));
        if (dev.bySupportLock && (e == 1 || e == 153)) {
            attempts = dev.byRetryLoginTime;
            snprintf(msg + strlen(msg), sizeof msg - strlen(msg), "; осталось попыток входа: %d", attempts);
            if (e == 153) {
                lock_s = (long)dev.dwSurplusLockTime;
                snprintf(msg + strlen(msg), sizeof msg - strlen(msg), "; до разблокировки: %ld с", lock_s);
            }
        }
        emit_error((long)e, msg, attempts, lock_s);
        sdk.Cleanup();
        return 3;
    }
    say("вход выполнен");
    if (g_stop) { emit_error(-3, "остановлено до начала разговора", -1, -1); rc = 0; goto out; }

    /* ---- кодек переговоров: терминал ждёт звук именно в нём */
    memset(&ac, 0, sizeof ac);
    int enc = 1;
    if (sdk.GetCurrentAudioCompress(uid, &ac)) enc = ac.byAudioEncType;
    else say("кодек не получен (код %u) — считаю G.711 µ-law, как у DS-K1T341AM", sdk.GetLastError());
    alaw = (enc == 2);
    if (enc != 1 && enc != 2) say("кодек терминала %s — мост умеет только G.711, шлю µ-law как есть", hik_codec_name(enc));

    /* ---- голосовая сессия в режиме пересылки (MR): звук даём сами через VoiceComSendData */
    vh = sdk.StartVoiceCom_MR_V30(uid, (DWORD)rq.channel, on_voice, NULL);
    if (vh < 0) {
        DWORD e = sdk.GetLastError();
        emit_error((long)e, hik_error_text(e), -1, -1);
        rc = 3;
        goto out;
    }
    if (g_stop) { emit_error(-3, "остановлено до начала разговора", -1, -1); rc = 0; goto out; }
    snprintf(msg, sizeof msg, "{\"type\":\"started\",\"codec\":\"%s\"}\n", hik_codec_name(enc));
    emit_line(msg);
    say("разговор начат, кодек %s", hik_codec_name(enc));

    if (alaw)
        for (int i = 0; i < 256; i++) g_u2a[i] = linear_to_alaw(ulaw_to_linear((unsigned char)i));

    pthread_t rt;
    if (pthread_create(&rt, NULL, reader_main, NULL)) { say("не создан поток чтения stdin"); rc = 2; goto out; }
    pthread_detach(rt); /* при выходе он может висеть в read() — ждать его незачем */

    /* ---- главный цикл: кадр каждые 20 мс по абсолютному времени (без накопления дрейфа) */
    struct timespec next, now;
    clock_gettime(CLOCK_MONOTONIC, &next);
    while (!g_stop) {
        unsigned char frame[FRAME_BYTES];
        int eof;
        ts_add(&next, FRAME_NS);
        clock_gettime(CLOCK_MONOTONIC, &now);
        if (ts_diff(&now, &next) > RESYNC_NS) next = now;
        while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &next, NULL) == EINTR && !g_stop) {}
        if (g_stop) break;
        if (!ring_pop_frame(frame, &eof)) {
            if (eof) break; /* stdin закрыт и буфер досыпан */
            continue;       /* тишина от клиента — ничего не шлём */
        }
        if (alaw)
            for (int i = 0; i < FRAME_BYTES; i++) frame[i] = g_u2a[frame[i]];
        if (sdk.VoiceComSendData(vh, (char *)frame, FRAME_BYTES)) { sent++; fails = 0; continue; }
        if (++fails == 1) say("VoiceComSendData: код %u", sdk.GetLastError());
        if (fails >= MAX_SEND_FAILS) { say("связь с терминалом потеряна"); rc = 4; break; }
    }

out:
    if (vh >= 0) sdk.StopVoiceCom(vh);
    if (uid >= 0) sdk.Logout(uid);
    sdk.Cleanup();
    say("итог: кадров отправлено=%llu, байт принято=%llu, колбэков=%llu, выброшено байт=%llu",
        sent, __atomic_load_n(&g_rx_dev, __ATOMIC_RELAXED), __atomic_load_n(&g_rx_cb, __ATOMIC_RELAXED), g_dropped);
    explicit_bzero(&rq, sizeof rq);
    return rc;
}

int main(int argc, char **argv)
{
    int selftest = 0;
    const char *dir_opt = NULL, *log_dir = NULL;
    char dir[PATH_MAX];

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--selftest")) selftest = 1;
        else if (!strcmp(argv[i], "--sdk-dir") && i + 1 < argc) dir_opt = argv[++i];
        else if (!strcmp(argv[i], "--sdk-log") && i + 1 < argc) log_dir = argv[++i];
        else {
            fprintf(stderr, "использование: bms_talk [--selftest] [--sdk-dir DIR] [--sdk-log DIR]\n");
            return 2;
        }
    }

    /* Настоящий stdout бережём для одной JSON-строки; всё, что вздумают печатать SDK или OpenAL,
       уходит в stderr и не ломает протокол. */
    g_out = dup(1);
    if (g_out < 0 || dup2(2, 1) < 0) { perror("bms_talk: dup"); return 2; }

    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_signal; /* без SA_RESTART: сон и read прерываются сразу */
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGHUP, &sa, NULL);
    signal(SIGPIPE, SIG_IGN); /* читатель ушёл — пусть write вернёт ошибку, а не убьёт процесс */

    /* Каталог SDK = каталог программы. Через загрузчик argv[0] — это путь к bms_talk, а
       /proc/self/exe — к ld-linux (лежит в том же каталоге), поэтому годится любой. */
    char tmp[PATH_MAX];
    const char *src = dir_opt ? dir_opt : argv[0];
    if (!realpath(src, tmp)) {
        ssize_t l = readlink("/proc/self/exe", tmp, sizeof tmp - 1);
        if (l <= 0) { fprintf(stderr, "bms_talk: не найден свой каталог\n"); return 2; }
        tmp[l] = 0;
    }
    memcpy(dir, tmp, sizeof dir);
    if (!dir_opt) { char *s = strrchr(dir, '/'); if (s) *s = 0; }
    /* SDK хранит путь к себе в поле из 256 байт — длиннее не влезет, а обрезанный путь хуже ошибки. */
    if (strlen(dir) > 200) {
        emit_error(-2, "путь к каталогу помощника длиннее 200 символов", -1, -1);
        return 2;
    }

    return selftest ? run_selftest(dir, log_dir) : run_talk(dir, log_dir);
}
