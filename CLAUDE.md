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
- **Claude Code CLI** (интерактивный + `runner: subprocess`):
  `C:\Users\Aliaksandr\.local\bin\claude.exe`, канал latest — сам обновляется
  (сейчас 2.1.272). Для `runner: sdk` — СВОЙ CLI из pip-пакета
  `claude-agent-sdk`==0.2.152 (бандл 2.1.259:
  `.venv\Lib\site-packages\claude_agent_sdk\_bundled\claude.exe`).
  Модель **`glm-5.3`** (custom, из юзер-окружения `ANTHROPIC_MODEL`),
  авторизация уже настроена — **НЕ передавать
  `--model` и НЕ трогать auth** (SDK наследует `ANTHROPIC_BASE_URL`/токен из
  окружения сам — ему ничего передавать не надо).
- **ffmpeg** стоит через winget; `speak._find_ffmpeg` находит его сам.
- **Секреты** в `.env` (в `.gitignore`): `TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_IDS`
  (chat_id владельца), `GROQ_API_KEY` (для STT).
- Настройки без секретов — `config.yaml`.

## Запуск и тесты
- Запуск бота: `run_bot.bat` или `.venv\Scripts\python.exe -u bot.py`.
- Остановка: `stop_bot.bat` (убивает все `python.exe`/`pythonw.exe` с `bot.py` в cmdline — независимо от способа запуска; плюс **фильтрованно** осиротевший бандл-CLI раннера sdk: только процессы с `claude_agent_sdk\_bundled` в cmdline — интерактивный `claude.exe` владельца НЕ трогает).
- Проверка компиляции/импортов:
  `.venv\Scripts\python.exe -c "import config,commands,claude_runner,sdk_runner,projects,messages,security,speak,transcribe,health,bot"`
- Дымовой тест раннера (без Telegram):
  `PYTHONUTF8=1 .venv\Scripts\python.exe claude_runner.py` (переменная `SMOKE=ask|badresume|task`).
  SDK-раннер: `PYTHONUTF8=1 .venv\Scripts\python.exe sdk_runner.py`
  (`SMOKE=ask|task|deny|interrupt|badresume|errpaths`; спавнит живой claude —
  сначала убедиться, что бот НЕ запущен).
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
| `claude_runner.py` | **ядро (subprocess)**: async-обёртка над `claude -p` (argv, stdin, защитный разбор JSON, таймаут, kill дерева); всегда передаёт `--append-system-prompt` (`BRIDGE_SYSTEM_PROMPT`) — контекст «ты headless за Telegram-мостом», без него сессия не знает о своём развёртывании и конфабулирует («я не в Telegram», «показано выше»). `ClaudeResult` и `BRIDGE_SYSTEM_PROMPT` переиспользуются sdk-раннером |
| `sdk_runner.py` | **альтернативное ядро (sdk)**: живой `ClaudeSDKClient` на проект через pip-пакет `claude-agent-sdk` (lazy-import — бот стартует и без пакета). `can_use_tool` → ✅/❌ кнопки, `interrupt()` → настоящий `/cancel`, `set_permission_mode()` на лету (`/ask` всегда plan, `/mode` — режим хода). Инвариант «не двух живых claude.exe»: закрыть ПЕРЕД новым connect; реестр держит клиента только текущего проекта; транспортная ошибка → drop → ленивый reconnect с `resume`. `setting_sources=["user"]` — режет проектные allow-правила (иначе авто-апрув ДО кнопок; режет и проектный CLAUDE.md — принятый трейд). ⚠️ НО user-уровень `~/.claude/settings.json` при этом ПОДКЛЮЧЁН: allow-правила оттуда сработали бы ДО кнопок — сейчас он пуст; если когда-то заполнишь его allow-правилами, кнопки для тех инструментов молча перестанут появляться (убрать `"user"` из `setting_sources` или чистить файл) |
| `projects.py` | состояние: текущий проект на чат, сессии (`sessions.json`), **глобальный лок**, `/cancel` (`RunningTask`: proc для subprocess, `active`+`cancel_hook` для sdk), режимы, voice-режим, пауза (`/pause`/`/resume`; старт-на-паузе через env `BOT_START_PAUSED`), **режим диктовки** (`note_mode`/`note_folder`/`note_browse` + `BrowseCache`), **очередь сообщений** (`task_queue`, sdk) и **реестр ✅/❌-промптов** (`perm_prompts`, монотонные id) |
| `commands.py` | обработчики команд (`/ask /task /new /diff /git /project /mode /cancel /status /speak /voice /pause /resume /note`) + голосовой ввод/вывод + диктовка (`cmd_note`, `_run_dictation`, inline-навигация `note_callback`) + **✅/❌-кнопки прав sdk** (`_TelegramPermUI`, `_perm_callback` `pm:<op>:<id>[:<opt>]`) + очередь (`_dispatch_queue`) |
| `transcribe.py` | STT: Groq (по умолч.) / local faster-whisper (lazy) |
| `speak.py` | TTS: Edge (по умолч.) / Silero (lazy) → OGG/Opus через ffmpeg |
| `messages.py` | чанкование (лимит 4096), `.txt` для длинных, footer |
| `health.py` | проверки связи с серверами (Telegram, Claude API via `ANTHROPIC_BASE_URL`, Groq, Edge TTS) — в стартовом сообщении и `/status`; токен redact-ится |
| `security.py` | allowlist по `chat_id` (декоратор `@authorized`) |

