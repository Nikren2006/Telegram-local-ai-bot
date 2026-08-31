#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tgbot.py — полноценный Telegram чат-агент на python-telegram-bot + OpenAI-совместимый шлюз (OmniRoute).
Адаптирован для запуска в Termux (Android).

Возможности:
  • диалоговая память — бот помнит контекст беседы в каждом чате, история сохраняется
    между перезапусками (PicklePersistence -> bot_data.pkl);
  • понимание изображений (vision) — фото и картинки-документы (можно с подписью);
  • работа в группах — отвечает на reply к боту и на упоминания @username;
  • команды: /start /help /reset /model /prompt /stats;
  • индикатор «печатает…», авто-разбивка длинных ответов (лимит Telegram 4096),
    Markdown с безопасным фолбэком на обычный текст;
  • блокирующие вызовы ИИ вынесены из event loop (asyncio.to_thread),
    централизованная обработка ошибок.

Termux-особенности:
  • пути (bot_data.pkl, skills/, bot.log) — всегда рядом со скриптом;
  • при старте автоматически берётся termux-wake-lock, чтобы Android не усыплял бота;
  • web_fetch использует certifi как запасной набор CA-сертификатов.

Быстрый старт в Termux:
  pkg install -y python && pip install -U python-telegram-bot openai
  python tgbot.py            # вручную
  bash run.sh                # рекомендуемо: wake lock + автоперезапуск + лог bot.log
  bash stop.sh               # остановка

Ключи задаются переменными окружения (приоритет) или дефолтами ниже:
  TELEGRAM_TOKEN, AI_BASE_URL, AI_API_KEY, AI_MODEL
"""

from __future__ import annotations

import ast
import asyncio
import base64
import datetime
import html
import importlib
import importlib.util
import io
import json
import logging
import math
import os
import random
import re
import ssl
import subprocess
import sys
import time
import types
import urllib.request
from contextlib import suppress

from openai_compat import OpenAI
import openai_compat as openai
from telegram import BotCommand, Update
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.error import BadRequest, TimedOut
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PicklePersistence,
    filters,
)

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("tgbot")

# Все файлы (база истории, скилы, логи) держим рядом со скриптом:
# в Termux текущий рабочий каталог зависит от того, откуда запущено.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8602453984:AAGQS8TF9SBONspjQX6cJeiL59Sc5xit6vc")
AI_BASE_URL = os.getenv("AI_BASE_URL", "")
AI_API_KEY = os.getenv("AI_API_KEY", "")
DEFAULT_MODEL = os.getenv("AI_MODEL", "")

DEFAULT_SYSTEM_PROMPT = (
    "Ты — дружелюбный и полезный ИИ-ассистент в Telegram. "
    "Отвечай на языке пользователя, кратко и по делу. "
    "Если уместно — оформляй ответ в Markdown (списки, код, выделение). "
    "Тебе доступны скилы-инструменты (функции): вызывай их, когда это помогает "
    "дать точный ответ (актуальное время, вычисления, чтение веб-страницы и т.п.)."
)

HISTORY_MAX_MESSAGES = int(os.getenv("HISTORY_MAX_MESSAGES", "40"))  # сообщений в контексте
HISTORY_MAX_CHARS = int(os.getenv("HISTORY_MAX_CHARS", "12000"))     # мягкий лимит символов контекста
HISTORY_KEEP_ON_DISK = HISTORY_MAX_MESSAGES * 3                      # сколько храним между сессиями
TEMPERATURE = float(os.getenv("AI_TEMPERATURE", "0.7"))
MAX_REPLY_CHARS = 4096      # жёсткий лимит Telegram на одно сообщение
TYPING_INTERVAL = 4.0       # период отправки «печатает…» (сек)
PERSISTENCE_FILE = os.path.realpath(os.getenv(
    "PERSISTENCE_FILE", os.path.join(SCRIPT_DIR, "bot_data.pkl")))
DROP_PENDING_UPDATES = os.getenv("DROP_PENDING_UPDATES", "0") == "1"
# 1 = пропускать сообщения, пришедшие пока бот был офлайн (полезно на телефоне)
# Каталог внешних скилов (репозитории с GitHub кладутся сюда)
SKILLS_DIR = os.path.realpath(os.getenv(
    "SKILLS_DIR",
    os.path.join(SCRIPT_DIR, "skills"),
))

# Контекст модели и скилы
CHARS_PER_TOKEN = float(os.getenv("CHARS_PER_TOKEN", "3"))  # грубая оценка: ~3 символа на токен
RESERVE_TOKENS = int(os.getenv("RESERVE_TOKENS", "4096"))   # запас контекста под ответ модели
MAX_TOOL_ITERATIONS = int(os.getenv("MAX_TOOL_ITERATIONS", "6"))  # максимум цепочек вызова скилов

# Устойчивость к долгой генерации и сбоям шлюза (504 и т.п.)
AI_MAX_ATTEMPTS = int(os.getenv("AI_MAX_ATTEMPTS", "4"))          # попыток на один запрос к ИИ
AI_RETRY_BASE_DELAY = float(os.getenv("AI_RETRY_BASE_DELAY", "2.0"))  # базовая пауза между попытками, сек

# Таймауты Telegram API (по умолчанию в PTB всего 5 сек — мало для файлов)
TELEGRAM_READ_TIMEOUT = int(os.getenv("TELEGRAM_READ_TIMEOUT", "30"))  # сек
TELEGRAM_DL_ATTEMPTS = int(os.getenv("TELEGRAM_DL_ATTEMPTS", "3"))     # попыток скачать файл

# Клиент OpenAI-совместимого шлюза (OmniRoute)
ai_client = OpenAI(base_url=AI_BASE_URL, api_key=AI_API_KEY)


# ---------------------------------------------------------------------------
# Termux: определение окружения, wake lock, SSL-сертификаты
# ---------------------------------------------------------------------------

def is_termux() -> bool:
    """True, если скрипт запущен внутри Termux (Android)."""
    if os.getenv("TERMUX_VERSION"):
        return True
    prefix = os.getenv("PREFIX", "")
    return "/com.termux/" in prefix


def acquire_wake_lock() -> None:
    """В Termux просим Android держать CPU включённым, пока работает бот.
    Без wake lock система усыпляет процесс через несколько минут без экрана —
    long polling рвётся и сообщения перестают приходить. Дополнительно стоит
    отключить оптимизацию батареи для приложения Termux в настройках Android."""
    if not is_termux():
        return
    exe = os.path.join(
        os.getenv("PREFIX", "/data/data/com.termux/files/usr"),
        "bin", "termux-wake-lock",
    )
    try:
        if os.path.exists(exe):
            subprocess.run([exe], check=False, timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logger.info("Termux wake lock получен — Android не усыпит бота")
        else:
            logger.warning("termux-wake-lock не найден: pkg install termux-tools")
    except Exception as e:
        logger.warning("Не удалось получить termux-wake-lock: %s", e)


def _web_ssl_context() -> ssl.SSLContext | None:
    """Контекст с CA-сертификатами для urllib. В Termux системный набор CA
    бывает не настроен — тогда используем certifi (приходит с pip)."""
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


def _unverified_ssl_context() -> ssl.SSLContext:
    """Контекст без проверки сертификатов — последний резерв для web_fetch."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


_WEB_SSL_CONTEXT = _web_ssl_context()


# ---------------------------------------------------------------------------
# Telegram-утилиты
# ---------------------------------------------------------------------------

async def send_long_message(message, text: str, parse_mode: str | None = None):
    """Отправка текста с авто-разбивкой по лимиту Telegram (4096 символов)
    и ретраями при таймаутах сети."""
    for chunk_start in range(0, len(text), MAX_REPLY_CHARS):
        part = text[chunk_start:chunk_start + MAX_REPLY_CHARS]
        for attempt in range(1, TELEGRAM_DL_ATTEMPTS + 1):
            try:
                await message.reply_text(part, parse_mode=parse_mode)
                break
            except BadRequest:
                # Markdown может быть битым (незакрытые */` и т.п.) — шлём как plain text
                if parse_mode:
                    try:
                        await message.reply_text(part)
                        break
                    except TimedOut:
                        if attempt == TELEGRAM_DL_ATTEMPTS:
                            raise
                        await asyncio.sleep(1.5 * attempt)
                else:
                    raise
            except TimedOut:
                logger.warning("Отправка сообщения: попытка %d/%d — таймаут",
                               attempt, TELEGRAM_DL_ATTEMPTS)
                if attempt == TELEGRAM_DL_ATTEMPTS:
                    raise
                await asyncio.sleep(1.5 * attempt)


async def keep_typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int, interval: float = TYPING_INTERVAL):
    """Периодически отправляет «печатает…», пока задача не отменена."""
    try:
        while True:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass


