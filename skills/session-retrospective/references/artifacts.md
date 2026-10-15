# Команды и артефакты v1

Helper: `python3 ~/.codex/skills/session-retrospective/scripts/retrospective.py COMMAND`.
Реальный CLI всегда использует `~/.codex/skills` и `~/.local/state/codex-dialogue-kit/retrospective`; альтернативные корни доступны только Python API для synthetic tests. Запускай helper без `-O`/подмены кода. Он не содержит inference, scheduler, subprocess или сети.

Команды:

- `baseline` — снимки всех разрешённых Markdown, без изменения навыков.
- `snapshot dialogue-explore/SKILL.md` — хеш/версия и адрес content-addressed backup.
- `scan --baseline --exclude-thread CURRENT_ID [--exclude-thread CHILD_ID]` — первая нулевая точка метаданных; затем обычный `scan --exclude-thread …`.
- `prepare dialogue-explore/SKILL.md /private/path/candidate.md /private/path/analysis.json` — ID кандидата; записывает неизменяемый по хешам baseline и candidate.
- `apply CANDIDATE_ID /private/path/validation.json /private/path/review.json` — заменяет один файл после gates; временный pending статус.
- `verify-applied CANDIDATE_ID /private/path/post.json` — подтверждает установленный smoke либо откатывает при отказе/невалидном артефакте.
- `rollback CANDIDATE_ID` — откат; поздние edits запрещают перезапись.
- `acknowledge SCAN_ID OUTCOME [--candidate CANDIDATE_ID]` — после фактического разбора фиксирует reviewed IDs/cursors, создаёт reports/SCAN_ID.json. OUTCOME: no-change, applied, rejected, rolled-back. Для applied/rolled-back ID обязателен; manifest должен подтверждать итог (pending apply не подходит).

Все JSON ниже заполняются реальными фактами. Не копируй фиктивные test fixtures в рабочее состояние. Артефакты строгие: дополнительные поля запрещены. Пароли и токены не должны попадать даже в входные JSON. Secret-pattern gate — дополнительная эвристика, не полный DLP. Source JSONL read-only; scanner никогда не копирует payload в manifest.

`analysis.json`:

- `analyst_id`: фактический actor ID рантайма: native UUID/simple ID либо canonical task_name `/root/<name>` (включая вложенные пути). Это только JSON-значение, не файловый путь; не придумывай UUID, если runtime возвращает task_name. Используй один последовательный способ идентификации агента во всех артефактах.
- `reason`, `expected_effect`, `comparison_criteria`: краткие санитизированные строки 8–1200 символов.
- `evidence`: 1–6 объектов с `session_id`, `turn_id`, `events: [start, end]`, `summary`, `kind: episode|current-user-correction`. В v1 events — byte offsets исходного JSONL, как в scan manifest. Reviewer сверяет с источником. Две разные пары IDs — только машинный минимум; смысловую независимость проверяет reviewer. Current-user-correction требует живой текущей поправки, архив не подходит.

`validation.json`:

- `candidate_sha256`, `baseline_sha256`: из prepare manifest.
- `runner_id`: ID исполнителя поведенческих проб в том же допустимом actor-формате.
- `syntax_passed`: true только после фактического quick_validate изменённого skill (на изолированной копии всего skill и нужных соседних references).
- `cases`: 6–20 объектов. Каждый содержит `control`, `expected`, `baseline_observed`, `candidate_observed`, `baseline_passed`, `candidate_passed`. Наблюдения — краткие пересказы действий, не исходные промпты. Все candidate_passed должны быть true. Для relevant-improvement baseline_passed false и candidate true; улучшение без наблюдаемой разницы не применяется.
- Обязательные control: `factual-direct`, `authorized-execution`, `no-recursive-delegation`, `privacy`, `authority`, `relevant-improvement`.

`review.json`:

- `reviewer_id`: другой native агент, не analyst и не runner; UUID/simple ID или canonical task_name.
- `analyst_id`, `candidate_sha256`: точное совпадение.
- `verdict: accept|reject`, `independent: true` только по факту.
- `reason`: проверенное основание.
- `boundaries_preserved`: объект с boolean `permissions`, `root-only-delegation`, `privacy`, `scope`; все true нужны для apply.

`post.json`:

- `candidate_sha256`, `runner_id`: реальная установленная версия и исполнитель smoke.
- `passed`: итог native smoke после apply.
- `reason`: наблюдавшееся поведение/провал без секретов. Ложь или невалидный/отсутствующий post.json приводит к rollback при вызове verify-applied; потерянный вызов — к recovery на следующей команде.

Хранение: `versions/HASH.md` — исходные байты; `candidates/ID/` — manifest, candidate и проверочные артефакты; `days/YYYY-MM-DD.json` — одна попытка записи в день Europe/Samara; `scan-index.json` — обработанные IDs/cursors/exclusions; `pending-scan.json` — незавершённый отбор; `reports/` — результаты. Директории 0700, файлы состояния 0600. Резервные копии навыков могут содержать личные инструкции: не публикуй их.

Границы гарантии: fcntl lock сериализует этот helper, не произвольный редактор. Хеш проверяется перед atomic replace; небольшой race между проверкой и replace остаётся, это не файловая песочница/CAS ядра. При явном конфликте helper отказывается. Подпись/неподделываемая аттестация IDs отсутствует: честность evidence и independent review обеспечивает главный агент. Сбой диска может помешать самому откату; manifest applying/recovery-conflict сохраняет необходимость восстановления. Не удаляй backup/manifest и не обходи conflict.

Scanner ограничен тремя UTC-днями, первым доступным набором ≤100 файлов и бюджетом чтения. Он выбирает завершённые turn по релевантным словам в user-сообщении, не понимает цель сессии. Большой record/файл, переименование, длительно незавершённый turn, отсутствие ожидаемых полей или старая дата могут оставить эпизод вне окна. Не объявляй полноту анализа. Владелец эпизода определяется только уникальным token_usage_record.thread_id для данного turn_id; имя continuation-файла и порядок session_meta не являются доказательством. Все встреченные metadata IDs используются для консервативного исключения собственного workflow. Без однозначной runtime identity эпизод пропускается с unknown_identity. Byte offsets остаются ссылкой на неизменённый источник, не копией. Одинаковый pending не анализируется заново; при прерывании продолжай сохранённую работу. Scanner выдаёт максимум 6 эпизодов с суммой диапазонов до 120 KB. Более крупный эпизод пропускается и учитывается в skipped_oversize; укажи его как непроверенный из-за бюджета. acknowledge закрывает только выданный manifest после фактического разбора.
