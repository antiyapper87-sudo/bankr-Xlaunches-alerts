# Product Roadmap

## Product Vision

Цель проекта — не очередной alert-бот, а сильный research-инструмент для ранних Base-лаунчей.
Продукт должен быстро объяснять, почему токен стоит смотреть, снижать шум, поддерживать
watchlist, smart-money tracking и дальнейшую монетизацию.

Позже, после стабилизации Base-продукта и первых платящих пользователей:

- Discord support
- Solana
- дополнительные on-chain метрики

## Общие Правила

- Не добавлять ничего нового, пока не закрыта текущая фаза.
- До конца Фазы 5 фокус только на Base.
- Для локального MVP можно использовать SQLite.
- Для сервиса на 1000+ клиентов использовать managed Postgres + Redis/queue.
- Каждую фазу заканчивать тестированием и 1-2 днями на багфиксы.
- После каждой фазы обновлять `architecture.md`.
- Не включать on-chain trading до отдельной безопасной реализации.

## Фаза 0: Подготовка

Оценка: 1 день.

Цель: четко зафиксировать текущее состояние и подготовить репозиторий.

Задачи:

- Создать ветку `roadmap-v1`.
- Обновить `architecture.md`:
  - добавить раздел `Current Limitations`
  - добавить раздел `Target Product Vision`
- Установить зависимости:

```bash
pip install aiosqlite asyncpg redis arq
```

- Использовать SQLite только для local/dev режима.
- Использовать managed Postgres для production persistence.
- Использовать Redis-backed queue для production fanout/retries.
- Создать `.env.example` со всеми новыми переменными.

Результат: чистый старт для дальнейшей разработки.

## Фаза 1: Service-Grade Core Stability & Persistence

Оценка: 10-15 дней.

Статус: самая важная фаза.

Цель: убрать in-memory проблемы и заложить foundation для сервиса, который может
одновременно обслуживать 1000+ клиентов/трейдеров без дублей, потери состояния и ручного
операционного хаоса.

Ключевое решение:

- Для локального single-user деплоя допустим SQLite.
- Для product/service деплоя сразу проектировать под Postgres.
- Redis/queue нужен не для discovery, а для fanout, rate limits, retries и background jobs.
- Trading остается выключенным.

Задачи:

- Внедрить production persistence.
- Локально: SQLite-compatible repository layer.
- Production: managed Postgres.
- Создать новый файл `database.py`.
- Не завязывать бизнес-логику на конкретный драйвер БД.
- Добавить таблицы:
  - `tenants`
    - `id`
    - `type` (`telegram_user`, `telegram_group`, later `discord_server`)
    - `external_id`
    - `plan`
    - `status`
    - `created_at`
  - `users`
    - `id`
    - `telegram_user_id`
    - `username`
    - `role`
    - `created_at`
  - `tenant_members`
    - `tenant_id`
    - `user_id`
    - `role`
  - `launches`
    - `ca`
    - `ticker`
    - `source`
    - `launched_at`
    - `raw_data`
    - `status`
    - `first_seen_at`
    - `last_checked_at`
    - `next_check_at`
    - `check_count`
    - `market_data`
  - `signals`
    - `ca`
    - `tenant_id`
    - `verdict_score`
    - `verdict_text`
    - `sent_at`
    - `chat_id`
    - `message_id`
    - unique key: `(tenant_id, ca)`
  - `signal_deliveries`
    - `signal_id`
    - `tenant_id`
    - `channel`
    - `status`
    - `attempt_count`
    - `last_error`
    - `delivered_at`
  - `verdict_cache`
    - `ca`
    - `verdict_json`
    - `expires_at`
  - `tenant_settings`
    - `tenant_id`
    - `min_score`
    - `enabled_sources`
    - `quiet_hours`
    - `delivery_mode`
  - `audit_events`
    - `tenant_id`
    - `event_type`
    - `payload`
    - `created_at`
- Заменить `seen_tokens` на запросы к БД.
- Заменить `signaled_tokens` на БД.
- Заменить `recheck_queue` на БД.
- Разделить runtime на логические контуры:
  - ingestion: Bankr/Clanker/Virtuals polling
  - enrichment: market/social enrichment
  - scoring: deterministic verdict
  - fanout: доставка сигналов tenant/user/group targets
  - commands: Telegram command handling
- Внедрить очередь для production:
  - локально можно оставить in-process async queue
  - production: Redis Queue / Arq / Celery, выбрать один после адаптера
  - не импортировать worker из `main.py`
  - worker должен импортировать только чистые сервисы
- Создать новый файл `worker.py`.
- Добавить delivery idempotency:
  - один CA не может быть отправлен одному tenant дважды
  - retries не должны создавать новые Telegram messages
  - Telegram edit должен использовать сохраненный `message_id`
- Добавить rate limiting:
  - Telegram Bot API limits
  - SocialData budget
  - GeckoTerminal cooldown
  - per-tenant daily signal limits
- Добавить backpressure:
  - bounded queues
  - max concurrent enrichment jobs
  - max concurrent verdict jobs
  - degraded mode when APIs rate-limit
- Добавить multi-source deduplication по CA.
- Добавить observability:
  - structured logs
  - `/status` из БД
  - `/health` или health log line
  - counters: launches/min, signals/day, queue depth, delivery failures
  - error budget по external API
- Добавить `.env.example`:
  - local SQLite mode
  - production Postgres mode
  - Redis URL
  - Telegram/SocialData/API keys