Поток: Telegram → хендлер → (по `config.yaml: runner`) `claude_runner.run_claude()`
в воркер-потоке **или** `sdk_runner.run_turn()` на живом клиенте → `ClaudeResult` →
`messages.reply_long()` → ответ. Голосовой ввод: `cmd_voice` качает ogg →
`transcribe.transcribe()` → как задача. Голосовой вывод: `_speak_answer()` →
Edge TTS → ffmpeg → `send_voice`.

**Второй бот: MT5-монитор (`mt5bot\`).** Независимый процесс со своим токеном
`MT5_BOT_TOKEN` (guard: пустой или равный `TELEGRAM_BOT_TOKEN` → отказ старта;
mutex + incumbent-guard как у claude-бота), наблюдает за терминалом MetaTrader 5
(bossaFX): события placement/fill/close+reason/margin + `/mt5status|/mt5orders|
/mt5positions` (полные токены — тап в Telegram шлёт команду целиком; аргумент
после пробела в автоссылку не попадает; меню команд — `set_my_commands` в
`post_init`; короткая форма `/mt5 <sub>` тоже работает). Кнопки — двумя
видами: (а) **reply-клавиатура** внизу чата (`ReplyKeyboardMarkup`,
`is_persistent` — ставится уже стартовым сообщением `post_init`, тап шлёт текст
кнопки → `MessageHandler` по ТОЧНОМУ лейблу, `filters.Regex` с `^…$`); (б)
голый `/mt5` — **inline-пульт**: тап рендерит ответ **в том же сообщении**
(`edit_message_text` с `reply_markup`; грабли: `CallbackQuery` без `.bot` →
`context.bot`, «message is not modified» = норма, не ошибка). Списки — с
inline-кнопкой **на каждый элемент**: тап → карточка с действием →
подтверждение ✅/❌ → исполнение (callback-цепочка `m5pos:/m5ord:<ticket>` →
`m5cl:/m5dl:` → `m5cly:/m5dly:`; все тапы — один `m5_callback`, pattern `^m5`).
Торговые методы (`close_position`/`delete_pending`) — за ДВОЙНЫМ гейтом:
`allow_trading` (yaml, пока false; в карточке элемента кнопки действий при
выключенном флаге НЕ показываются) + `terminal_info().trade_allowed` (внятный
10027), filling перебирается при 10030. Уведомления — через asyncio-очередь
с пампом ретраев (сбой отправки НЕ теряет событие: штампы сделок/защёлки
margin двигаются при детекте). Фаза 1 = чтение + действия над
существующими объектами;
план и фазы —
`..\_BrokerPolski\_BOSSA\_MT5\Plan_TelegramBridgeMT5.md`. Инварианты: живёт
целиком в `mt5bot\` (код + свои bats + `logs\mt5bot.log`), из корня берёт только
`security.py`/`messages.py` (sys.path-шим в `mt5_bot.py`); НЕ импортирует
claude_runner/sdk_runner/projects — ни claude.exe, ни локов, ни `~/.claude.json`.
Все вызовы `MetaTrader5` — синхронное IPC → только `asyncio.to_thread` под
`threading.Lock` (не блокировать event-loop). Текст в чат — plain. Stop-фильтры
двух ботов строго взаимоисключающие (`\bbot\.py\b` / `mt5_bot\.py` — имя
`mt5_bot.py` СОДЕРЖИТ подстроку `bot.py`). Дифф событий — только после baseline
(старт/реконнект = снимок без уведомлений, иначе шторм). Терминал обязан быть
на той же машине (IPC-attach); пакет `MetaTrader5` в requirements (Windows-only).
**`mt5.initialize(path=...)` ЗАПУСКАЕТ закрытый терминал** (с живыми EA!) —
поэтому `_attach` сначала гейтит по процессу (`_running_image_paths`,
Toolhelp32): attach только к уже работающему, закрытый — ждать, не запускать
(гейт обязателен и при ПУСТОМ terminal_path — тогда по любому запущенному
terminal64.exe, т.к. initialize без пути тоже умеет запускать).
Смоук без Telegram: `PYTHONUTF8=1 SMOKE=attach python mt5bot\mt5_bot.py`.

**SDK-раннер** (`runner: sdk`): один живой `ClaudeSDKClient` на проект,
поднимается лениво и живёт между ходами (контекст — в сессии, не в `--resume` на
каждое сообщение). Нужные настройки: `runner`, `permission_timeout_minutes`
(сколько ждать тапа ✅/❌ до авто-отклонения), `task_queue_max` (очередь на
проект, пока идёт ход). Права: всё вне `READ_ONLY_ALLOW` (Read/Glob/Grep/TodoWrite)
→ сообщение с кнопками; `/ask` и strict откатывают запись ещё ДО кнопок
(авто-deny). `AskUserQuestion` → кнопки-варианты (≤4, без multiSelect; иначе
deny «продолжай без уточнений»). Очередь: сообщения, пришедшие во время хода,
паркуются (`_dispatch_queue` раздаёт по одному после хода; `/resume`
перезапускает раздачу; `/cancel` чистит). /new и смена проекта закрывают клиент
(сессия в `sessions.json` выживает → ленивый resume), но **запрещены, пока идёт
ход** (гейт `_turn_in_progress_reply` в `_do_new_dialog`/`_switch_project`/
`project_callback`/`cmd_project`): дроп клиента под ногами `_drive` убил бы ход,
а завершившийся ход всё равно вернул бы свой session_id, отменив `/new`; честный
отказ «⏳ идёт запрос — /cancel или дождитесь» виден сразу после команды. То же
в `/cancel` в окне подключения sdk (~60 c, до регистрации хука): ход уже идёт,
но прервать его нельзя — бот так и отвечает («повторите через несколько
секунд»), а не «ничего не выполняется». `runner` через `/config` пишется
**только в yaml** (`_RESTART_ONLY_FIELDS`): живой setattr менял бы семантику
ходов/очереди под работающим ботом; применяется рестартом, `/status` показывает
фактический раннер процесса. Бюджет на живом клиенте —
**на процесс-сессию, не на запрос**: при `error_max_budget_usd` раннер роняет
клиента, следующий ход получает свежий бюджет (см. ограничение 5). Ловушка
interrupt: `/cancel` дренить поток **нельзя** — ридер хода (`_drive`) ещё жив и
сам съедает aborted-ResultMessage; второй читатель в хуке УКРАДЁТ терминальное
сообщение, и ход зависнет до таймаута. Дренить можно только когда ридер уже
мёртв (таймаут/отмена задачи) — см. `_interrupt_turn` vs `_abort`.

**Пауза** (`/pause`/`/resume`, `State.paused` — per-chat, в памяти): пока чат на
паузе, свободный текст и голосовые **молча игнорируются** (ранний `return` в
`cmd_freetext`/`cmd_voice`; исключение — ветка диктовки `note_mode`, она стоит
до гейта и на паузе пишет в файл), а `/ask`/`/task` отвечают «⏸ на паузе» (гейт в
`_do_claude` **до** захвата `claude_lock` — идущий запрос дорабатывает, новые нет).
Старт-на-паузе задаётся env `BOT_START_PAUSED=1` (ставит только `run_autostart.vbs`);
ручной `run_bot.bat` стартует активным. После рестарта — всегда «возобновлено».

**Диктовка** (`/note`, `State.note_mode`/`note_folder`/`note_browse` — per-chat, в
памяти): голосовое → `STT.transcribe()` → **дописывается в**
`dictations/<folder>/ГГГГ-ММ-ДД.md`, **Claude НЕ вызывается**; набранный текст —
туда же (ветка в `cmd_freetext`, до гейта паузы). В обоих хендлерах ветка
`note_mode` стоит **до** гейта паузы — диктовка работает и на паузе Claude.
Сохранение+подтверждение — общий хелпер `_save_note` (голос редактирует статус
«распознаю…», текст шлёт новое сообщение).
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
   Касается и sdk-раннера: лок кроет и живой `ClaudeSDKClient` (ходы по одному),
   плюс инвариант «закрыть ПЕРЕД новым connect» — никогда два живых claude.exe.
2. **`subprocess.Popen`, НЕ `asyncio.create_subprocess_exec`.** На Windows Proactor
   loop rejects `encoding=`. Раннер использует `Popen(text=True, encoding="utf-8")`
   внутри `run_in_executor`. UTF-8 обязателен (кириллица). (Касается НАШЕГО кода:
   SDK внутри себя спавнит CLI через собственный anyio/asyncio-subprocess с
   байтовыми пайпами — этой причине там нечему срабатывать, правилом не
   ограничивается.)
3. **Промпт — через stdin**, не argv (`communicate(input=prompt)`). Защита от
   квотирования и лимита длины командной строки Windows для длинных русских текстов.
4. **Защитный разбор JSON.** Битый/чужой `--resume <uuid>` возвращает plain text с
   exit 0 → `json.loads` в `try/except`, иначе краш. Плюс: `ANTHROPIC_LOG`
   вычищается из окружения ребёнка (`debug` заставляет SDK писать HTTP-дампы в
   stdout и топит JSON-ответ), при неудачном разборе берётся хвост вывода
   (причина в конце), JSON при мусоре в stdout ищется с конца.
5. **`--max-budget-usd` — единственный реальный лимит** (всегда передаётся).
   `--max-turns` **не гарантируется** — не полагаться. На sdk-клиенте бюджет —
   на процесс-сессию (не на запрос): `error_max_budget_usd` → drop клиента →
   следующий ход reconnect с свежим бюджетом; `total_cost_usd` в ResultMessage
   кумулятивный по сессии (проверено смоуком SMOKE=task).
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
11. **✅/❌-кнопки прав (sdk): callback_data `pm:<op>:<id>[:<opt>]` с монотонным id**
    из реестра `State.perm_prompts` — НЕ индексом списка (параллельные tool_use
    сдвинули бы индекс, чужой тап разрешил бы чужой вызов). Текст промпта —
    plain (динамика ломает Markdown-парсер) и с ZWSP после `.md` (ловушка №9).
    /ask и strict: авто-deny всего вне `READ_ONLY_ALLOW` ДО показа кнопок —
    случайный тап не может разрешить запись в read-only ходе. Любой сбой UI
    (send упал, таймаут) = deny с объяснением модели, не краш хода.

## Конвенции работы
- **НЕ коммитить без явной просьбы пользователя.** Сначала создать ветку, затем
  спросить подтверждение на коммит. (Пользователь настоял; однажды заставил
  откатить самовольные коммиты.)
- **Каждое изменение бота — записью в `WHATS_NEW.md`** (новое сверху, дата +
  суть для владельца, по-русски), тем же коммитом, что и изменение.
- Новые STT/TTS-провайдеры делать **pluggable** (переключаются в `config.yaml`)
  и **lazy-import** (опциональные тяжёлые зависимости не должны ломать запуск).
- Секреты — только в `.env`, никогда не логировать токен.
- Сетевые сбои бота — норма (слабый интернет у владельца); polling
  авто-повторяется, обработчик ошибок пишет кратко.
