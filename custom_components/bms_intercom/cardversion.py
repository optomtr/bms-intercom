"""Метка версии поп-апа: md5 файла карточки, одна на весь запуск HA.

Та же метка идёт в ?v= URL карточки (__init__) и в атрибут
intercom_card_version (entity): открытая вкладка сравнивает их и со старым JS
перезагружается сама. Считается ОДИН раз — при регистрации фронтенда:
- атрибут пишется на каждый state, читать файл каждый раз — блокирующий
  ввод-вывод в цикле событий;
- HACS меняет файл без перезапуска, а URL зарегистрирован со старым хэшем до
  перезапуска HA; свежий хэш с диска разошёлся бы с URL, и вкладки
  перезагружались бы в ту же старую карточку.
"""
from __future__ import annotations

import hashlib
import os

CARD_FILE = "bms_intercom_card.js"

_version: str | None = None


def load_card_version() -> str:
    """Посчитать метку (читает файл — звать в executor). Повторно не читает.

    Ручной номер версии забывали поднимать — браузеры держали старый поп-ап.
    Хэш содержимого меняется с каждой правкой сам; хватает перезапуска HA.
    """
    global _version
    if _version is None:
        path = os.path.join(os.path.dirname(__file__), "frontend", CARD_FILE)
        try:
            with open(path, "rb") as fh:
                _version = hashlib.md5(fh.read()).hexdigest()[:10]
        except OSError:
            _version = "dev"
    return _version


def card_version() -> str | None:
    """Уже посчитанная метка или None — без ввода-вывода."""
    return _version
