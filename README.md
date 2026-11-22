# Utility Meter Bot & Mini App ⚡📟

Telegram-бот и веб-приложение (Telegram Mini App) для учёта показаний коммунальных счётчиков, управления объектами недвижимости/аренды, отправки уведомлений и автобэкапов.

---

## 🏗 Архитектура и стек технологий

* **Backend / API:** Python 3.12, FastAPI, Uvicorn.
* **Telegram Bot:** Aiogram 3.x (режим Polling).
* **Фоновые задачи:** APScheduler (напоминания о показаниях, проверка окончания договоров аренды, резервное копирование).
* **Авторизация:** Проверка подписи Telegram `X-Init-Data` (`HMAC-SHA256`) на бэкенде.
* **Контейнеризация:** Docker & Docker Compose (2 контейнера: `utility-meter-app` и `tunnel`).
* **Ингресс / SSL:** Cloudflare Tunnel (`cloudflared`) с маршрутизацией на порт `8000`.

---

## 🚀 Основные возможности

1. **Telegram Mini App:** Удобный интерфейс для ввода показаний счётчиков (электричество, вода, газ и др.) напрямую из Telegram.
2. **Уведомления и напоминания:**
   * Автоматическое напоминание о сдаче показаний счетчиков.
   * Уведомления об истечении сроков аренды/найма объектов.
3. **Автоматический бэкап:** Регулярное создание резервных копий баз данных по расписанию.
4. **Безопасный доступ:** API защищён валидацией `initData` Telegram, исключая несанкционированный доступ извне.

---

## 📁 Структура проекта

```text
/opt/utility_meter_bot/
├── main.py                  # Точка входа (запуск FastAPI + Aiogram + APScheduler)
├── config.py                # Загрузка и валидация конфигурации из .env
├── requirements.txt         # Зависимости Python
├── Dockerfile               # Образ на базе python:3.12-slim
├── docker-compose.yml       # Описание сервисов (app + cloudflared tunnel)
├── .env                     # Переменные окружения (токены, ключи, домены)
├── data/                    # Хранилище SQLite / бэкапов
└── README.md                # Документация проекта
```

---

## ⚙️ Конфигурация (.env)

Файл `.env` должен иметь строгие права доступа (`chmod 600 .env`) и содержать следующие основные переменные:

```env
TELEGRAM_BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrsTUVwxyZ
WEBAPP_URL=https://utility-meter.litvinov.uk
# Дополнительные настройки БД, администраторов и расписаний
```

---

## 🐳 Развёртывание и запуск (Docker Compose)

### 1. Первичный запуск
```bash
cd /opt/utility_meter_bot
sudo chmod 600 .env
sudo docker compose up -d --build
```

### 2. Обновление токена или параметров в `.env`
Так как Docker Compose и Python не всегда подхватывают изменения `.env` при обычном `restart`, для обновления переменных окружения перезапускайте приложение со флагом `--force-recreate`:

```bash
cd /opt/utility_meter_bot
sudo docker compose up -d --force-recreate utility-meter-app
```

---

## 🛠 Проверка статуса и диагностика

### Проверка запущенных контейнеров
```bash
sudo docker compose ps
```

### Просмотр логов приложения и туннеля
```bash
# Логи основного приложения
sudo docker compose logs --tail=30 utility-meter-app

# Логи туннеля Cloudflare
sudo docker compose logs --tail=20 tunnel
```

### Проверка работоспособности эндпоинтов (с сервера)
```bash
# Проверка главной страницы Mini App (ожидается HTTP/2 200)
curl -sI https://utility-meter.litvinov.uk/ | head -n 1

# Проверка защищенного API без заголовка авторизации (ожидается HTTP/2 401)
curl -i -s https://utility-meter.litvinov.uk/api/me | head -n 6
```

---

## 📱 Настройка бота в BotFather

Чтобы веб-приложение открывалось по кнопке внутри Telegram:

1. Перейдите в бота **[@BotFather](https://t.me/BotFather)**.
2. Выберите вашего бота через `/mybots`.
3. Откройте **Bot Settings** -> **Menu Button** -> **Configure menu button**.
4. Введите URL веб-приложения: `https://utility-meter.litvinov.uk`
5. Задайте название кнопки меню (например: `📱 Счётчики`).