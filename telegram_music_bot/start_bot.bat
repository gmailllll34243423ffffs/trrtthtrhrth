@echo off
chcp 65001 > nul
title Музыкальный Бот Знакомств
echo ====================================================
echo  Запуск музыкального бота знакомств...
echo ====================================================

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ОШИБКА] Python не найден в системе!
    echo Установите Python с официального сайта python.org
    echo Обязательно поставьте галочку "Add python.exe to PATH".
    pause
    exit /b
)

if not exist venv (
    echo [1/3] Создание виртуального окружения (venv)...
    python -m venv venv
)

echo [2/3] Проверка библиотек (aiogram, aiosqlite, python-dotenv)...
call venv\Scripts\activate
python -m pip install -q -r requirements.txt

echo [3/3] Запуск бота...
python bot.py

pause