- Добавить smoke tests:
  - DB schema init
  - CA dedupe
  - tenant delivery uniqueness
  - restart no-duplicate scenario
  - recheck survives restart

Что НЕ включать в Фазу 1:

- Solana.
- On-chain trading.
- Dynamic thresholds как active filter.
- LLM scoring.
- Stripe/subscriptions.
- Discord.
- ML/user feedback.

Файлы:

- `main.py`
- `database.py`
- `worker.py`
- `services/ingestion.py`
- `services/enrichment.py`
- `services/scoring.py`
- `services/delivery.py`
- `services/tenants.py`
- `.env.example`
- `tests/`

Метрика успеха:

- 0 дублирующихся сигналов после рестарта.
- Recheck queue переживает рестарт.
- Один сигнал доставляется 1000 tenants без дублей.
- Telegram fanout имеет retries и rate limiting.
- 1000 клиентов могут иметь разные `min_score` / delivery settings.
- Обработка 500+ лаунчей/час без падения.
- 10k signal deliveries/day без ручного вмешательства.
- `/status` показывает состояние из БД, а не из process memory.

## Фаза 2: Verdict 2.0 + Spoof Detection

Оценка: 5-7 дней.

Цель: сделать главный edge продукта — понятный ruthless verdict и защиту от spoof/ticker reuse.

Задачи:

- Полностью переписать `research_pipeline.py`.
- Ввести 5-балльную структуру verdict:
  - Market Health — 30%
  - Deployer — 25%
  - Social — 30%
  - Risk — 10%
  - Narrative — 5%
- Сохранить deterministic scoring.
- Добавить selective LLM:
  - запускать LLM только если score > 65.
- Добавить Spoof / Ticker History:
  - новая таблица `historical_launches`
  - при каждом новом лаунче проверять, сколько раз ticker запускался за последние 30-60 дней
  - добавлять автоматический risk-блок в verdict
- Улучшить deployer history:
  - количество предыдущих лаунчей
  - процент dead coins
- Обновить Telegram-сигнал:
  - заменить placeholder на новый структурированный verdict.

Метрика успеха:

- Пользователь за 5 секунд понимает, почему токен попал в сигнал.
- Средний score сигналов выше 6.5.

## Фаза 3: User Features & Retention

Оценка: 6-8 дней.

Цель: сделать бот полезным каждый день, а не только при новых лаунчах.

Задачи:

- Watchlist:
  - `/watch 0xCA [label]`
  - `/unwatch`
  - `/watchlist`
- Добавить таблицу `user_watchlists`.
- Background job:
  - каждые 15 минут проверять price/volume change для watchlist-токенов.
- Custom score threshold:
  - `/settings min_score 7.5`
  - каждый пользователь хранит свои настройки.
- Улучшить `/research`:
  - глубокий поиск по CA/ticker
  - исторические данные
  - spoof check
- Добавить user feedback:
  - кнопка `Worth watching`
  - кнопка `Skip`
  - сохранять ответы для будущего ML/ранжирования.

Метрика успеха:

- 30%+ сигналов попадают в watchlist пользователей.

## Фаза 4: Proprietary Edge — Wallet Tracking

Оценка: 7-10 дней.

Цель: добавить smart-money inflow как один из самых сильных сигналов.

Задачи:

- Добавить таблицу `tracked_wallets`.
- Команды:
  - `/track 0xWALLET [label]`
  - `/untrack`
  - `/wallets`
- Background worker:
  - мониторинг покупок tracked wallets в новые лаунчи.
- Источники для wallet monitoring:
  - DexScreener
  - Alchemy webhooks
- Добавить блок в verdict:
  - `Smart Money`
  - показывать, если топ-кошельки зашли в первые 5-10 минут.
- Опционально:
  - парсинг публичного Fomo App leaderboard 1 раз в 5 минут.

Метрика успеха:

- Появление сигналов формата:

```text
Smart Money inflow: 3 wallets в первые 3 минуты
```

## Фаза 5: Monetization & Multi-tenant

Оценка: 8-12 дней.

Цель: начать зарабатывать.

Задачи:

- Telegram-подписка:
  - `/premium`
  - Stripe payment link
- Добавить таблицу `user_subscriptions`.
- Ограничение free users:
  - например, 10 сигналов/день.
- Discord support:
  - отдельный режим
  - второй бот или multi-bot architecture
  - `/setup` на сервере
  - per-server оплата, дороже Telegram-подписки
- Analytics dashboard:
  - простой веб-интерфейс
  - или команды `/stats`.

Метрика успеха:

- Первые 10-20 платящих пользователей.

## Фаза 6: Expansion

Начинать только после retention и дохода на Base.

Направления:

- Solana:
  - Pump.fun
  - Raydium
  - отдельный модуль `solana_pipeline.py`
- Дополнительные on-chain метрики:
  - snipers
  - LP velocity
  - wallet clustering

## Timeline

Реалистичная оценка:

- Фазы 1-2: 9-13 дней — бот уже сильно лучше текущей версии.
- Фазы 1-3: 3-4 недели — готовый research product.
- Фазы 1-5: 6-8 недель — монетизация.
- Полный продукт с wallet tracking и Discord: 2-2.5 месяца.

## Что Не Делать До Фазы 5

- Не трогать Solana.
- Не включать on-chain trading.
- Не добавлять новые launch sources.
- Не делать `AI brief` маркетинговым хайпом, пока LLM не будет в production.
