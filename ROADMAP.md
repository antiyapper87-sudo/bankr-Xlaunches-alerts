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
- Использовать SQLite как легкое локальное хранилище без внешней БД.
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
pip install sqlalchemy aiosqlite redis rq rq-scheduler
```

- Использовать SQLite для persistence.
- Использовать RQ для очередей.
- Создать `.env.example` со всеми новыми переменными.

Результат: чистый старт для дальнейшей разработки.

## Фаза 1: Core Stability & Persistence

Оценка: 4-6 дней.

Статус: самая важная фаза.

Цель: убрать in-memory проблемы, чтобы бот мог работать 24/7 без дублирования сигналов.

Задачи:

- Внедрить SQLite.
- Создать новый файл `database.py`.
- Добавить таблицы:
  - `launches`
    - `ca`
    - `ticker`
    - `source`
    - `launched_at`
    - `raw_data`
    - `status`
  - `signals`
    - `ca`
    - `verdict_score`
    - `verdict_text`
    - `sent_at`
    - `chat_id`
  - `verdict_cache`
    - `ca`
    - `verdict_json`
    - `expires_at`
- Заменить `seen_tokens` на запросы к БД.
- Заменить `signaled_tokens` на БД.
- Заменить `recheck_queue` на БД.
- Внедрить RQ:
  - producer в `main.py`
  - workers для enrichment
  - workers для verdict
- Создать новый файл `worker.py`.
- Добавить dynamic thresholds:
  - каждые 10 минут пересчитывать медианы по последним 100 лаунчам.
- Добавить multi-source deduplication по CA.

Файлы:

- `main.py`
- `database.py`
- `worker.py`

Метрика успеха:

- 0 дублирующихся сигналов после рестарта.
- Обработка 500+ лаунчей/час без падения.

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