async def fetch_image_bytes(update: Update) -> bytes | None:
    """Достаёт изображение из апдейта: фото (макс. разрешение) или документ-картинку.
    Серверы Telegram бывают медленными — ретраим таймауты несколько раз."""
    message = update.message
    if message is None:
        return None
    photo = (message.photo or [None])[-1]
    target = photo or (message.document if message.document else None)
    if target is None:
        return None

    last_error: Exception | None = None
    for attempt in range(1, TELEGRAM_DL_ATTEMPTS + 1):
        try:
            file = await target.get_file(read_timeout=TELEGRAM_READ_TIMEOUT)
            buf = io.BytesIO()
            await file.download_to_memory(buf, read_timeout=TELEGRAM_READ_TIMEOUT)
            return buf.getvalue()
        except TimedOut as e:
            last_error = e
            logger.warning("Скачивание изображения: попытка %d/%d — таймаут",
                           attempt, TELEGRAM_DL_ATTEMPTS)
            await asyncio.sleep(1.5 * attempt)
        except BadRequest as e:
            # файл недоступен (удалён, нет прав) — повторять бессмысленно
            logger.warning("Изображение недоступно: %s", e)
            return None
        except Exception as e:
            last_error = e
            logger.warning("Скачивание изображения: попытка %d/%d — %s",
                           attempt, TELEGRAM_DL_ATTEMPTS, e)
            await asyncio.sleep(1.5 * attempt)
    if last_error:
        raise last_error
    return None


# ---------------------------------------------------------------------------
# Скилы (инструменты) — регистрируются декоратором @skill
# ---------------------------------------------------------------------------

SKILLS: dict[str, dict] = {}  # name -> {"func", "description", "parameters", "kind", "source"}
_CURRENT_SKILL_SOURCE = "built-in"  # источник скилов, регистрируемых в данный момент
_TOOLS_CACHE: list | None = None    # кэш payload инструментов (сбрасывается при /reload)

# Ленивая загрузка скилов: при старте храним ТОЛЬКО метаданные (список),
# код/инструкции подключаются в момент вызова (см. invoke_skill).
_PY_SKILL_FILES: dict[str, dict] = {}   # name -> {"path", "modname", "pkg_dir", "kind", "source", ...}
_MD_SKILL_BODIES: dict[str, str] = {}   # name -> тело SKILL.md (читается при старте, без исполнения)
_MD_SKILL_META: dict[str, dict] = {}    # name -> {"description", "source"}
_SKILL_CATALOG: list[dict] = []         # каталог для системного промпта (стабильный порядок)


def skill(name: str, description: str, parameters: dict, kind: str = "tool"):
    """Декоратор регистрации скила: func(args: dict) -> str.
    kind="tool" — функция-инструмент; kind="prompt" — возвращает промпт для модели."""
    def decorator(func):
        if name in SKILLS:
            logger.warning("Скил '%s' перезаписывается (источник: %s)", name, _CURRENT_SKILL_SOURCE)
        SKILLS[name] = {
            "func": func,
            "description": description,
            "parameters": parameters,
            "kind": kind,
            "source": _CURRENT_SKILL_SOURCE,
        }
        return func
    return decorator


def tools_payload() -> list:
    """Модели уходит ОДИН универсальный инструмент use_skill(name, args).
    Схемы всех скилов не отправляются (ленивая загрузка): в системном
    промпте есть только каталог 'имя: описание' (см. skill_catalog_text)."""
    global _TOOLS_CACHE
    if _TOOLS_CACHE is None:
        _TOOLS_CACHE = [{
            "type": "function",
            "function": {
                "name": "use_skill",
                "description": (
                    "Вызвать скил по имени из каталога скилов. Используй, когда "
                    "это помогает дать точный ответ. Аргументы передавай так, "
                    "как описано у скила в каталоге."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Имя скила из каталога"},
                        "args": {"type": "object", "description": "Аргументы скила (JSON-объект)"},
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
            },
        }]
    return _TOOLS_CACHE


def confine(path: str) -> str | None:
    """Проверяет, что путь находится строго внутри SKILLS_DIR.
    Возвращает абсолютный путь или None, если выход за пределы каталога."""
    candidate = os.path.realpath(os.path.join(SKILLS_DIR, str(path)))
    root = SKILLS_DIR.rstrip(os.sep) + os.sep
    if candidate.startswith(root):
        return candidate
    return None


def safe_read_file(rel_path: str, max_bytes: int = 64_000) -> str | None:
    """Читает текстовый файл строго внутри SKILLS_DIR (для внешних скилов).
    Возвращает содержимое или None, если путь вне каталога / файл недоступен."""
    full = confine(rel_path)
    if full is None:
        return None
    try:
        with open(full, "rb") as fh:
            return fh.read(max_bytes).decode("utf-8", errors="replace")
    except OSError:
        return None


def execute_skill(name: str, arguments: str | None) -> str:
    """Безопасный запуск скила: любые ошибки превращаются в строку для модели."""
    meta = SKILLS.get(name)
    if not meta:
        return f"Ошибка: неизвестный скил '{name}'"
    try:
        args = json.loads(arguments or "{}")
        if not isinstance(args, dict):
            raise ValueError("аргументы должны быть JSON-объектом")
    except (ValueError, json.JSONDecodeError) as e:
        return f"Ошибка разбора аргументов скила '{name}': {e}"
    try:
        return str(meta["func"](args))
    except Exception as e:  # noqa: BLE001 — модель должна увидеть текст ошибки
        logger.exception("Ошибка в скиле %s", name)
        return f"Ошибка при выполнении скила '{name}': {e}"


@skill(
    "current_time",
    "Возвращает текущие дату и время (UTC и локальное), день недели.",
    {"type": "object", "properties": {}, "additionalProperties": False},
)
def _skill_current_time(args: dict) -> str:
    now = datetime.datetime.now().astimezone()
    utc = datetime.datetime.now(datetime.timezone.utc)
    return (
        f"Локальное время: {now.strftime('%Y-%m-%d %H:%M:%S %z (%A)')}\n"
        f"UTC: {utc.strftime('%Y-%m-%d %H:%M:%S')}"
    )


@skill(
    "calculate",
    "Вычисляет математическое выражение (арифметика, скобки, степени, "
    "sin/cos/tan/sqrt/log/log10/floor/ceil/pi/e).",
    {
        "type": "object",
        "properties": {
            "expression": {"type": "string", "description": "Например: (2+3)*sin(pi/6) или 2**10"}
        },
        "required": ["expression"],
        "additionalProperties": False,
    },
)
def _skill_calculate(args: dict) -> str:
    expr = str(args.get("expression", "")).strip()
    if not expr or len(expr) > 500:
        return "Ошибка: нужно непустое выражение до 500 символов"
    if re.search(r"__|import|exec|eval|open|input|\blambda\b", expr):
        return "Ошибка: выражение содержит запрещённые конструкции"
    allowed = {k: v for k, v in math.__dict__.items() if not k.startswith("_")}
    allowed.update({"abs": abs, "round": round, "min": min, "max": max})
    try:
        result = eval(expr, {"__builtins__": {}}, allowed)  # noqa: S307 — песочница выше
        return f"{expr} = {result}"
    except Exception as e:
        return f"Не удалось вычислить '{expr}': {e}"


@skill(
    "random_number",
    "Генерирует случайное целое число в диапазоне [from, to].",
    {
        "type": "object",
        "properties": {
            "from": {"type": "integer", "description": "Нижняя граница (по умолчанию 1)"},
            "to": {"type": "integer", "description": "Верхняя граница (по умолчанию 100)"},
        },
        "additionalProperties": False,
    },
)
def _skill_random_number(args: dict) -> str:
    lo = int(args.get("from", 1))
    hi = int(args.get("to", 100))
    if lo > hi:
        lo, hi = hi, lo
    return f"Случайное число от {lo} до {hi}: {random.randint(lo, hi)}"


@skill(
    "web_fetch",
    "Скачивает веб-страницу по URL и возвращает её текст "
    "(HTML теги вырезаются). Полезно для актуальной информации.",
    {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Полный URL, начиная с http:// или https://"}
        },
        "required": ["url"],
        "additionalProperties": False,
    },
)
def _skill_web_fetch(args: dict) -> str:
    url = str(args.get("url", "")).strip()
    if not re.match(r"^https?://", url):
        return "Ошибка: URL должен начинаться с http:// или https://"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; TelegramAIBot/1.0)",
            "Accept-Language": "ru,en;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15, context=_WEB_SSL_CONTEXT) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            raw = resp.read(1_000_000).decode(charset, errors="replace")
    except ssl.SSLCertVerificationError:
        # В Termux иногда отсутствует системный набор CA — последний резерв без проверки.
        logger.warning("web_fetch: нет CA-сертификатов, проверка отключена для %s", url)
        with urllib.request.urlopen(req, timeout=15, context=_unverified_ssl_context()) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            raw = resp.read(1_000_000).decode(charset, errors="replace")
    # вырезаем скрипты/стили и HTML-теги
    raw = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw[:6000] if raw else "Страница не содержит текста."


# ---------------------------------------------------------------------------
# Внешние скилы: загрузка из каталога SKILLS_DIR (репозитории с GitHub и т.п.)
# ---------------------------------------------------------------------------

