/*
 * Типы и структуры HCNetSDK (Linux, LP64) — ровно те, что проверены на живом терминале
 * DS-K1T341AM в bms-intercom-sdk-probe/probe.py (ctypes). Здесь они повторены на C.
 *
 * Почему свой заголовок, а не HCNetSDK.h: помощник не линкуется с SDK (грузит его через dlopen),
 * а весь HCNetSDK.h — десятки тысяч строк; нам нужны пять структур и дюжина функций.
 * Размеры сверены с ctypes (sizeof/offset) и зафиксированы _Static_assert ниже: если структура
 * «поплывёт», SDK начнёт писать за границы буфера — лучше не собраться, чем упасть у клиента.
 */
#ifndef BMS_HIK_SDK_H
#define BMS_HIK_SDK_H

#include <stddef.h>

/* В HCNetSDK.h для Linux: BOOL=int, LONG=int (не long!), DWORD=unsigned int. */
typedef int BOOL;
typedef int LONG;
typedef unsigned int DWORD;
typedef unsigned short WORD;
typedef unsigned char BYTE;

typedef struct {
    BYTE sSerialNumber[48];
    BYTE byAlarmInPortNum, byAlarmOutPortNum, byDiskNum, byDVRType, byChanNum, byStartChan;
    BYTE byAudioChanNum, byIPChanNum, byZeroChanNum, byMainProto, bySubProto;
    BYTE bySupport, bySupport1, bySupport2;
    WORD wDevType;
    BYTE bySupport3, byMultiStreamProto, byStartDChan, byStartDTalkChan, byHighDChanNum;
    BYTE bySupport4, byLanguageType, byVoiceInChanNum, byStartVoiceInChanNo;
    BYTE bySupport5, bySupport6, byMirrorChanNum;
    WORD wStartMirrorChanNo;
    BYTE bySupport7, byRes2;
} NET_DVR_DEVICEINFO_V30;

typedef struct {
    NET_DVR_DEVICEINFO_V30 struDeviceV30;
    BYTE bySupportLock;      /* 1 — терминал умеет блокировать вход после неудачных попыток */
    BYTE byRetryLoginTime;   /* сколько попыток осталось (при ошибке пароля) */
    BYTE byPasswordLevel;
    BYTE byProxyType;
    DWORD dwSurplusLockTime; /* сколько секунд ещё заблокирован (при ошибке 153) */
    BYTE byCharEncodeType, bySupportDev5, bySupport, byLoginMode;
    DWORD dwOEMCode;
    int iResidualValidity;
    BYTE byResidualValidity, bySingleStartDTalkChan, bySingleDTalkChanNums, byPassWordResetLevel;
    BYTE bySupportStreamEncrypt, byMarketType, byTLSCap;
    BYTE byRes2[237];
} NET_DVR_DEVICEINFO_V40;

typedef void (*fLoginResultCallBack)(LONG lUserID, DWORD dwResult, NET_DVR_DEVICEINFO_V30 *dev, void *pUser);

typedef struct {
    char sDeviceAddress[129];
    BYTE byUseTransport;
    WORD wPort;
    char sUserName[64];
    char sPassword[64];
    fLoginResultCallBack cbLoginResult;
    void *pUser;
    BOOL bUseAsynLogin;
    BYTE byProxyType, byUseUTCTime;
    BYTE byLoginMode; /* 0 — приватный протокол (порт 8000) */
    BYTE byHttps;
    LONG iProxyID;
    BYTE byVerifyMode;
    BYTE byRes3[119];
} NET_DVR_USER_LOGIN_INFO;

typedef struct {
    BYTE byAudioEncType; /* 1 — G.711 µ-law, 2 — G.711 A-law, ... */
    BYTE byAudioSamplingRate;
    BYTE byAudioBitRate;
    BYTE byres[4];
    BYTE bySupport;
} NET_DVR_COMPRESSION_AUDIO;

typedef struct {
    char sPath[256];
    BYTE byRes[128];
} NET_DVR_LOCAL_SDK_PATH;

/* void (*fVoiceDataCallBack)(LONG handle, char *buf, DWORD size, BYTE byAudioFlag, void *pUser) */
typedef void (*fVoiceDataCallBack)(LONG lVoiceComHandle, char *pRecvDataBuffer, DWORD dwBufSize,
                                   BYTE byAudioFlag, void *pUser);

/* NET_DVR_SetSDKInitCfg: где лежат компоненты HCNetSDKCom/ и OpenSSL — до NET_DVR_Init. */
enum { NET_SDK_INIT_CFG_SDK_PATH = 2, NET_SDK_INIT_CFG_LIBEAY_PATH = 3, NET_SDK_INIT_CFG_SSLEAY_PATH = 4 };

