# CLAUDE.md — Claude Code ↔ Telegram Bridge

Guidance for any Claude Code session working in this repo. Read first.

## Что это
Telegram-бот (python-telegram-bot, async) на Windows, через который владелец
удалённо управляет Claude Code с телефона (текст + голос). Бот принимает
сообщение → запускает `claude -p` (headless) в папке проекта → возвращает ответ.
Полная документация для пользователя — в `README.md`.

## Окружение (НЕ угадывать — использовать это)
- **Python 3.12** через `python` / `py` (**НЕ `python3`**). Вендор в `.venv`
  → запускать `.venv\Scripts\python.exe`.
- **Claude Code CLI** ~2.1.183: `C:\Users\Aliaksandr\.local\bin\claude.exe`.
  Модель **`glm-5.1`** (custom), авторизация уже настроена — **НЕ передавать
  `--model` и НЕ трогать auth**.
- **ffmpeg** стоит через winget; `speak._find_ffmpeg` находит его сам.
- **Секреты** в `.env` (в `.gitignore`): `TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_IDS`
  (chat_id владельца), `GROQ_API_KEY` (для STT).
- Настройки без секретов — `config.yaml`.

## Запуск и тесты
- Запуск бота: `run_bot.bat` или `.venv\Scripts\python.exe -u bot.py`.
- Остановка: `stop_bot.bat` (убивает все `python.exe`/`pythonw.exe` с `bot.py` в cmdline — независимо от способа запуска).
- Проверка компиляции/импортов:
  `.venv\Scripts\python.exe -c "import config,commands,claude_runner,projects,messages,security,speak,transcribe,health,bot"`
- Дымовой тест раннера (без Telegram):
  `PYTHONUTF8=1 .venv\Scripts\python.exe claude_runner.py` (переменная `SMOKE=ask|badresume|task`).
- Тест TTS: `.venv\Scripts\python.exe speak.py` (пишет `speak_test.ogg`).
- Логи: `logs\bot.log`. Состояние сессий: `sessions.json`.
- Автозапуск при входе в Windows: `install_autostart.bat` (создаёт задачу
  `ClaudeTelegramBot`, ONLOGON, `/RL LIMITED` — без админа) / `uninstall_autostart.bat`.
  Задача гонит `run_autostart.vbs` → `pythonw` со env `BOT_START_PAUSED=1`
  (бот поднимается **на паузе** до `/resume`, без окна). Ручной `run_bot.bat` — активным.

## Архитектура
| Файл | Ответственность |
|---|---|
| `bot.py` | точка входа: логирование, `Application.builder().post_init(...)` (стартовое сообщение), регистрация хендлеров (включая `CallbackQueryHandler` для `/note`), обработчик ошибок, `run_polling(allowed_updates=["message","callback_query"], drop_pending_updates=True)` |
| `config.py` | загрузка `.env` + `config.yaml`, дата-классы `Settings`/`Project`, маппинг режим→флаги |
| `claude_runner.py` | **ядро**: async-обёртка над `claude -p` (argv, stdin, защитный разбор JSON, таймаут, kill дерева); всегда передаёт `--append-system-prompt` (`BRIDGE_SYSTEM_PROMPT`) — контекст «ты headless за Telegram-мостом», без него сессия не знает о своём развёртывании и конфабулирует («я не в Telegram», «показано выше») |
| `projects.py` | состояние: текущий проект на чат, сессии (`sessions.json`), **глобальный лок**, `/cancel`, режимы, voice-режим, пауза (`/pause`/`/resume`; старт-на-паузе через env `BOT_START_PAUSED`), **режим диктовки** (`note_mode`/`note_folder`/`note_browse` + `BrowseCache`) |
| `commands.py` | обработчики команд (`/ask /task /new /diff /git /project /mode /cancel /status /speak /voice /pause /resume /note`) + голосовой ввод/вывод + диктовка (`cmd_note`, `_run_dictation`, inline-навигация `note_callback`) |
| `transcribe.py` | STT: Groq (по умолч.) / local faster-whisper (lazy) |
| `speak.py` | TTS: Edge (по умолч.) / Silero (lazy) → OGG/Opus через ffmpeg |
| `messages.py` | чанкование (лимит 4096), `.txt` для длинных, footer |
| `health.py` | проверки связи с серверами (Telegram, Claude API via `ANTHROPIC_BASE_URL`, Groq, Edge TTS) — в стартовом сообщении и `/status`; токен redact-ится |
| `security.py` | allowlist по `chat_id` (декоратор `@authorized`) |

Поток: Telegram → хендлер → `claude_runner.run_claude()` (в воркер-потоке) →
разбор JSON → `messages.reply_long()` → ответ. Голосовой ввод: `cmd_voice`
качает ogg → `transcribe.transcribe()` → как задача. Голосовой вывод:
`_speak_answer()` → Edge TTS → ffmpeg → `send_voice`.