def load_external_skills() -> dict[str, list[str]]:
    """Ленивая загрузка: при старте ТОЛЬКО сканируем каталог и собираем список
    скилов (имя, описание, параметры) — код .py не исполняется. Код/инструкция
    подключаются в момент вызова (invoke_skill). Поддерживаются *.py,
    пакеты с __init__.py и markdown-скилы SKILL.md."""
    result = {"loaded": [], "errors": []}

    if not os.path.isdir(SKILLS_DIR):
        os.makedirs(SKILLS_DIR, exist_ok=True)
        logger.info("Каталог скилов создан: %s", SKILLS_DIR)
        return result

    # чтобы лениво импортируемые скилы могли делать `from tgbot import skill`
    main_mod = sys.modules.get("__main__")
    if main_mod is not None and hasattr(main_mod, "SKILLS"):
        sys.modules.setdefault("tgbot", main_mod)

    _PY_SKILL_FILES.clear()
    _MD_SKILL_BODIES.clear()
    _MD_SKILL_META.clear()
    _SKILL_CATALOG.clear()
    # вычищаем лениво импортированные ранее скилы (для /reload)
    for name in [n for n, m in SKILLS.items() if m.get("source", "built-in") != "built-in"]:
        del SKILLS[name]
    global _TOOLS_CACHE
    _TOOLS_CACHE = None

    for root, dirs, files in os.walk(SKILLS_DIR):
        dirs[:] = [d for d in dirs if not d.startswith((".", "__"))]
        dirs.sort()
        packages = [d for d in list(dirs)
                    if os.path.isfile(os.path.join(root, d, "__init__.py"))]
        for d in packages:
            pkg_dir = os.path.join(root, d)
            try:
                if not os.path.realpath(pkg_dir).startswith(SKILLS_DIR + os.sep):
                    continue
            except OSError:
                continue
            init_path = os.path.join(pkg_dir, "__init__.py")
            _scan_py_skills(init_path, os.path.relpath(init_path, SKILLS_DIR),
                            result, pkg_dir=pkg_dir)
            dirs.remove(d)
        md_dirs = [d for d in list(dirs)
                   if os.path.isfile(os.path.join(root, d, "SKILL.md"))]
        for d in md_dirs:
            md_path = os.path.join(root, d, "SKILL.md")
            try:
                if not os.path.realpath(md_path).startswith(SKILLS_DIR + os.sep):
                    continue
            except OSError:
                continue
            _register_markdown_skill_lazy(md_path,
                                          os.path.relpath(md_path, SKILLS_DIR), result)
            dirs.remove(d)
        for fn in sorted(files):
            fpath = os.path.join(root, fn)
            try:
                if not os.path.realpath(fpath).startswith(SKILLS_DIR + os.sep):
                    continue
            except OSError:
                continue
            if fn.endswith(".py") and not fn.startswith("_"):
                _scan_py_skills(fpath, os.path.relpath(fpath, SKILLS_DIR), result)
            elif fn == "SKILL.md":
                _register_markdown_skill_lazy(fpath,
                                              os.path.relpath(fpath, SKILLS_DIR), result)
    return result


def _scan_py_skills(path: str, source: str, result: dict,
                    pkg_dir: str | None = None) -> None:
    """Находит @skill-декораторы в файле БЕЗ исполнения кода (AST-разбор)."""
    try:
        with open(path, "rb") as fh:
            tree = ast.parse(fh.read(500_000).decode("utf-8", errors="replace"))
    except Exception as e:
        result["errors"].append(f"{source}: {e}")
        return

    def _const(node):
        return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None

    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call)
                    and ((isinstance(dec.func, ast.Name) and dec.func.id == "skill")
                         or (isinstance(dec.func, ast.Attribute) and dec.func.attr == "skill"))):
                continue
            name = _const(dec.args[0]) if dec.args else None
            desc = _const(dec.args[1]) if len(dec.args) > 1 else None
            kind = "tool"
            if len(dec.args) > 3:
                kind = _const(dec.args[3]) or "tool"
            for kw in dec.keywords:
                if kw.arg == "kind":
                    kind = _const(kw.value) or kind
            params = None
            if len(dec.args) > 2:
                try:
                    params = ast.literal_eval(dec.args[2])
                except Exception:
                    params = None
            name = name or node.name
            if name in SKILLS or name in _PY_SKILL_FILES:
                continue  # встроенные и уже найденные не дублируем
            _PY_SKILL_FILES[name] = {
                "path": path, "pkg_dir": pkg_dir,
                "modname": "skills_ext_" + re.sub(r"\W", "_",
                           os.path.relpath(path, SKILLS_DIR)),
                "kind": kind, "source": source,
                "description": (desc or name)[:200],
                "parameters": params if isinstance(params, dict) else {
                    "type": "object", "properties": {}, "additionalProperties": False},
            }
            count += 1
    if count:
        result["loaded"].append(f"{source}: скилов {count}")


def _register_markdown_skill_lazy(md_path: str, rel: str, result: dict) -> None:
    """SKILL.md: при старте читаем только заголовок (имя/описание).
    Тело-инструкция подключается при вызове (invoke_skill)."""
    meta = _parse_skill_md(md_path)
    if meta is None or not meta["body"]:
        result["errors"].append(f"{rel}: пустой или нечитаемый SKILL.md")
        return
    name = meta["name"][:64]
    if name in SKILLS or name in _MD_SKILL_BODIES:
        result["errors"].append(f"{rel}: имя '{name}' занято — скил пропущен")
        return
    _MD_SKILL_BODIES[name] = meta["body"][:24_000]
    _MD_SKILL_META[name] = {"description": meta["description"][:200], "source": rel}
    result["loaded"].append(f"{rel}: markdown-скил")


def skill_names() -> set:
    """Все известные скилы: встроенные + внешние .py + markdown."""
    return set(SKILLS) | set(_PY_SKILL_FILES) | set(_MD_SKILL_BODIES)


def skill_catalog_text() -> str:
    """Каталог 'имя: описание' для системного промпта (без схем и кода)."""
    if os.getenv("AI_DISABLE_TOOLS") == "1":
        return ""  # тестовый режим: инструменты полностью выключены
    if not _SKILL_CATALOG:
        return ""
    lines = ["",
             "=== КАТАЛОГ СКИЛОВ ===",
             "Для вызова скила используй инструмент use_skill(name, args), "
             "name — точно как в каталоге. Не выдумывай имена."]
    for meta in _SKILL_CATALOG:
        kind = " [промпт-скил]" if meta.get("kind") == "prompt" else ""
        lines.append(f"- {meta['name']}: {meta['description']}{kind}")
    lines.append("=== КОНЕЦ КАТАЛОГА ===")
    return "\n".join(lines)


def _rebuild_catalog() -> None:
    """Пересобирает каталог (стабильный порядок — важно для кэша промпта)."""
    entries = []
    for name, m in SKILLS.items():
        entries.append({"name": name, "description": m["description"][:200],
                        "kind": m.get("kind", "tool")})
    for name, m in _PY_SKILL_FILES.items():
        entries.append({"name": name, "description": m["description"][:200],
                        "kind": m.get("kind", "tool")})
    for name in _MD_SKILL_BODIES:
        entries.append({"name": name,
                        "description": _MD_SKILL_META.get(name, {}).get(
                            "description", "Markdown-скил"),
                        "kind": "prompt"})
    _SKILL_CATALOG[:] = sorted(entries, key=lambda e: e["name"])


def invoke_skill(name: str, arguments: str | None) -> str:
    """Единая точка вызова скила. Код .py импортируется ТОЛЬКО сейчас;
    markdown-скил отдаёт инструкцию прямо здесь. Ошибки — строкой для модели."""
    name = (name or "").strip()
    if name in SKILLS:
        return execute_skill(name, arguments)
    meta = _PY_SKILL_FILES.get(name)
    if meta:
        load_result = {"loaded": [], "errors": []}
        _load_skill_module(meta["path"], meta["modname"], load_result,
                           pkg_dir=meta.get("pkg_dir"))
        if name in SKILLS:
            return execute_skill(name, arguments)
        return (f"Ошибка: файл {meta['source']} загружен, но скил '{name}' "
                f"не зарегистрировался")
    if name in _MD_SKILL_BODIES:
        body = _MD_SKILL_BODIES[name]
        try:
            args = json.loads(arguments or "{}")
            if not isinstance(args, dict):
                args = {}
        except (ValueError, json.JSONDecodeError):
            args = {}
        task = str(args.get("task", "")).strip() or "(без уточнения)"
        return (
            "Работай строго по следующей инструкции-скилу.\n\n"
            f"=== ИНСТРУКЦИЯ СКИЛА '{name}' ===\n{body}\n"
            f"=== КОНЕЦ ИНСТРУКЦИИ ===\n\n"
            f"Запрос пользователя к этому скилу: {task}\n\n"
            "Выполни запрос, следуя инструкции. Если инструкция предполагает "
            "диалог/вопросы — задай их в ответе."
        )
    known = ", ".join(sorted(skill_names())[:30])
    return f"Ошибка: неизвестный скил '{name}'. Доступны: {known}"


