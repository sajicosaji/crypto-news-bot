@echo off
chcp 932 > nul
cd /d "%~dp0"
echo 各チャンネルにテスト投稿します...
echo.
python main.py --test-webhooks
echo.
pause
