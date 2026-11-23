# Utility Meter Bot — правила проекта

## О проекте
Telegram-бот + Telegram Web App (Mini App) для учёта показаний счётчиков воды и электричества, управления ролями (семья / арендодатель / арендатор), договорами, фото счётчиков и экспортом отчётов в Excel и PDF.

## Стек и окружение
- **Язык и среда:** Python 3.12 (Docker `python:3.12-slim`), `asyncio`
- **Бот и веб-сервер:** `aiogram` 3.x, `FastAPI` + `uvicorn` (единая точка запуска в `main.py`)
- **База данных:** SQLite (`data/utility_meter.db`), `SQLAlchemy` 2.0 (asyncio) + `aiosqlite`
- **Фоновые задачи:** `APScheduler` (напоминания о показаниях, автобэкапы)
- **Экспорт данных:** `openpyxl` (Excel), `fpdf2` (PDF-квитанции)
- **Фронтенд Mini App:** HTML5, CSS3, Vanilla JS (Telegram WebApp SDK, initData HMAC-SHA256)
- **Тесты и конфиг:** `pytest`, `pytest-asyncio`, `python-dotenv` (настройки в `config.py`)
- **Инфраструктура:** Docker Compose (`docker-compose.yml`), Cloudflare Tunnel (`cloudflared`)
- **Правила окружения:** Windows (разработка, пути только `pathlib.Path`, окончания строк LF), Ubuntu 24.04 VPS (прод)

## Структура проекта
- `main.py` — единая точка входа: инициализация БД, запуск FastAPI, aiogram и APScheduler
- `bot_handlers.py` — хэндлеры Telegram-бота (`/start`, кнопка Mini App, приём фото счётчиков)
- `config.py` — типизированная конфигурация приложения и загрузка `.env`
- `database/` — работа с базой данных:
  - `models.py` — SQLAlchemy модели таблиц (пользователи, квартиры, показания, договоры)
  - `session.py` — async engine и фабрика сессий SQLite
  - `crud.py`, `operations.py` — CRUD и прикладные бизнес-операции с БД
- `webapp/` — бэкенд и статика Telegram Web App:
  - `api.py`, `auth.py`, `schemas.py` — инициализация FastAPI, валидация initData, Pydantic-схемы
  - `routers/` — эндпоинты (`apartments`, `readings`, `tenants`, `families`, `admin`)
  - `static/` — клиентская часть Mini App (`index.html`, `style.css`, `app.js`)
- `services/` — сервисный слой:
  - `calculations.py`, `export.py` — расчёты начислений, формирование Excel и PDF
  - `access.py`, `notifications.py` — разграничение прав и отправка уведомлений в Telegram
  - `scheduler.py`, `backup.py` — планировщик задач и бэкапы БД
- `data/` — каталог для локальной SQLite БД, бэкапов и файлов (в `.gitignore`)
- `tests/` — модульные и интеграционные тесты (`test_security_and_billing.py`)
- `Dockerfile`, `docker-compose.yml` — контейнеризация сервиса и Cloudflare Tunnel

## Жёсткие ограничения
1. **Безопасность:** не читать, не выводить в ответы и не перезаписывать `.env`, токены, реальные файлы `*.db` / `*.sqlite` и бэкапы.
2. **Сохранность данных:** не менять существующую схему SQLite с потерей данных; любые новые поля добавлять через безопасные миграции (`ALTER TABLE ... ADD COLUMN` с проверкой существования).
3. **Деплой:** не ломать пути bind-монтирования в Docker и формат переменных в `.env`.
4. **Интерфейс:** все тексты для пользователей бота, кнопки, уведомления и комментарии в коде писать на русском языке; сохранять чистый UI (основная работа в Mini App, в чате — кнопка открытия, уведомления и пересылка файлов).
5. **Процесс правок:** перед изменением более 2 файлов или правкой логики БД сначала выводить краткий план изменений и ждать подтверждения. Не рефакторить код, не относящийся к текущей задаче.

## Проверка перед завершением задачи
- Проверить синтаксис изменённых Python-файлов (`python -m py_compile <файл>`).
- Убедиться, что новые зависимости (если появились) добавлены в `requirements.txt`.
- В конце ответа кратко перечислить изменённые файлы и указать, нужен ли перезапуск контейнера (`docker compose restart`) или пересборка образа.