def _load_skill_module(path: str, modname: str, result: dict,
                       pkg_dir: str | None = None) -> None:
    """Импортирует один файл скила заново при каждой загрузке (надёжный /reload).
    Для пакетов задаёт submodule_search_locations — работают относительные импорты."""
    global _CURRENT_SKILL_SOURCE
    rel = os.path.relpath(path, SKILLS_DIR)
    try:
        # сбрасываем предыдущую версию модуля и его подмодулей (для /reload)
        for stale in [m for m in sys.modules
                      if m == modname or m.startswith(modname + ".")]:
            del sys.modules[stale]

        # источник выставляем ДО исполнения — декораторы @skill его читают
        _CURRENT_SKILL_SOURCE = rel
        spec = importlib.util.spec_from_file_location(
            modname, path,
            submodule_search_locations=[pkg_dir] if pkg_dir else None,
        )
        if spec is None or spec.loader is None:
            raise ImportError("не удалось создать spec для файла")
        module = importlib.util.module_from_spec(spec)
        sys.modules[modname] = module

        # pkg_dir в sys.path: чтобы работали и абсолютные self-импорты внутри пакета
        added_paths = []
        if pkg_dir and pkg_dir not in sys.path:
            sys.path.insert(0, pkg_dir)
            added_paths.append(pkg_dir)
        try:
            spec.loader.exec_module(module)
        finally:
            for p in added_paths:
                with suppress(ValueError):
                    sys.path.remove(p)

        result["loaded"].append(rel)
        logger.info("Загружен скил-файл: %s", rel)
    except Exception as e:
        sys.modules.pop(modname, None)  # не оставляем полудохлый модуль
        logger.exception("Ошибка загрузки скила %s", rel)
        result["errors"].append(f"{rel}: {e}")
    finally:
        _CURRENT_SKILL_SOURCE = "built-in"


# ---------------------------------------------------------------------------
# Markdown-скилы (формат Claude: папка со SKILL.md и YAML-заголовком)
# ---------------------------------------------------------------------------

