## Структура проекта

```
/opt/utility_meter_bot/
├── .env                    # Конфигурация и секреты 
├── requirements.txt        # Зависимости Python 3.12+
├── config.py               # Загрузка и парсинг .env, часовой пояс CET+5 (UTC+5)
├── models.py               # Модели SQLAlchemy 2.0 (User, Apartment, MeterReading, TariffHistory, AuditLog)
├── database.py             # Расчёты на Decimal, транзакции, CRUD, логирование, бэкапы
├── ui.py                   # Single-Message UI: рендеринг всех экранов и управление panel_msg_id
├── exports.py              # Выгрузка в MS Excel (.xlsx) и PDF (с кириллицей)
├── scheduler.py            # Напоминания в 15:00 (CET+5) и повторы каждые 3 дня + автобэкап
├── handlers.py             # Middleware авторизации, FSM-состояния, точечный и пакетный ввод
├── bot.py                  # Точка входа
└── tests/
    └── test_calculations.py # Модульные тесты расчёта потребления и стоимости
``` 
    
    
# Создание systemd-сервиса 
```
файл - utility_meter_bot.service
Путь /etc/systemd/system/utility_meter_bot.service
```

# Файл .env
```
В переменной TELEGRAM_BOT_TOKEN указать токен бота, выданный в @BotFather
В переменной ADMIN_TELEGRAM_ID указать Telegram ID администратора бота. ID можно узнать в @userinfobot
В переменной LANDLORD_TELEGRAM_ID указать Telegram ID владельца квартиры. ID можно узнать в @userinfobot
В переменной TENANT_ID указать Telegram ID арендатора. ID можно узнать в @userinfobot
```

# 1. Установка системных пакетов (Python 3.12, venv и шрифты DejaVu для кириллицы в PDF)
```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip fonts-dejavu-core
```

# 2. Создание отдельного пользователя без права интерактивного входа
```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin utility_meter_bot_user
```

# 3. Создание структуры папок проекта в /opt/utility_meter_bot
```bash
sudo mkdir -p /opt/utility_meter_bot/{data,backups,exports,tests}
```

# 4. Перенос файлов кода в /opt/utility_meter_bot и создание виртуального окружения
```bash
cd /opt/utility_meter_bot
sudo python3 -m venv venv
sudo /opt/utility_meter_bot/venv/bin/pip install --upgrade pip
sudo /opt/utility_meter_bot/venv/bin/pip install -r requirements.txt
```

# 5. Назначение прав доступа только пользователю utility_meter_bot_user
```bash
sudo chown -R utility_meter_bot_user:utility_meter_bot_user /opt/utility_meter_bot
sudo chmod 750 /opt/utility_meter_bot
sudo chmod 600 /opt/utility_meter_bot/.env
/opt/utility_meter_bot/venv/bin/pip install "sqlalchemy[asyncio]" greenlet
```

# 6. Выравнивание владельца после работы pip от root и запуск тестов
```bash
chown -R utility_meter_bot_user:utility_meter_bot_user /opt/utility_meter_bot
```

# 7. Запуск модульных тестов перед стартом сервиса
```bash
sudo -u utility_meter_bot_user /opt/utility_meter_bot/venv/bin/pytest -v
```

# Активация и проверка статуса:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now utility_meter_bot
sudo systemctl status utility_meter_bot --no-pager
```