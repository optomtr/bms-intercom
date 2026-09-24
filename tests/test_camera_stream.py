"""Поток кадров с панели, у которой нет снимка.

Жалоба владельца 24.09.2026: «при звонке домофона видео не видно». Замер на
живом объекте: `/api/camera_proxy_stream/camera.domofon_video` отвечает
HTTP 200 с правильным `multipart/x-mixed-replace` и присылает **ноль байт** за
25 секунд, тогда как одиночный снимок `/api/camera_proxy/...` отдаёт
77-килобайтный JPEG за полсекунды.

Причина в самом Home Assistant: `handle_async_still_stream` зовёт
`async_camera_image` НАПРЯМУЮ, минуя `use_stream_for_stills`. У DS-K1T341AM
ISAPI-снимка нет (404 на все picture-ручки), поэтому `async_camera_image`
возвращает None, а `async_get_still_stream` на первом же пустом кадре делает
`break`.

Здесь проверяется источник кадров, которым мы этот путь заменили.

Run: python -m unittest discover -s tests
"""
from __future__ import annotations

import asyncio
import unittest

from _loader import load

frames = load("stream_frames")


class StreamFrameSourceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.slept: list[float] = []

    async def _sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    def test_первый_кадр_ждут_а_не_обрывают_поток(self) -> None:
        # Панель отвечает не сразу: ffmpeg поднимается и ждёт опорный кадр.
        answers = [None, None, b"\xff\xd8first"]

        async def get_image() -> bytes | None:
            return answers.pop(0)

        source = frames.make_stream_frame_source(get_image, sleep=self._sleep)
        self.assertEqual(b"\xff\xd8first", asyncio.run(source()))
        # Ждали, а не крутились вхолостую.
        self.assertEqual([frames._FIRST_FRAME_WAIT] * 2, self.slept)

    def test_одиночный_пропуск_держит_прошлый_кадр(self) -> None:
        answers = [b"one", None, b"two"]

        async def get_image() -> bytes | None:
            return answers.pop(0)

        source = frames.make_stream_frame_source(get_image, sleep=self._sleep)
        self.assertEqual(b"one", asyncio.run(source()))
        # Пусто — но поток НЕ рвём: отдаём прошлый кадр. Одинаковые байты
        # async_get_still_stream просто не пошлёт, соединение живо.
        self.assertEqual(b"one", asyncio.run(source()))
        self.assertEqual(b"two", asyncio.run(source()))
        # Второй кадр ждать не надо — первый уже был.
        self.assertEqual([], self.slept)

    def test_панель_пропала_насовсем_поток_закрывается(self) -> None:
        async def get_image() -> bytes | None:
            return get_image.answer  # type: ignore[attr-defined]

        get_image.answer = b"one"  # type: ignore[attr-defined]
        source = frames.make_stream_frame_source(get_image, sleep=self._sleep)
        self.assertEqual(b"one", asyncio.run(source()))
        get_image.answer = None  # type: ignore[attr-defined]
        for _ in range(frames._MISS_LIMIT):
            self.assertEqual(b"one", asyncio.run(source()))
        self.assertIsNone(asyncio.run(source()))

    def test_панели_не_было_вовсе_поток_закрывается_а_не_висит(self) -> None:
        async def get_image() -> bytes | None:
            return None

        source = frames.make_stream_frame_source(get_image, sleep=self._sleep)
        self.assertIsNone(asyncio.run(source()))
        # Ровно столько попыток, сколько обещано: не бесконечный цикл.
        self.assertEqual([frames._FIRST_FRAME_WAIT] * frames._FIRST_FRAME_TRIES, self.slept)


if __name__ == "__main__":
    unittest.main()
