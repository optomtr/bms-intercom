/*
 * G.711: µ-law → линейный PCM → A-law. Нужно, если терминал говорит в A-law, а оператор (клиент)
 * всегда шлёт µ-law: перекодируем таблицей из 256 байт, построенной этими функциями.
 * Алгоритм — классический g711.c (Sun), тот же, что lin2alaw в проверенном probe.py.
 */
#ifndef BMS_G711_H
#define BMS_G711_H

static int ulaw_to_linear(unsigned char u)
{
    u = (unsigned char)~u;
    int t = (((u & 0x0F) << 3) + 0x84) << ((u & 0x70) >> 4);
    return (u & 0x80) ? (0x84 - t) : (t - 0x84);
}

static unsigned char linear_to_alaw(int pcm) /* классический g711.c (Sun), как lin2alaw в probe.py */
{
    static const int seg_end[8] = {0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF};
    int mask, seg;
    pcm >>= 3;
    if (pcm >= 0) mask = 0xD5;
    else { mask = 0x55; pcm = -pcm - 1; }
    for (seg = 0; seg < 8 && pcm > seg_end[seg]; seg++) {}
    if (seg >= 8) return (unsigned char)(0x7F ^ mask);
    unsigned char a = (unsigned char)(seg << 4);
    a |= (unsigned char)(((seg < 2) ? (pcm >> 1) : (pcm >> seg)) & 0x0F);
    return (unsigned char)(a ^ mask);
}

#endif
