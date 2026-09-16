@echo off
chcp 932 > nul
cd /d "%~dp0"
echo ====================================
echo  crypto-news-bot セットアップ
echo ====================================
echo.
echo 必要なライブラリをインストールします...
echo.
python -m pip install -r requirements.txt
echo.
if errorlevel 1 (
    echo [エラー] インストールに失敗しました。
    echo Python がインストールされているか確認してください。
) else (
    echo [完了] セットアップが終わりました。
    echo 次に .env ファイルに Discord の Webhook URL を設定してください。
)
echo.
pause