**Пауза** (`/pause`/`/resume`, `State.paused` — per-chat, в памяти): пока чат на
паузе, свободный текст и голосовые **молча игнорируются** (ранний `return` в
`cmd_freetext`/`cmd_voice`), а `/ask`/`/task` отвечают «⏸ на паузе» (гейт в
`_do_claude` **до** захвата `claude_lock` — идущий запрос дорабатывает, новые нет).
Старт-на-паузе задаётся env `BOT_START_PAUSED=1` (ставит только `run_autostart.vbs`);
ручной `run_bot.bat` стартует активным. После рестарта — всегда «возобновлено».

**Диктовка** (`/note`, `State.note_mode`/`note_folder`/`note_browse` — per-chat, в
памяти): голосовое → `STT.transcribe()` → **дописывается в**
`dictations/<folder>/ГГГГ-ММ-ДД.md`, **Claude НЕ вызывается**. В `cmd_voice` ветка
`note_mode` стоит **до** гейта паузы — диктовка работает и на паузе Claude.
`/note on|off` (toggle) · `/note folder <имя>` (папка-категория; sanitize в
`_safe_folder`, защита от traversal в `_dictation_dir`) · `/note browse` (чтение
через inline-кнопки: `CallbackQueryHandler`, callback_data `nb:<op>[:i]` по
**индексам** в `BrowseCache`, не по именам — обходит лимит 64 б и подделку пути).
Файлы в `dictations/` (в `.gitignore`).

**Старт бота**: `post_init` шлёт владельцу статус + список команд +
`health.check_all()` (Telegram / Claude API via `ANTHROPIC_BASE_URL` / Groq / Edge).
`drop_pending_updates=True` — накопленные пока бот лежал апдейты **сбрасываются**
(иначе каскад запросов к Claude → «typing»-шторм + ошибки при недоступном API).

## ЖЁСТКИЕ ограничения (не нарушать)
1. **Глобальный лок на ВСЕ вызовы `claude`.** Конкурентные `claude -p` калечат
   глобальный `~/.claude.json` (на этой машине найдено 8 повреждённых копий).
   `State.claude_lock` сериализует и `/ask`, и `/task`. **Не вводить** параллелизм
   по проектам и не запускать `claude` concurrently — иначе конфиг ломается.
2. **`subprocess.Popen`, НЕ `asyncio.create_subprocess_exec`.** На Windows Proactor
   loop rejects `encoding=`. Раннер использует `Popen(text=True, encoding="utf-8")`
   внутри `run_in_executor`. UTF-8 обязателен (кириллица).
3. **Промпт — через stdin**, не argv (`communicate(input=prompt)`). Защита от
   квотирования и лимита длины командной строки Windows для длинных русских текстов.
4. **Защитный разбор JSON.** Битый/чужой `--resume <uuid>` возвращает plain text с
   exit 0 → `json.loads` в `try/except`, иначе краш.
5. **`--max-budget-usd` — единственный реальный лимит** (всегда передаётся).
   `--max-turns` в 2.1.183 **не гарантируется** — не полагаться.
6. **`/ask` всегда read-only** (`--permission-mode plan` + disallow Edit/Write)
   независимо от текущего режима.
7. cwd = `project.path` и **должен быть стабилен** — сессии Claude привязаны к cwd.
8. **`allowed_updates` обязано включать `"callback_query"`.** Иначе inline-кнопки
   (`/note browse`) **молча** не работают — Telegram просто не доставляет нажатия,
   в логе чисто, ошибок нет. `edited_message` намеренно исключён (правки команд не
   перезапускаются; `update.message is None` для edits).
9. **`<дата>.md` в тексте сообщения = битая ссылка.** `.md` — реальный TLD (Молдова),
   Telegram делает `2026-06-21.md` кликабельным → тап открывает браузер →
   `DNS_PROBE_FINISHED_NXDOMAIN`. Показывать дату через `path.stem` (без `.md`).
   Имена файлов на диске, текст кнопок, одиночное `.md` без домена — безопасны.
10. **У `CallbackQuery` нет атрибута `.bot`** (в этой версии python-telegram-bot) —
    брать `context.bot` и пробрасывать в функции навигации. `query.edit_message_text`
    и `query.answer` — есть.

## Конвенции работы
- **НЕ коммитить без явной просьбы пользователя.** Сначала создать ветку, затем
  спросить подтверждение на коммит. (Пользователь настоял; однажды заставил
  откатить самовольные коммиты.) Сейчас в репо **нет коммитов**.
- Новые STT/TTS-провайдеры делать **pluggable** (переключаются в `config.yaml`)
  и **lazy-import** (опциональные тяжёлые зависимости не должны ломать запуск).
- Секреты — только в `.env`, никогда не логировать токен.
- Сетевые сбои бота — норма (слабый интернет у владельца); polling
  авто-повторяется, обработчик ошибок пишет кратко.