def _parse_skill_md(path: str) -> dict | None:
    """Парсит SKILL.md: YAML front-matter (name, description) + тело-инструкция."""
    try:
        with open(path, "rb") as fh:
            text = fh.read(200_000).decode("utf-8", errors="replace")
    except OSError as e:
        logger.warning("Не удалось прочитать %s: %s", path, e)
        return None
    meta: dict = {"name": "", "description": ""}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            front, body = parts[1], parts[2]
            # лёгкий парсер YAML: name/description (+ вложенный metadata.version)
            current_key = None
            for line in front.splitlines():
                if not line.strip() or line.strip().startswith("#"):
                    continue
                m = re.match(r"^(\s*)([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
                if not m:
                    continue
                indent, key, value = m.groups()
                if indent == "":  # верхний уровень
                    current_key = key.lower()
                    if current_key in ("name", "description"):
                        meta[current_key] = value.strip().strip("\"'")
                elif current_key == "metadata":
                    continue  # вложенные поля (version и т.п.) не критичны
    body = body.strip()
    if not meta["name"]:
        # имя возьмём из имени папки/файла
        parent = os.path.basename(os.path.dirname(path))
        meta["name"] = parent if parent and parent != "skills" else \
            os.path.splitext(os.path.basename(path))[0]
    if not meta["description"]:
        meta["description"] = body.split("\n", 1)[0][:200] if body else "Markdown-скил"
    meta["body"] = body
    return meta


def _register_markdown_skill(md_path: str, rel: str, result: dict) -> None:
    """Регистрирует SKILL.md как скил: при вызове модель получает инструкцию
    из тела файла и работает по ней (аналог того, как это делает Claude)."""
    global _CURRENT_SKILL_SOURCE
    meta = _parse_skill_md(md_path)
    if meta is None or not meta["body"]:
        result["errors"].append(f"{rel}: пустой или нечитаемый SKILL.md")
        return

    skill_name = meta["name"][:64]
    description = meta["description"][:500]
    body = meta["body"]
    if len(body) > 24_000:  # страховка от гигантских инструкций
        body = body[:24_000] + "\n…(инструкция обрезана)"

    _CURRENT_SKILL_SOURCE = rel

    @skill(skill_name, description,
           {
               "type": "object",
               "properties": {
                   "task": {"type": "string",
                            "description": "Что нужно сделать по этой инструкции "
                                           "(запрос/контекст пользователя)"}
               },
               "required": ["task"],
               "additionalProperties": False,
           },
           kind="prompt")
    def _markdown_skill(args: dict) -> str:
        task = str(args.get("task", "")).strip() or "(без уточнения)"
        return (
            "Работай строго по следующей инструкции-скилу.\n\n"
            f"=== ИНСТРУКЦИЯ СКИЛА '{skill_name}' ===\n{body}\n"
            f"=== КОНЕЦ ИНСТРУКЦИИ ===\n\n"
            f"Запрос пользователя к этому скилу: {task}\n\n"
            "Выполни запрос, следуя инструкции. Если инструкция предполагает "
            "диалог/вопросы — задай их в ответе."
        )

    # переопределяем отображаемое имя функции для читаемости
    _markdown_skill.__name__ = f"_md_{skill_command_name(skill_name)}"
    result["loaded"].append(rel)
    logger.info("Загружен markdown-скил: %s (name=%s)", rel, skill_name)
    _CURRENT_SKILL_SOURCE = "built-in"


# ---------------------------------------------------------------------------
# Определение контекста модели с сервера (OmniRoute /v1/models)
# ---------------------------------------------------------------------------

MODEL_CONTEXT_CACHE: dict[str, tuple[float, int | None]] = {}
MODEL_CONTEXT_TTL = 600.0  # сек, кэшируем параметры модели


def _extract_context_len(model_obj) -> int | None:
    """Достаёт длину контекста из объекта/словаря модели (у шлюзов поля различаются)."""
    try:
        data = model_obj.model_dump() if hasattr(model_obj, "model_dump") else dict(model_obj)
    except Exception:
        data = model_obj.__dict__ if hasattr(model_obj, "__dict__") else {}
    # рекурсивно ищем ключи вида *context*, *ctx* со значением-числом
    candidates: list[int] = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                kl = str(key).lower()
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    if any(t in kl for t in ("context_length", "context_window", "context_size",
                                             "max_context", "max_ctx", "ctx_len")):
                        candidates.append(int(value))
                elif isinstance(value, dict):
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    if candidates:
        return max(candidates)  # берём максимальный найденный
    # эвристика по атрибутам объекта
    for attr in ("context_length", "context_window", "max_context_length"):
        value = getattr(model_obj, attr, None)
        if isinstance(value, (int, float)) and value:
            return int(value)
    return None


def detect_model_context(model: str) -> int | None:
    """Запрашивает у шлюза параметры модели и возвращает длину контекста (токенов)."""
    now = time.time()
    cached = MODEL_CONTEXT_CACHE.get(model)
    if cached and now - cached[0] < MODEL_CONTEXT_TTL:
        return cached[1]
    context_len: int | None = None
    try:
        # точный запрос конкретной модели
        context_len = _extract_context_len(ai_client.models.retrieve(model))
    except Exception:
        pass
    if not context_len:
        try:
            # fallback: ищем в общем списке моделей
            for m in ai_client.models.list().data:
                if getattr(m, "id", "") == model:
                    context_len = _extract_context_len(m)
                    break
        except Exception:
            pass
    if context_len and context_len < 1024:
        context_len = None  # мусорное значение — игнорируем
    MODEL_CONTEXT_CACHE[model] = (now, context_len)
    if context_len:
        logger.info("Контекст модели %s: %d токенов (с сервера)", model, context_len)
    else:
        logger.info("Не удалось узнать контекст модели %s — используем лимит по символам", model)
    return context_len


def history_budget(model: str | None) -> tuple[int, int]:
    """Возвращает (макс_сообщений, макс_символов) истории под конкретную модель.
    Если контекст известен с сервера — считаем в токенах, иначе — дефолт по символам."""
    context_len = detect_model_context(model) if model else None
    if context_len:
        budget_tokens = max(1024, context_len - RESERVE_TOKENS)
        max_chars = max(2000, int(budget_tokens * CHARS_PER_TOKEN))
        max_messages = max(8, min(200, budget_tokens // 100))
        return max_messages, max_chars
    return HISTORY_MAX_MESSAGES, HISTORY_MAX_CHARS


# ---------------------------------------------------------------------------
# Работа с историей диалога
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Собственное хранилище чатов (вместо PTB chat_data/PicklePersistence)
# ---------------------------------------------------------------------------

STORE_FILE = os.path.join(SCRIPT_DIR, "bot_history.json")
CHAT_STORE: dict[int, dict] = {}


def store_for(update: Update) -> dict:
    """Единый словарь состояния чата: живёт всё время работы процесса и
    сохраняется на диск после каждого изменения. PTB в этой среде пересоздаёт
    chat_data на каждый апдейт (см. лог [HIST]: dict меняется при одном pid) —
    поэтому историю держим сами."""
    chat = update.effective_chat
    return CHAT_STORE.setdefault(int(chat.id) if chat else 0, {})


def save_store() -> None:
    """Атомарная запись: tmp + os.replace — файл всегда валиден, даже если
    процесс убьют в момент записи."""
    try:
        tmp = STORE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(CHAT_STORE, fh, ensure_ascii=False, default=str)
        os.replace(tmp, STORE_FILE)
    except Exception:
        logger.exception("Не удалось сохранить %s", STORE_FILE)


def load_store() -> None:
    global CHAT_STORE
    if not os.path.exists(STORE_FILE):
        return
    try:
        with open(STORE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            CHAT_STORE = {int(k): v for k, v in data.items() if isinstance(v, dict)}
            logger.info("Хранилище загружено: чатов %d", len(CHAT_STORE))
    except Exception:
        logger.exception("Не удалось прочитать %s — начинаем с пустого", STORE_FILE)


def content_length(content) -> int:
    """Приблизительный «размер» сообщения: str либо vision-список частей."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    total += len(part.get("text", ""))
                elif part.get("type") == "image_url":
                    total += 1000  # условная «стоимость» картинки в контексте
        return total
    return 0


def remember(chat_data: dict, role: str, content) -> None:
    """Добавляем сообщение в историю чата и подрезаем её для хранения."""
    history = chat_data.setdefault("history", [])
    history.append({"role": role, "content": content})
    del history[:-HISTORY_KEEP_ON_DISK]


def bump_epoch(chat_data: dict) -> int:
    """Увеличивает «поколение» диалога (после /reset) и возвращает новое значение.
    Активные генерации сравнивают свой epoch с этим числом и отбрасывают
    устаревшие результаты — иначе долгий скил записывал бы ответ В НОВЫЙ
    (только что очищенный) диалог, и '/reset' не работал во время генерации."""
    chat_data["epoch"] = chat_data.get("epoch", 0) + 1
    return chat_data["epoch"]


def epoch_changed(chat_data: dict, epoch: int) -> bool:
    """True, если диалог был сброшен/изменил поколение после начала операции."""
    return chat_data.get("epoch", 0) != epoch


def trim_history(history: list, max_messages: int, max_chars: int) -> list:
    """Обрезаем историю под лимиты контекста (по числу сообщений и символам)."""
    msgs = history[-max_messages:]
    total = 0
    start = 0
    for i in range(len(msgs) - 1, -1, -1):
        total += content_length(msgs[i].get("content", ""))
        if total > max_chars:
            start = i + 1
            break
    return msgs[start:]


def build_messages(chat_data: dict, model: str | None = None) -> list:
    """Payload для API: системный промпт + история, обрезанная под бюджет модели.
    Может обратиться к сети за параметрами модели (кэшируется) — вызывать из потока."""
    # Сжимаем старые гигантские промпты скилов ДО обрезки истории: они (по 8-24 КБ)
    # при малом бюджете выталкивали всю беседу — у пользователя «пропадал контекст»
    # после использования скилов. Свежий промпт (последнее сообщение) не трогаем —
    # модель должна получить полную инструкцию текущего вызова.
    history = chat_data.get("history")
    if history:
        compact_skill_prompts(chat_data, keep_last=True)
    max_messages, max_chars = history_budget(model)
    system_prompt = chat_data.get("system_prompt", DEFAULT_SYSTEM_PROMPT)
    system_prompt += skill_catalog_text()
    return [{"role": "system", "content": system_prompt}] + trim_history(
        chat_data.get("history", []), max_messages, max_chars
    )


def downgrade_old_images(chat_data: dict) -> None:
    """После ответа заменяем старые vision-сообщения текстовой заглушкой,
    чтобы не отправлять тяжёлые base64-картинки в каждом следующем запросе."""
    history = chat_data.get("history", [])
    for msg in history[:-1]:
        content = msg.get("content")
        if isinstance(content, list):
            texts = " ".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ).strip()
            msg["content"] = (texts + "\n[пользователь отправил изображение]").strip()


# ---------------------------------------------------------------------------
# Ядро: обращение к ИИ
# ---------------------------------------------------------------------------

def _chat_completion_with_retry(model: str, messages: list, tools: list | None = None):
    """Один запрос к ИИ со стримингом и собственными ретраями.

    Стриминг решает проблему 504 от OmniRoute: шлюз обрывает запросы, которые
    молчат дольше 15 сек (maxWaitMs), а при stream=True первый чанк приходит
    быстро и данные текут постоянно, поэтому долгая генерация не рвётся.
    504/таймауты/сбои соединения переживаются повторными попытками с backoff.
    Возвращает (content, tool_calls) последнего полученного сообщения."""
    last_error: Exception | None = None
    for attempt in range(1, AI_MAX_ATTEMPTS + 1):
        try:
            kwargs = {"model": model, "messages": messages, "temperature": TEMPERATURE,
                      "stream": True}
            if tools:
                kwargs["tools"] = tools
            stream = ai_client.chat.completions.create(**kwargs)

            content_parts: list[str] = []
            tool_calls_acc: dict[int, dict] = {}  # index -> {id, name, arguments}
            for event in stream:
                if not event.choices:
                    continue
                delta = event.choices[0].delta
                if delta is None:
                    continue
                if delta.content:
                    content_parts.append(delta.content)
                for tc in (getattr(delta, "tool_calls", None) or []):
                    slot = tool_calls_acc.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            slot["name"] = tc.function.name
                        if tc.function.arguments:
                            slot["arguments"] += tc.function.arguments

            content = "".join(content_parts)
            tool_calls = None
            if tool_calls_acc:
                tool_calls = [
                    types.SimpleNamespace(  # совместимо с .model_dump() и .id/.function
                        id=slot["id"] or f"call_{i}",
                        function=types.SimpleNamespace(name=slot["name"], arguments=slot["arguments"]),
                    )
                    for i, slot in sorted(tool_calls_acc.items())
                ]
            return content, tool_calls
        except openai.InternalServerError as e:
            # 504 и прочие 5xx — шлюз не успел/перегружен, пробуем ещё раз
            last_error = e
            logger.warning("Попытка %d/%d: ошибка шлюза: %s", attempt, AI_MAX_ATTEMPTS, e)
        except (openai.APIConnectionError, openai.APITimeoutError) as e:
            last_error = e
            logger.warning("Попытка %d/%d: проблема соединения: %s", attempt, AI_MAX_ATTEMPTS, e)
        if attempt < AI_MAX_ATTEMPTS:
            time.sleep(AI_RETRY_BASE_DELAY * (2 ** (attempt - 1)))  # 2, 4, 8 сек...
    if last_error is not None:
        raise last_error  # все попытки исчерпаны
    raise RuntimeError("Запрос к ИИ не выполнен: неизвестная ошибка")


def ai_chat_sync(model: str, messages: list) -> str:
    """Блокирующий агентный цикл с поддержкой скилов (function calling).
    Модель просит вызвать инструмент -> выполняем -> возвращаем результат,
    пока не получит финальный ответ (не более MAX_TOOL_ITERATIONS итераций)."""
    if os.getenv("AI_DISABLE_TOOLS") == "1":
        tools = None  # тестовый режим: совсем без инструментов
    else:
        tools = tools_payload()
    for _ in range(MAX_TOOL_ITERATIONS):
        content, tool_calls = _chat_completion_with_retry(model, messages, tools or None)
        if not tool_calls:
            return content
        # фиксируем запрос инструментов в переписке
        messages.append({
            "role": "assistant",
            "content": content or "",
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in tool_calls
            ],
        })
        # выполняем скилы (лениво: код подключается только сейчас)
        for tc in tool_calls:
            fname = tc.function.name or ""
            if fname == "use_skill":
                try:
                    payload = json.loads(tc.function.arguments or "{}")
                    if not isinstance(payload, dict):
                        payload = {}
                except (ValueError, json.JSONDecodeError):
                    payload = {}
                inner = payload.get("args", {})
                args_json = (inner if isinstance(inner, str)
                             else json.dumps(inner, ensure_ascii=False))
                result = invoke_skill(str(payload.get("name", "")), args_json)
            elif fname in skill_names():
                # модель вызвала скил напрямую по имени — тоже поддерживаем
                result = invoke_skill(fname, tc.function.arguments)
            else:
                result = (f"Ошибка: неизвестный инструмент '{fname}'. "
                          "Доступен use_skill(name, args) — имена в каталоге скилов.")
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })
    # лимит итераций исчерпан — просим модель подвести итог без инструментов
    messages.append({
        "role": "user",
        "content": "(система: лимит вызовов инструментов исчерпан — дай финальный ответ без них)",
    })
    content, _ = _chat_completion_with_retry(model, messages)
    return content


def image_message(text: str | None, image_bytes: bytes) -> list:
    """Vision-сообщение: текстовая часть + картинка в base64 data-URL."""
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    parts = [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]
    if text:
        parts.insert(0, {"type": "text", "text": text})
    return parts


# ---------------------------------------------------------------------------
# Обработчики команд
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return
    remember(store_for(update), "assistant",
             "Бот поприветствовал пользователя командой /start.")
    await update.message.reply_text(
        "Привет! 👋 Я полноценный ИИ-чат-агент.\n\n"
        "• Просто пишите мне — я помню контекст нашей беседы.\n"
        "• Пришлите фото — я его разберу (поддерживается vision).\n"
        "• В группах ответьте reply на моё сообщение или упомяните меня через @.\n"
        "• У меня есть скилы (время, калькулятор, чтение веб-страниц) — /skills\n\n"
        "Команды:\n"
        "/help — справка\n"
        "/reset — очистить историю диалога\n"
        "/model [имя] — показать/сменить модель\n"
        "/prompt [текст] — посмотреть/задать системный промпт\n"
        "/stats — статистика диалога и контекст модели"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return
    await update.message.reply_text(
        "<b>Как со мной работать</b>\n\n"
        "Я агент с памятью: каждое ваше сообщение учитывается в контексте беседы. "
        "История сохраняется между перезапусками бота.\n\n"
        "<b>Команды</b>\n"
        "/reset — забыть диалог (начать с чистого листа)\n"
        "/model — текущая модель; <code>/model имя</code> — переключить\n"
        "/prompt — текущий системный промпт; <code>/prompt текст</code> — задать свой\n"
        "/stats — размер истории, контекст модели и настройки чата\n\n"
        "<b>Скилы</b>\n"
        "/skills — список скилов-инструментов (с указанием источника)\n"
        "/run скил JSON — вызвать скил вручную\n"
        "/reload — перечитать скилы из папки skills без перезапуска\n\n"
        "<b>Изображения</b>\n"
        "Отправьте фото или документ-картинку (можно с подписью-вопросом).",
        parse_mode=ParseMode.HTML,
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return
    chat_data = store_for(update)
    chat_data.pop("history", None)
    chat_data.pop("system_prompt", None)
    bump_epoch(chat_data)  # активные генерации увидят смену поколения и не допишут историю
    save_store()
    await update.message.reply_text("🧹 История диалога очищена. Начинаем с чистого листа!")


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return
    chat_data = store_for(update)
    args = context.args
    if not args:
        current = chat_data.get("model", DEFAULT_MODEL)
        await update.message.reply_text(
            f"Текущая модель: <code>{html.escape(current)}</code>\n"
            f"Сменить: <code>/model имя_модели</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    chat_data["model"] = " ".join(args).strip()
    # сбрасываем кэш контекста для новой модели, чтобы бюджет пересчитался
    MODEL_CONTEXT_CACHE.pop(chat_data["model"], None)
    save_store()
    await update.message.reply_text(f"✅ Модель переключена на: {chat_data['model']}")


async def cmd_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return
    chat_data = store_for(update)
    if not context.args:
        current = chat_data.get("system_prompt", DEFAULT_SYSTEM_PROMPT)
        await update.message.reply_text(
            f"Текущий системный промпт:\n\n{current}\n\n"
            "Задать свой: /prompt твой текст · сбросить: /prompt default"
        )
        return
    text = " ".join(context.args).strip()
    if text.lower() in ("default", "сброс", "reset"):
        chat_data.pop("system_prompt", None)
        save_store()
        await update.message.reply_text("♻️ Системный промпт сброшен к значению по умолчанию.")
    else:
        chat_data["system_prompt"] = text
        save_store()
        await update.message.reply_text("✅ Новый системный промпт сохранён для этого чата.")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return
    chat_data = store_for(update)
    history = chat_data.get("history", [])
    print(f"[HIST] /stats: pid={os.getpid()} "
          f"chat={update.effective_chat.id} dict={id(chat_data)} "
          f"сообщений={len(history)}", flush=True)
    user_msgs = sum(1 for m in history if m["role"] == "user")
    ai_msgs = sum(1 for m in history if m["role"] == "assistant")
    chars = sum(content_length(m.get("content", "")) for m in history)
    model = chat_data.get("model", DEFAULT_MODEL)
    status = await update.message.reply_text("📊 Собираю статистику…")
    # запрос контекста у сервера — блокирующий, выносим из event loop
    max_messages, max_chars = await asyncio.to_thread(history_budget, model)
    context_len = MODEL_CONTEXT_CACHE.get(model, (0, None))[1]  # уже в кэше после history_budget
    ctx_line = f"{context_len} токенов (с сервера)" if context_len else "неизвестен шлюзу"
    await status.edit_text(
        f"📊 Статистика диалога:\n"
        f"• сообщений в истории: {len(history)} (ваших: {user_msgs}, моих: {ai_msgs})\n"
        f"• объём истории: ~{chars} символов\n"
        f"• модель: {model}\n"
        f"• контекст модели: {ctx_line}\n"
        f"• бюджет истории: до {max_messages} сообщений / {max_chars} символов\n"
        f"• температура: {TEMPERATURE}\n"
        f"• скилы: {len(skill_names())} шт. (список: /skills)"
    )


async def cmd_skills(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список доступных скилов-инструментов."""
    if update.message is None:
        return
    all_names = sorted(skill_names())
    if not all_names:
        await update.message.reply_text("Скилы не зарегистрированы.")
        return
    lines = ["🛠 <b>Доступные скилы</b> (модель вызывает их через use_skill):\n"]
    for name in all_names:
        if name in SKILLS:
            meta = SKILLS[name]
        elif name in _PY_SKILL_FILES:
            meta = _PY_SKILL_FILES[name]
        else:
            meta = _MD_SKILL_META.get(name, {})
        src = meta.get("source", "built-in")
        marker = "встроенный" if src == "built-in" else f"из файла: {html.escape(src)}"
        desc = html.escape(meta["description"][:160]) + ("…" if len(meta["description"]) > 160 else "")
        lines.append(f"• <code>{html.escape(name)}</code> — {desc}\n  <i>({marker})</i>")
    lines.append("\nВызвать напрямую: /run <code>скил</code> JSON-аргументы")
    # разбивка на части — 50+ скилов с описаниями не влезают в лимит Telegram
    await send_long_message(update.message, "\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручной запуск скила: /run calculate {\"expression\": \"2+2\"}"""
    if update.message is None:
        return
    if not context.args:
        await update.message.reply_text("Использование: /run имя_скила JSON-аргументы\n"
                                        "Список скилов: /skills")
        return
    name = context.args[0]
    raw_args = " ".join(context.args[1:]).strip() or "{}"
    if name not in skill_names():
        await update.message.reply_text(f"Неизвестный скил '{name}'. Доступны: {', '.join(sorted(skill_names()))}")
        return
    result = await asyncio.to_thread(invoke_skill, name, raw_args)
    text = result if len(result) <= 3500 else result[:3500] + "…(обрезано)"
    await send_long_message(update.message, f"Результат <code>{html.escape(name)}</code>:\n{html.escape(text)}",
                            parse_mode=ParseMode.HTML)


async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Перечитывает скилы из каталога skills/ без перезапуска бота."""
    if update.message is None:
        return
    result = await asyncio.to_thread(load_external_skills)
    _rebuild_catalog()
    await refresh_bot_commands(context.bot)
    lines = [f"🔄 Перечитано из <code>{html.escape(SKILLS_DIR)}</code>:"]
    if result["loaded"]:
        lines.append(f"• загружено файлов: {len(result['loaded'])}")
        lines.extend(f"  ✔ {html.escape(f)}" for f in result["loaded"])
    else:
        lines.append("• файлы скилов (.py / SKILL.md) не найдены")
    if result["errors"]:
        lines.append(f"• ошибок: {len(result['errors'])}")
        lines.extend(f"  ✖ {html.escape(e)}" for e in result["errors"])
    lines.append(f"• всего скилов доступно модели: {len(skill_names())}")
    # разбивка на части — при большом каталоге список не влезает в лимит Telegram
    await send_long_message(update.message, "\n".join(lines), parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# Меню команд Telegram: каждый скил — отдельная команда с подсказкой
# ---------------------------------------------------------------------------

BUILTIN_COMMANDS = [
    ("start", "Начало работы — что я умею"),
    ("help", "Справка по возможностям"),
    ("reset", "Очистить историю диалога"),
    ("model", "Показать/сменить модель"),
    ("prompt", "Посмотреть/задать системный промпт"),
    ("stats", "Статистика диалога и контекста"),
    ("skills", "Список скилов-инструментов"),
    ("run", "Вызвать скил: /run имя JSON"),
    ("reload", "Перечитать скилы из папки skills"),
]

# имена команд Telegram: латиница/цифры/подчёркивание, 1-32 символа
_CMD_NAME_RE = re.compile(r"[^a-z0-9_]+")

# транслитерация кириллицы для имён команд (Telegram допускает только a-z0-9_)
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def skill_command_name(skill_name: str) -> str:
    """Превращает имя скила в допустимое имя Telegram-команды (a-z0-9_)."""
    name = skill_name.lower()
    name = "".join(_TRANSLIT.get(ch, ch) for ch in name)
    return _CMD_NAME_RE.sub("_", name).strip("_")[:32] or "skill"


def build_bot_commands() -> list[BotCommand]:
    """Меню команд: встроенные + по команде на каждый скил."""
    commands = [BotCommand(name, desc) for name, desc in BUILTIN_COMMANDS]
    used: set[str] = set()
    for name in sorted(skill_names()):
        cmd = skill_command_name(name)
        if cmd in used or cmd in dict(BUILTIN_COMMANDS):
            continue  # не затираем встроенные команды и дубликаты
        used.add(cmd)
        if name in SKILLS:
            description = SKILLS[name]["description"][:256]
        elif name in _PY_SKILL_FILES:
            description = _PY_SKILL_FILES[name]["description"][:256]
        else:
            description = _MD_SKILL_META.get(name, {}).get("description", "markdown-скил")[:256]
        commands.append(BotCommand(cmd, f"🛠 {description}"))
    return commands[:100]  # Telegram допускает максимум 100 команд


async def refresh_bot_commands(bot) -> None:
    """Публикует меню команд в Telegram (появляется при вводе '/')."""
    with suppress(Exception):
        await bot.set_my_commands(build_bot_commands())


async def post_init(application) -> None:
    """Вызывается после инициализации Application: меню команд + чистка истории."""
    await refresh_bot_commands(application.bot)

    # Чистим старые «отравленные» истории: гигантские промпты скилов (8-24 КБ),
    # сохранённые до исправления, выталкивали контекст беседы.
    try:
        cleaned = 0
        for chat_data in CHAT_STORE.values():
            if chat_data:
                cleaned += compact_skill_prompts(chat_data)
        if cleaned:
            logger.info("Сжато старых промптов скилов в историях: %d", cleaned)
    except Exception:
        logger.exception("Не удалось почистить истории при старте")


def _skill_param_hint(parameters: dict) -> str:
    """Подсказка по аргументам скила для сообщения-помощи."""
    props = parameters.get("properties", {}) if isinstance(parameters, dict) else {}
    if not props:
        return "Аргументы не требуются — просто вызовите команду."
    parts = []
    for prop_name, spec in props.items():
        ptype = spec.get("type", "any") if isinstance(spec, dict) else "any"
        parts.append(f"{prop_name} ({ptype})")
    return "Аргументы: " + ", ".join(parts) + "\nМожно JSON: /команда {\"prop\": value}"


async def handle_skill_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Динамический обработчик: /<имя_скила> [аргументы].
    Аргументы — JSON-объект, либо одно значение для скила с единственным параметром."""
    raw = ((update.message.text if update.message else None) or "").strip()
    if not raw:
        return
    body = raw[1:] if raw.startswith("/") else raw  # срезаем "/"
    # "calculate@MyBot 2+2" -> ("calculate", " 2+2"); "@MyBot" выбираем отдельно
    head, sep, tail = body.partition(" ")
    command = head.split("@")[0].lower()
    mention = head.split("@")[1].lower() if "@" in head else None
    rest = tail if sep else ""

    # команда адресована другому боту — молча пропускаем
    if mention:
        me = await context.bot.get_me()
        if mention != (me.username or "").lower():
            return

    # находим скил по имени команды
    matched = [n for n in skill_names() if skill_command_name(n) == command]
    message = update.message
    if message is None:
        return
    if not matched:
        # в группах молчим (не мешаем чужим командам), в личке — подсказываем
        if message.chat.type == ChatType.PRIVATE:
            await message.reply_text(f"Скил для команды /{command} не найден. /skills — список")
        return
    name = matched[0]
    rest = rest.strip()

    meta = (SKILLS.get(name) or _PY_SKILL_FILES.get(name)
            or {"kind": "prompt",
                "parameters": {"type": "object",
                               "properties": {"task": {"type": "string"}},
                               "required": ["task"],
                               "additionalProperties": False}})
    props = meta["parameters"].get("properties", {}) if isinstance(meta["parameters"], dict) else {}
    args: dict = {}

    if rest:
        stripped = rest.strip()
        if stripped.startswith("{"):
            try:
                args = json.loads(stripped)
                if not isinstance(args, dict):
                    args = {}
            except json.JSONDecodeError:
                await message.reply_text(
                    f"Не удалось разобрать JSON:\n<code>{html.escape(stripped[:500])}</code>\n\n"
                    f"{_skill_param_hint(meta['parameters'])}",
                    parse_mode=ParseMode.HTML,
                )
                return
        elif len(props) == 1:
            # единственный параметр — считаем введённый текст его значением
            only_key = next(iter(props))
            ptype = props[only_key].get("type", "string") if isinstance(props[only_key], dict) else "string"
            if ptype in ("integer", "number"):
                try:
                    args = {only_key: float(stripped) if ptype == "number" else int(stripped)}
                except ValueError:
                    args = {only_key: stripped}  # пусть скил сам обработает
            else:
                args = {only_key: stripped}
        else:
            # несколько параметров и не JSON — покажем подсказку
            await message.reply_text(
                f"{_skill_param_hint(meta['parameters'])}\n\nПример JSON: /{command} "
                + json.dumps({k: "..." for k in props}, ensure_ascii=False)
            )
            return

    if not args and props:
        await message.reply_text(
            f"🛠 <b>{html.escape(name)}</b>\n{_skill_param_hint(meta['parameters'])}\n\n"
            f"Пример: /{command} " + json.dumps({k: "..." for k in props}, ensure_ascii=False),
            parse_mode=ParseMode.HTML,
        )
        return

    status = await message.reply_text(f"🛠 Выполняю скил <code>{html.escape(name)}</code>…",
                                      parse_mode=ParseMode.HTML)
    args_json = json.dumps(args, ensure_ascii=False)
    if meta.get("kind") == "prompt":
        # prompt-скил: инструкция уходит в модель, пользователю — готовый ответ
        with suppress(Exception):
            await status.delete()
        sent = await run_skill_via_ai(update, context, name, args_json)
        if not sent:
            with suppress(Exception):
                await status.edit_text("😔 Не удалось выполнить скил.")
        return

    result = await asyncio.to_thread(invoke_skill, name, args_json)
    text = result if len(result) <= 3500 else result[:3500] + "…(обрезано)"
    with suppress(Exception):
        await status.delete()
    await send_long_message(message, f"🛠 <b>{html.escape(name)}</b>\n{html.escape(text)}",
                            parse_mode=ParseMode.HTML)


def compact_skill_prompts(chat_data: dict, keep_last: bool = False) -> int:
    """Сжимает сохранённые промпты скилов в истории. Полная инструкция (8-24 КБ)
    уже отработала к моменту ответа — в истории она не нужна и при малом бюджете
    контекста выталкивала ВСЮ предыдущую беседу (бот «забывал» пользователя).
    keep_last=True — не трогать последний промпт (это промпт текущего вызова,
    модель должна получить полную инструкцию).
    Возвращает число сжатых записей."""
    history = chat_data.get("history", [])
    n = 0
    limit = len(history) - 1 if keep_last else len(history)
    for idx in range(limit):
        msg = history[idx]
        content = msg.get("content")
        if msg.get("role") != "user" or not isinstance(content, str):
            continue
        if not content.startswith("[скил ") or len(content) <= 600:
            continue
        m = re.match(r"\[скил ([^\]]+)\]", content)
        name = m.group(1) if m else "скил"
        msg["content"] = (content[:200] +
                          f"\n[…полная инструкция скила '{name}' передана модели, ниже её ответ]").strip()
        n += 1
    return n


async def run_skill_via_ai(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           name: str, args_json: str):
    """Выполняет prompt-скил: строит промпт скилом и пропускает через модель
    (с историей диалога, «печатает…» и ретраями — как в обычном ответе).
    Возвращает True, если ответ отправлен."""
    message = update.message
    chat = update.effective_chat
    if message is None or chat is None:
        return False
    chat_data = store_for(update)
    model = chat_data.get("model", DEFAULT_MODEL)
    epoch = chat_data.get("epoch", 0)  # поколение диалога на момент старта

    prompt = await asyncio.to_thread(invoke_skill, name, args_json)
    if epoch_changed(chat_data, epoch):
        # диалог сбросили, пока скил строился/ждал — не начинаем генерацию
        await message.reply_text(f"↩️ Диалог был сброшен — вызов скила '{name}' отменён.")
        return False
    # промпт + пользовательский запрос идут в историю как обычное сообщение
    remember(chat_data, "user", f"[скил {name}]\n{prompt}")

    typing_task = asyncio.create_task(keep_typing(context, chat.id))
    try:
        # история нужна модели для контекста беседы; свежий промпт добавим последним
        messages = await asyncio.to_thread(build_messages, chat_data, model)
    except Exception:
        logger.exception("Ошибка сборки контекста для скила %s", name)
        history = chat_data.get("history", [])
        if history and history[-1]["role"] == "user":
            history.pop()
        typing_task.cancel()
        return False
    finally:
        typing_task.cancel()
        with suppress(asyncio.CancelledError):
            await typing_task

    typing_task = asyncio.create_task(keep_typing(context, chat.id))
    try:
        reply = await asyncio.to_thread(ai_chat_sync, model, messages)
    except Exception:
        logger.exception("Ошибка модели при выполнении скила %s", name)
        history = chat_data.get("history", [])
        if history and history[-1]["role"] == "user":
            history.pop()
        await message.reply_text("😔 Не удалось выполнить скил — ошибка модели. Попробуйте ещё раз.")
        return False
    finally:
        typing_task.cancel()
        with suppress(asyncio.CancelledError):
            await typing_task

    reply = reply.strip() or "…"
    if epoch_changed(chat_data, epoch):
        # диалог сбросили, пока шла генерация скила — результат не пишем в новую историю
        await message.reply_text("↩️ Диалог был сброшен — ответ скила не добавлен в контекст.")
        return True
    await send_long_message(message, reply, parse_mode=ParseMode.MARKDOWN)
    remember(chat_data, "assistant", reply)

    # сжимаем гигантский промпт скила в истории (иначе он выталкивает контекст)
    compact_skill_prompts(chat_data)
    save_store()
    return True


# ---------------------------------------------------------------------------
# Обработчики сообщений
# ---------------------------------------------------------------------------

def should_reply_in_group(update: Update, bot_username: str, bot_id: int) -> bool:
    """В группах отвечаем на reply к боту и на упоминания @bot_username."""
    message = update.message
    if message is None:
        return False
    if message.chat.type == ChatType.PRIVATE:
        return True
    # reply на сообщение бота
    replied = message.reply_to_message
    if replied and replied.from_user:
        if replied.from_user.id == bot_id:
            return True
    # упоминание @bot в тексте или в подписи
    text = message.text or message.caption or ""
    return f"@{bot_username}" in text


def strip_bot_mention(text: str, bot_username: str) -> str:
    """Убираем @упоминание бота из текста, чтобы не мусорить в промпте."""
    return text.replace(f"@{bot_username}", "").strip()


async def flush_persistence(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Форс-сохранение на диск сразу после изменения истории: без этого данные
    теряются, если процесс убит до плановой записи (Android, kill и т.п.)."""
    try:
        persistence = getattr(context.application, "persistence", None)
        if persistence is not None:
            await persistence.flush()
    except Exception:
        logger.debug("flush persistence failed", exc_info=True)


async def respond_with_ai(update: Update, context: ContextTypes.DEFAULT_TYPE, user_content):
    """Общая логика ответа: история -> запрос к ИИ -> отправка -> обновление истории."""
    message = update.message
    chat = update.effective_chat
    if message is None or chat is None:
        return
    chat_data = store_for(update)
    epoch = chat_data.get("epoch", 0)  # поколение диалога на момент старта
    remember(chat_data, "user", user_content)
    print(f"[HIST] вопрос записан: pid={os.getpid()} chat={chat.id} "
          f"dict={id(chat_data)}", flush=True)

    model = chat_data.get("model", DEFAULT_MODEL)
    # build_messages может дёргать сеть (контекст модели с сервера) — в поток
    messages = await asyncio.to_thread(build_messages, chat_data, model)

    typing_task = asyncio.create_task(keep_typing(context, chat.id))
    try:
        reply = await asyncio.to_thread(ai_chat_sync, model, messages)
    except Exception as e:
        logger.exception("Ошибка при запросе к ИИ")
        # откатываем последнее сообщение пользователя, чтобы не портить историю
        history = chat_data.get("history", [])
        if history and history[-1]["role"] == "user":
            history.pop()
        reason = str(e).strip()
        if len(reason) > 500:
            reason = reason[:500] + "…"
        await message.reply_text(
            "😔 Не удалось получить ответ модели.\n"
            f"Причина: {reason or type(e).__name__}")
        return
    finally:
        typing_task.cancel()
        with suppress(asyncio.CancelledError):
            await typing_task

    if epoch_changed(chat_data, epoch):
        # диалог был сброшен (/reset), пока шла генерация — результат устарел,
        # не пишем его в новую историю (иначе «reset не помогает при активном скиле»)
        await message.reply_text("↩️ Диалог был сброшен — этот ответ не добавлен в контекст.")
        return

    reply = reply.strip() or "…"
    await send_long_message(message, reply, parse_mode=ParseMode.MARKDOWN)

    remember(chat_data, "assistant", reply)
    downgrade_old_images(chat_data)
    print(f"[HIST] ответ записан: pid={os.getpid()} chat={chat.id} "
          f"dict={id(chat_data)} "
          f"сообщений={len(chat_data.get('history', []))}", flush=True)
    save_store()


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    me = await context.bot.get_me()
    username = me.username or ""
    if not should_reply_in_group(update, username, me.id):
        return  # в группе молчим, если нас не звали
    if message is None:
        return

    user_text = strip_bot_mention(message.text or "", username)
    if not user_text:
        await message.reply_text("Напишите что-нибудь 🙂")
        return

    await respond_with_ai(update, context, user_text)


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Фото (в т.ч. пересланное) или документ-изображение — vision-запрос."""
    me = await context.bot.get_me()
    username = me.username or ""
    if not should_reply_in_group(update, username, me.id):
        return

    message = update.message
    if message is None:
        return
    caption = strip_bot_mention(message.caption or "", username) or \
        "Что на этом изображении? Опиши подробно."

    status = await message.reply_text("🖼 Смотрю изображение…")
    try:
        image_bytes = await fetch_image_bytes(update)
    except Exception:
        logger.exception("Не удалось скачать изображение")
        image_bytes = None
    if not image_bytes:
        with suppress(BadRequest, TimedOut):
            await status.edit_text("Не получилось скачать изображение 😕")
        return
    with suppress(BadRequest, TimedOut):
        await status.delete()

    await respond_with_ai(update, context, image_message(caption, image_bytes))


async def handle_other(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Голосовые, видео, стикеры и прочее — вежливо поясняем ограничения."""
    if update.message and update.message.chat.type != ChatType.PRIVATE:
        return
    if update.message:
        await update.message.reply_text(
            "Пока я умею работать только с текстом и изображениями 🙏 "
            "Пришлите текст или картинку."
        )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Централизованный обработчик ошибок python-telegram-bot."""
    logger.error("Необработанная ошибка при обработке апдейта: %s", context.error)
    # сообщаем пользователю, что что-то пошло не так (если это был апдейт с сообщением)
    message = getattr(update, "effective_message", None)
    if message is not None:
        with suppress(Exception):
            await message.reply_text(
                "⚠️ Произошла внутренняя ошибка при обработке. Попробуйте ещё раз."
            )


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

def main():
    if sys.version_info < (3, 10):
        raise SystemExit("Нужен Python 3.10+. В Termux: pkg upgrade -y && pkg install python")

    if is_termux():
        # Android усыпляет фоновые процессы — берём wake lock и подсказываем про батарею
        acquire_wake_lock()
        print("Termux обнаружен: wake lock включён. Рекомендуется также отключить "
              "оптимизацию батареи для Termux (Настройки → Приложения → Батарея).")

    # Загружаем внешние скилы из каталога skills/ (репозитории с GitHub и т.п.)
    load_store()
    ext = load_external_skills()
    _rebuild_catalog()
    print(f"Скилы: {len(_SKILL_CATALOG)} шт. в каталоге "
          f"(ленивая загрузка — код подключается только при вызове)")
    if ext["errors"]:
        print(f"ВНИМАНИЕ: ошибок сканирования скилов: {len(ext['errors'])} (подробности в логе)")

    # Персистентность: chat_data (история, настройки чата) сохраняется
    # в bot_data.pkl (рядом со скриптом) и переживает перезапуски бота.
    # PTB-persistence отключена: история в CHAT_STORE + bot_history.json

    # Отдельные httpx-клиенты: для API и для polling — с увеличенными таймаутами
    # (дефолт PTB — 5 сек, из-за чего падало скачивание изображений)
    api_request = HTTPXRequest(
        connection_pool_size=8,
        read_timeout=TELEGRAM_READ_TIMEOUT,
        write_timeout=TELEGRAM_READ_TIMEOUT,
        connect_timeout=TELEGRAM_READ_TIMEOUT,
    )
    get_updates_request = HTTPXRequest(
        connection_pool_size=1,
        read_timeout=15,  # long polling сам держит соединение открытым
        write_timeout=TELEGRAM_READ_TIMEOUT,
        connect_timeout=TELEGRAM_READ_TIMEOUT,
    )

    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .request(api_request)
        .get_updates_request(get_updates_request)
        .post_init(post_init)
        .build()
    )

    # Команды
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("reset", cmd_reset))
    application.add_handler(CommandHandler("model", cmd_model))
    application.add_handler(CommandHandler("prompt", cmd_prompt))
    application.add_handler(CommandHandler("stats", cmd_stats))
    application.add_handler(CommandHandler("skills", cmd_skills))
    application.add_handler(CommandHandler("run", cmd_run))
    application.add_handler(CommandHandler("reload", cmd_reload))

    # Динамические команды скилов: /<имя_скила> — регистрируется ПОСЛЕ статических
    # CommandHandler, поэтому перехватывает только НЕизвестные статике команды
    # (/start, /help и т.п. уже обработаны выше и сюда не дойдут)
    application.add_handler(MessageHandler(filters.COMMAND, handle_skill_command))

    # Сообщения: изображения (фото и документы-картинки), текст, остальное
    application.add_handler(
        MessageHandler(filters.PHOTO | (filters.Document.IMAGE & ~filters.COMMAND), handle_media)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )
    application.add_handler(
        MessageHandler(~filters.TEXT & ~filters.COMMAND & ~filters.PHOTO, handle_other)
    )

    application.add_error_handler(on_error)

    print(f"Бот запущен: pid={os.getpid()}. Если эта строка появляется "
          f"в логе несколько раз за вечер — бот перезапускается.")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=DROP_PENDING_UPDATES,
    )


if __name__ == "__main__":
    main()