/*
 * Разбор ОДНОЙ строки JSON с параметрами разговора и экранирование строк для ответа.
 *
 * Почему свой крошечный разбор, а не библиотека: помощник везётся готовым бинарником внутри
 * интеграции, лишняя .so — лишний риск «not found» под Alpine. Формат строки фиксирован и плоский:
 *   {"host":"192.168.70.121","port":8000,"user":"admin","password":"…","channel":1}
 * Строки поддерживают все экраны JSON, включая \uXXXX и суррогатные пары (Python json.dumps
 * по умолчанию пишет не-ASCII пароль именно так). Вложенные объекты/массивы — ошибка.
 */
#ifndef BMS_JSONLINE_H
#define BMS_JSONLINE_H

#include <stdio.h>
#include <string.h>

typedef struct {
    char host[129];    /* как sDeviceAddress: до 128 байт */
    int port;
    char user[64];     /* как sUserName: до 63 байт */
    char password[64]; /* как sPassword: до 63 байт; обнуляется сразу после входа */
    int channel;
} talk_request;

typedef struct { const char *p, *e; } jcur;

static void j_ws(jcur *c)
{
    while (c->p < c->e && (*c->p == ' ' || *c->p == '\t' || *c->p == '\r' || *c->p == '\n'))
        c->p++;
}

static int j_hex4(jcur *c, unsigned *out)
{
    unsigned v = 0;
    if (c->e - c->p < 4)
        return -1;
    for (int i = 0; i < 4; i++) {
        char ch = *c->p++;
        v <<= 4;
        if (ch >= '0' && ch <= '9') v |= (unsigned)(ch - '0');
        else if (ch >= 'a' && ch <= 'f') v |= (unsigned)(ch - 'a' + 10);
        else if (ch >= 'A' && ch <= 'F') v |= (unsigned)(ch - 'A' + 10);
        else return -1;
    }
    *out = v;
    return 0;
}

/* Дописать кодовую точку в UTF-8, оставив место под завершающий \0. */
static int j_put_utf8(char *out, size_t cap, size_t *n, unsigned cp)
{
    unsigned char b[4];
    size_t k;
    if (cp < 0x80) { b[0] = (unsigned char)cp; k = 1; }
    else if (cp < 0x800) { b[0] = 0xC0 | (cp >> 6); b[1] = 0x80 | (cp & 0x3F); k = 2; }
    else if (cp < 0x10000) { b[0] = 0xE0 | (cp >> 12); b[1] = 0x80 | ((cp >> 6) & 0x3F); b[2] = 0x80 | (cp & 0x3F); k = 3; }
    else { b[0] = 0xF0 | (cp >> 18); b[1] = 0x80 | ((cp >> 12) & 0x3F); b[2] = 0x80 | ((cp >> 6) & 0x3F); b[3] = 0x80 | (cp & 0x3F); k = 4; }
    if (*n + k >= cap)
        return -2; /* не влезает */
    memcpy(out + *n, b, k);
    *n += k;
    return 0;
}

/* Строка JSON → UTF-8 с \0. 0 — ок, -1 — ошибка формата, -2 — длиннее cap-1 байт. */
static int j_str(jcur *c, char *out, size_t cap)
{
    size_t n = 0;
    if (c->p >= c->e || *c->p != '"')
        return -1;
    c->p++;
    while (c->p < c->e) {
        unsigned char ch = (unsigned char)*c->p++;
        unsigned cp;
        if (ch == '"') { out[n] = 0; return 0; }
        if (ch < 0x20) return -1;
        if (ch != '\\') {
            if (n + 1 >= cap) return -2;
            out[n++] = (char)ch;
            continue;
        }
        if (c->p >= c->e) return -1;
        switch (*c->p++) {
        case '"': cp = '"'; break;
        case '\\': cp = '\\'; break;
        case '/': cp = '/'; break;
        case 'b': cp = '\b'; break;
        case 'f': cp = '\f'; break;
        case 'n': cp = '\n'; break;
        case 'r': cp = '\r'; break;
        case 't': cp = '\t'; break;
        case 'u':
            if (j_hex4(c, &cp)) return -1;
            if (cp >= 0xD800 && cp <= 0xDBFF) { /* суррогатная пара */
                unsigned lo;
                if (c->e - c->p < 6 || c->p[0] != '\\' || c->p[1] != 'u') return -1;
                c->p += 2;
                if (j_hex4(c, &lo) || lo < 0xDC00 || lo > 0xDFFF) return -1;
                cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
            } else if (cp >= 0xDC00 && cp <= 0xDFFF) {
                return -1;
            }
            break;
        default:
            return -1;
        }
        if (cp == 0) return -1; /* \u0000 в C-строку не положить — отвергаем */
        int r = j_put_utf8(out, cap, &n, cp);
        if (r) return r;
    }
    return -1;
}

/* Целое без дробей и экспоненты. */
static int j_int(jcur *c, long *out)
{
    int neg = 0, digits = 0;
    long v = 0;
    if (c->p < c->e && *c->p == '-') { neg = 1; c->p++; }
    while (c->p < c->e && *c->p >= '0' && *c->p <= '9') {
        if (++digits > 9) return -1;
        v = v * 10 + (*c->p++ - '0');
    }
    if (!digits || (c->p < c->e && (*c->p == '.' || *c->p == 'e' || *c->p == 'E')))
        return -1;
    *out = neg ? -v : v;
    return 0;
}