/* Сверено с ctypes в probe.py (одинаково на aarch64 и x86-64: обе LP64). */
_Static_assert(sizeof(NET_DVR_DEVICEINFO_V30) == 80, "DEVICEINFO_V30");
_Static_assert(sizeof(NET_DVR_DEVICEINFO_V40) == 344, "DEVICEINFO_V40");
_Static_assert(offsetof(NET_DVR_DEVICEINFO_V40, dwSurplusLockTime) == 84, "V40.dwSurplusLockTime");
_Static_assert(offsetof(NET_DVR_DEVICEINFO_V40, byRes2) == 107, "V40.byRes2");
_Static_assert(sizeof(NET_DVR_USER_LOGIN_INFO) == 416, "USER_LOGIN_INFO");
_Static_assert(offsetof(NET_DVR_USER_LOGIN_INFO, wPort) == 130, "LOGIN.wPort");
_Static_assert(offsetof(NET_DVR_USER_LOGIN_INFO, sPassword) == 196, "LOGIN.sPassword");
_Static_assert(offsetof(NET_DVR_USER_LOGIN_INFO, cbLoginResult) == 264, "LOGIN.cbLoginResult");
_Static_assert(offsetof(NET_DVR_USER_LOGIN_INFO, bUseAsynLogin) == 280, "LOGIN.bUseAsynLogin");
_Static_assert(offsetof(NET_DVR_USER_LOGIN_INFO, byLoginMode) == 286, "LOGIN.byLoginMode");
_Static_assert(offsetof(NET_DVR_USER_LOGIN_INFO, iProxyID) == 288, "LOGIN.iProxyID");
_Static_assert(offsetof(NET_DVR_USER_LOGIN_INFO, byRes3) == 293, "LOGIN.byRes3");
_Static_assert(sizeof(NET_DVR_COMPRESSION_AUDIO) == 8, "COMPRESSION_AUDIO");
_Static_assert(sizeof(NET_DVR_LOCAL_SDK_PATH) == 384, "LOCAL_SDK_PATH");

/* Коды ошибок NET_DVR_GetLastError, которые реально встречаются при входе и разговоре. */
static const struct { DWORD code; const char *text; } HIK_ERRORS[] = {
    {1, "Неверный логин или пароль"},
    {2, "Недостаточно прав у пользователя"},
    {3, "SDK не инициализирован"},
    {4, "Неверный номер голосового канала"},
    {5, "Превышено число подключённых клиентов"},
    {6, "Несовпадение версии протокола"},
    {7, "Не удалось подключиться к терминалу (нет сети, неверный IP или порт, порт закрыт)"},
    {8, "Ошибка отправки на терминал"},
    {9, "Ошибка приёма от терминала"},
    {10, "Терминал не ответил вовремя"},
    {11, "Неверные данные"},
    {12, "Неверный порядок вызовов SDK"},
    {13, "Нет разрешения на операцию"},
    {14, "Терминал не выполнил команду вовремя"},
    {17, "Неверный параметр"},
    {23, "Терминал не поддерживает голосовую связь через SDK"},
    {24, "Терминал занят"},
    {29, "Операция на терминале не удалась"},
    {31, "Голосовой канал терминала занят (идёт другой разговор)"},
    {41, "Ошибка выделения ресурсов"},
    {44, "Не удалось создать сокет"},
    {47, "Пользователь не существует"},
    {52, "На терминале достигнуто максимальное число пользователей"},
    {73, "Соединение с терминалом разорвано"},
    {100, "Не загрузилась библиотека голосовой связи (HCNetSDKCom/libAudioIntercom.so)"},
    {102, "Пользователь ещё не вошёл"},
    {108, "Не загрузился голосовой компонент libHCVoiceTalk.so"},
    {123, "Голосовой компонент не совпадает по версии с ядром SDK"},
    {137, "Голосовой компонент не совпадает по версии с HCNetSDK"},
    {148, "Не загрузилась библиотека SSL (libssl/libcrypto)"},
    {152, "Пользователь не существует"},
    {153, "Пользователь заблокирован после неудачных попыток входа"},
    {154, "Сессия SDK недействительна"},
    {155, "Слишком старая версия протокола входа"},
    {156, "Не загрузилась libcrypto"},
    {157, "Не загрузилась libssl"},
    {158, "Не загрузилась libiconv"},
};

static const char *hik_error_text(DWORD code)
{
    for (size_t i = 0; i < sizeof HIK_ERRORS / sizeof HIK_ERRORS[0]; i++)
        if (HIK_ERRORS[i].code == code)
            return HIK_ERRORS[i].text;
    return "Ошибка HCNetSDK (подробности — в журнале SDK)";
}

/* byAudioEncType → имя кодека в протоколе помощника. */
static const char *hik_codec_name(int enc)
{
    switch (enc) {
    case 0: return "G.722";
    case 1: return "G.711ulaw";
    case 2: return "G.711alaw";
    case 5: return "MP2L2";
    case 6: return "G.726";
    case 7: return "AAC";
    case 8: return "PCM";
    case 9: return "G.722.1C";
    case 12: return "AAC-LC";
    case 13: return "AAC-LD";
    case 14: return "Opus";
    case 15: return "MP3";
    case 16: return "ADPCM";
    default: return "unknown";
    }
}

#endif
