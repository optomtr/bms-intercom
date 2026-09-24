"""Кадры для <img>-потока панели, у которой нет снимка.

Отдельный модуль без единого импорта Home Assistant: то, что здесь решается,
проверяется на голом Python, а `camera.py` тянет за собой половину ядра HA.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

# Первый кадр из RTSP приходит не мгновенно: ffmpeg надо подняться и дождаться
# опорного кадра. Ждём до трёх секунд, иначе поток оборвался бы, не показав
# ничего, — ровно то, на что жаловался владелец.
_FIRST_FRAME_TRIES = 6
_FIRST_FRAME_WAIT = 0.5
# Одиночный пропуск кадра — не повод рвать видео: держим прошлую картинку.
# Столько пропусков подряд — панель действительно пропала.
_MISS_LIMIT = 8


def make_stream_frame_source(
    get_image: Callable[[], Awaitable[bytes | None]],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Callable[[], Awaitable[bytes | None]]:
    """Источник кадров для потока панели, у которой нет снимка.

    ``async_get_still_stream`` обрывает поток на ПЕРВОМ же пустом кадре
    (``if not img_bytes: break``). У DS-K1T341AM снимка нет вовсе, кадры
    добываются из RTSP, и любая заминка означала бы чёрный экран вместо
    гостя. Поэтому первый кадр ждём, а одиночные пропуски прикрываем прошлым
    кадром: повтор тех же байт ``async_get_still_stream`` просто не пошлёт,
    зато соединение останется живым.
    """
    last: bytes | None = None
    misses = 0

    async def frame() -> bytes | None:
        nonlocal last, misses
        image = await get_image()
        if image is None and last is None:
            for _ in range(_FIRST_FRAME_TRIES):
                await sleep(_FIRST_FRAME_WAIT)
                image = await get_image()
                if image is not None:
                    break
        if image is not None:
            last, misses = image, 0
            return image
        misses += 1
        if misses > _MISS_LIMIT:
            return None
        return last

    return frame