/* Пропустить значение неизвестного ключа (только простые значения). */
static int j_skip(jcur *c)
{
    char scratch[2048];
    long dummy;
    if (c->p >= c->e) return -1;
    if (*c->p == '"') return j_str(c, scratch, sizeof scratch) ? -1 : 0;
    if (*c->p == '-' || (*c->p >= '0' && *c->p <= '9')) {
        if (j_int(c, &dummy) == 0) return 0;
        while (c->p < c->e && strchr("0123456789+-.eE", *c->p)) c->p++; /* дробное — просто пропускаем */
        return 0;
    }
    static const char *lits[] = {"true", "false", "null"};
    for (int i = 0; i < 3; i++) {
        size_t l = strlen(lits[i]);
        if ((size_t)(c->e - c->p) >= l && !memcmp(c->p, lits[i], l)) { c->p += l; return 0; }
    }
    return -1;
}

static int host_ok(const char *h)
{
    if (!*h) return 0;
    for (; *h; h++)
        if (!((*h >= 'a' && *h <= 'z') || (*h >= 'A' && *h <= 'Z') || (*h >= '0' && *h <= '9') ||
              *h == '.' || *h == '-' || *h == ':' || *h == '_'))
            return 0;
    return 1;
}

/* NULL — ок; иначе текст ошибки по-русски (без значений полей: там может быть пароль). */
static const char *parse_request(const char *line, size_t len, talk_request *rq)
{
    jcur c = {line, line + len};
    char key[32];
    long v;
    int have_host = 0, have_pw = 0;

    memset(rq, 0, sizeof *rq);
    rq->port = 8000;
    rq->channel = 1;
    snprintf(rq->user, sizeof rq->user, "admin");

    j_ws(&c);
    if (c.p >= c.e || *c.p++ != '{') return "первая строка должна быть JSON-объектом";
    j_ws(&c);
    if (c.p < c.e && *c.p == '}') { c.p++; goto done; }
    for (;;) {
        int r;
        j_ws(&c);
        if (j_str(&c, key, sizeof key)) return "неверный JSON (ключ)";
        j_ws(&c);
        if (c.p >= c.e || *c.p++ != ':') return "неверный JSON (нет ':')";
        j_ws(&c);
        if (!strcmp(key, "host")) {
            r = j_str(&c, rq->host, sizeof rq->host);
            if (r == -2) return "host длиннее 128 байт";
            if (r || !host_ok(rq->host)) return "неверный host";
            have_host = 1;
        } else if (!strcmp(key, "user")) {
            r = j_str(&c, rq->user, sizeof rq->user);
            if (r == -2) return "user длиннее 63 байт";
            if (r || !rq->user[0]) return "неверный user";
        } else if (!strcmp(key, "password")) {
            r = j_str(&c, rq->password, sizeof rq->password);
            if (r == -2) return "пароль длиннее 63 байт";
            if (r || !rq->password[0]) return "неверный или пустой пароль";
            have_pw = 1;
        } else if (!strcmp(key, "port")) {
            if (j_int(&c, &v) || v < 1 || v > 65535) return "port должен быть целым 1..65535";
            rq->port = (int)v;
        } else if (!strcmp(key, "channel")) {
            if (j_int(&c, &v) || v < 1 || v > 255) return "channel должен быть целым 1..255";
            rq->channel = (int)v;
        } else if (j_skip(&c)) {
            return "неверный JSON (значение)";
        }
        j_ws(&c);
        if (c.p < c.e && *c.p == ',') { c.p++; continue; }
        if (c.p < c.e && *c.p == '}') { c.p++; break; }
        return "неверный JSON (нет ',' или '}')";
    }
done:
    j_ws(&c);
    if (c.p != c.e) return "лишние символы после JSON";
    if (!have_host) return "нет поля host";
    if (!have_pw) return "нет поля password";
    return NULL;
}

/* Кадр S постоянного режима: {"channel":N}; пустой payload или {} — канал 1.
   NULL — ок; иначе текст ошибки (помощник не выходит — ошибка только этого кадра). */
static const char *parse_voice(const char *s, size_t len, int *channel)
{
    jcur c = {s, s + len};
    char key[32];
    long v;

    *channel = 1;
    j_ws(&c);
    if (c.p == c.e) return NULL;
    if (*c.p++ != '{') return "S: ожидался JSON-объект";
    j_ws(&c);
    if (c.p < c.e && *c.p == '}') { c.p++; goto done; }
    for (;;) {
        j_ws(&c);
        if (j_str(&c, key, sizeof key)) return "S: неверный JSON (ключ)";
        j_ws(&c);
        if (c.p >= c.e || *c.p++ != ':') return "S: неверный JSON (нет ':')";
        j_ws(&c);
        if (!strcmp(key, "channel")) {
            if (j_int(&c, &v) || v < 1 || v > 255) return "S: channel должен быть целым 1..255";
            *channel = (int)v;
        } else if (j_skip(&c)) {
            return "S: неверный JSON (значение)";
        }
        j_ws(&c);
        if (c.p < c.e && *c.p == ',') { c.p++; continue; }
        if (c.p < c.e && *c.p == '}') { c.p++; break; }
        return "S: неверный JSON (нет ',' или '}')";
    }
done:
    j_ws(&c);
    return c.p == c.e ? NULL : "S: лишние символы после JSON";
}

/* Экранировать строку для JSON-ответа (кавычки, обратная косая, управляющие символы). */
static void json_escape(char *dst, size_t cap, const char *s)
{
    size_t n = 0;
    for (; *s && n + 7 < cap; s++) {
        unsigned char ch = (unsigned char)*s;
        if (ch == '"' || ch == '\\') { dst[n++] = '\\'; dst[n++] = (char)ch; }
        else if (ch < 0x20) n += (size_t)snprintf(dst + n, cap - n, "\\u%04x", ch);
        else dst[n++] = (char)ch;
    }
    dst[n] = 0;
}

#endif
