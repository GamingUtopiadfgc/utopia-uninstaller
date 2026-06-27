@echo off
title Utopia Uninstaller - Setup

:: Check for Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Python is not installed or not in PATH.
    echo Please download and install Python 3.10+ from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

:: Install dependencies
echo Installing required packages...
python -m pip install -r requirements.txt --quiet
if %errorlevel% neq 0 (
    echo Failed to install dependencies. Check your internet connection.
    pause
    exit /b 1
)

:: Run the program
echo Starting Utopia Uninstaller...
python main.py
