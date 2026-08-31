# 🤖 AI Telegram Agent

Полноценный ИИ-чат-агент для Telegram в **трёх файлах Python**. Работает на
Android (Termux) и любом сервере, подключается к любому OpenAI-совместимому API.

## ✨ Возможности

- **Диалоговая память** — история отдельно для каждого чата, переживает
  перезапуски (`bot_history.json`, атомарная запись после каждого сообщения);
- **Скилы с ленивой загрузкой** — при старте код скилов не исполняется:
  каталог собирается AST-сканом, модель видит только список имён, код
  подключается в момент вызова. Поддерживаются `.py`, пакеты с `__init__.py`
  и markdown-скилы `SKILL.md`;
- **Vision** — фото и картинки-документы (можно с подписью-вопросом);
- **Группы** — отвечает на reply и упоминания `@username`;
- **Универсальный клиент** (`openai_compat.py`, без SDK) — стриминг, ретраи
  с backoff, разбор ответов OpenAI / Anthropic / Gemini / SSE / NDJSON;
- **Причина ошибки — прямо в чат**, без изучения логов.

## 📦 Установка

```bash
# Termux (Android) или любой Linux с Python 3.10+
pkg install -y python                 # только для Termux
pip install -U python-telegram-bot httpx certifi

git clone https://github.com/ВАШ_ЛОГИН/ai-telegram-agent.git
cd ai-telegram-agent

export TELEGRAM_TOKEN="123456:ABC..."           # токен от @BotFather
export AI_BASE_URL="https://api.example.com/v1" # OpenAI-совместимый шлюз
export AI_API_KEY="sk-..."
export AI_MODEL="имя-модели"

python tgbot.py
```

Данные хранятся рядом со скриптом: `bot_history.json` (история чатов),
`bot.log` (лог при фоновом запуске).

## 🛠 Скилы

Модель получает каталог скилов (имя + описание) и один инструмент
`use_skill(name, args)`. Код импортируется только при вызове — хоть 100 скилов
не утяжеляют ни старт, ни контекст.

Скилы кладутся в папку `skills/` рядом с `tgbot.py` (создаётся автоматически):

```bash
mkdir -p skills && cp example_skill.py skills/
```

`example_skill.py` — готовый шаблон:

```python
from tgbot import skill

@skill(
    "echo",
    "Повторяет текст обратно.",
    {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
)
def echo(args: dict) -> str:
    return str(args.get("text", ""))
```

Встроенные скилы: `current_time`, `calculate`, `random_number`, `web_fetch`.
Markdown-скил — папка `skills/имя/SKILL.md` с front-matter `name/description`.
После добавления файлов: `/reload` (или перезапуск бота).

## 📋 Команды

| Команда | Действие |
|---|---|
| `/start`, `/help` | знакомство и справка |
| `/reset` | очистить историю диалога |
| `/model [имя]` | показать/сменить модель |
| `/prompt [текст]` | посмотреть/задать системный промпт |
| `/stats` | статистика: история, контекст модели, бюджет |
| `/skills` | список скилов с источниками |
| `/run скил JSON` | вызвать скил вручную |
| `/reload` | перечитать `skills/` без перезапуска |
| `/имя_скила` | быстрый вызов любого скила командой |

## ⚙️ Конфигурация (переменные окружения)

| Переменная | По умолчанию | Описание |
|---|---|---|
| `TELEGRAM_TOKEN` | — | токен бота (обязателен) |
| `AI_BASE_URL` | `http://localhost:20128/v1` | адрес OpenAI-совместимого API |
| `AI_API_KEY` | — | ключ API |
| `AI_MODEL` | — | модель по умолчанию |
| `AI_TEMPERATURE` | `0.7` | температура генерации |
| `HISTORY_MAX_MESSAGES` | `40` | сообщений в отправляемом контексте |
| `MAX_TOOL_ITERATIONS` | `6` | макс. цепочек вызова скилов |
| `SKILLS_DIR` | `./skills` | каталог скилов |
| `AI_DISABLE_TOOLS` | `0` | `1` — отключить скилы (диагностика) |
| `AI_DEBUG_RAW` | `0` | `1` — дампить сырые ответы шлюза в лог |

Длина контекста модели запрашивается у шлюза (`/v1/models`) и кэшируется —
бюджет истории считается в токенах, если шлюз её сообщил.

## 🔍 Диагностика

- Бот прислал «**Причина: …**» — это точный текст ошибки модели/шлюза;
- `AI_DEBUG_RAW=1 python tgbot.py` — сырые ответы шлюза в лог;
- `AI_DISABLE_TOOLS=1` — проверить чат без инструментов;
- Termux: wake lock берётся автоматически; также отключите оптимизацию батареи
  для Termux. На Android 12+ учтите «phantom process killer».

## 🔒 Безопасность

- Токены задавайте только через переменные окружения;
- Скилы — это исполняемый Python-код: кладите в `skills/` только проверенные
  файлы; файловый доступ скилов ограничен каталогом `skills/`.

## 📄 Лицензия

MIT — см. [LICENSE](LICENSE).
