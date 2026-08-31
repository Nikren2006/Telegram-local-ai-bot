# -*- coding: utf-8 -*-
"""Пример внешнего скила для бота.

Положите любой .py файл (или папку-репозиторий с __init__.py) в каталог
C:\\ai\\skills — бот найдёт и загрузит его автоматически (или по команде /reload).

Соглашение: функция-скил декорируется @skill("имя", "описание", schema),
принимает dict аргументов от модели и возвращает str.
"""
import json
import urllib.request

from tgbot import skill, confine, safe_read_file  # мини-API бота


@skill(
    "read_local_file",
    "Читает текстовый файл из каталога skills (и только из него). "
    "Аргумент path — относительный путь внутри skills.",
    {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Относительный путь внутри каталога skills"}
        },
        "required": ["path"],
        "additionalProperties": False,
    },
)
def _skill_read_local_file(args: dict) -> str:
    result = safe_read_file(args.get("path", ""), max_bytes=64_000)
    if result is None:
        return "Ошибка: путь вне каталога skills или файл недоступен."
    return result
