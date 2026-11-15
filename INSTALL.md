# Создание systemd-сервиса 
файл - utility_meter_bot.service
Путь /etc/systemd/system/utility_meter_bot.service


# 1. Установка системных пакетов (Python 3.12, venv и шрифты DejaVu для кириллицы в PDF)
sudo apt update && sudo apt install -y python3 python3-venv python3-pip fonts-dejavu-core

# 2. Создание отдельного пользователя без права интерактивного входа
sudo useradd --system --no-create-home --shell /usr/sbin/nologin utility_meter_bot_user

# 3. Создание структуры папок проекта в /opt/utility_meter_bot
sudo mkdir -p /opt/utility_meter_bot/{data,backups,exports,tests}

# 4. Перенос файлов кода в /opt/utility_meter_bot и создание виртуального окружения
cd /opt/utility_meter_bot
sudo python3 -m venv venv
sudo /opt/utility_meter_bot/venv/bin/pip install --upgrade pip
sudo /opt/utility_meter_bot/venv/bin/pip install -r requirements.txt

# 5. Назначение прав доступа только пользователю utility_meter_bot_user
sudo chown -R utility_meter_bot_user:utility_meter_bot_user /opt/utility_meter_bot
sudo chmod 750 /opt/utility_meter_bot
sudo chmod 600 /opt/utility_meter_bot/.env
/opt/utility_meter_bot/venv/bin/pip install "sqlalchemy[asyncio]" greenlet

# 6. Выравнивание владельца после работы pip от root и запуск тестов
chown -R utility_meter_bot_user:utility_meter_bot_user /opt/utility_meter_bot

# 7. Запуск модульных тестов перед стартом сервиса
sudo -u utility_meter_bot_user /opt/utility_meter_bot/venv/bin/pytest -v

#Активация и проверка статуса:
sudo systemctl daemon-reload
sudo systemctl enable --now utility_meter_bot
sudo systemctl status utility_meter_bot --no-pager