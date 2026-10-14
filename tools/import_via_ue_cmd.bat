@echo off
chcp 65001 > nul
rem ===========================================================================
rem  ワンポチツール: UE を headless（commandlet）で起動してインポートする
rem
rem  用途: CI / バッチ処理 / GUI を開かずに検証する
rem
rem  使い方:
rem    import_via_ue_cmd.bat "<onepochi.json のパス>" ["<.uproject のパス>"]
rem
rem  ★注意★
rem    同じプロジェクトを GUI エディタで開いたまま実行すると、アセットや DDC の
rem    書き込みが競合する可能性があります。**GUI エディタを閉じてから**実行してください。
rem
rem  環境変数:
rem    UE_ROOT  ... UE のインストール先（既定: C:\Program Files\Epic Games\UE_5.8）
rem ===========================================================================
setlocal

set "SCRIPT_DIR=%~dp0"

if "%~1"=="" (
    echo [エラー] マニフェストのパスを指定してください。
    echo         import_via_ue_cmd.bat "C:\...\fbx\onepochi.json"
    exit /b 1
)
set "MANIFEST=%~1"

if "%~2"=="" (
    set "UPROJECT=%SCRIPT_DIR%..\UEProject\OnePochiPlayground\OnePochiPlayground.uproject"
) else (
    set "UPROJECT=%~2"
)

if not exist "%UPROJECT%" (
    echo [エラー] .uproject が見つかりません: %UPROJECT%
    exit /b 1
)

if not defined UE_ROOT set "UE_ROOT=C:\Program Files\Epic Games\UE_5.8"
set "UE_CMD=%UE_ROOT%\Engine\Binaries\Win64\UnrealEditor-Cmd.exe"

if not exist "%UE_CMD%" (
    echo [エラー] UnrealEditor-Cmd.exe が見つかりません: %UE_CMD%
    echo         環境変数 UE_ROOT に UE のインストール先を設定してください。
    exit /b 1
)

rem ジョブファイルを書き出す（py への引数渡しに依存しないため）
set "JOB_DIR=%LOCALAPPDATA%\OnePochi"
if not exist "%JOB_DIR%" mkdir "%JOB_DIR%"
set "JOB=%JOB_DIR%\job.json"

rem パス区切りを / に正規化して JSON に書く
set "MANIFEST_JSON=%MANIFEST:\=/%"
for %%F in ("%MANIFEST%") do set "MANIFEST_DIR=%%~dpF"
set "REPORT=%MANIFEST_DIR%ue_report.json"
set "REPORT_JSON=%REPORT:\=/%"

> "%JOB%" echo {
>>"%JOB%" echo   "manifest": "%MANIFEST_JSON%",
>>"%JOB%" echo   "report": "%REPORT_JSON%",
>>"%JOB%" echo   "dryRun": false,
>>"%JOB%" echo   "allowFailed": false
>>"%JOB%" echo }

echo [ワンポチ] UE:         %UE_CMD%
echo [ワンポチ] プロジェクト: %UPROJECT%
echo [ワンポチ] マニフェスト: %MANIFEST%
echo [ワンポチ] レポート:     %REPORT%
echo [ワンポチ] 実行中...（初回はシェーダコンパイルで数分かかることがあります）

"%UE_CMD%" "%UPROJECT%" -run=pythonscript -script="%SCRIPT_DIR%ue_plugin\run_import.py" -unattended -nopause -nosplash -stdout -FullStdOutLogOutput
set "EXITCODE=%ERRORLEVEL%"

echo.
echo [ワンポチ] UE の終了コード: %EXITCODE%
if exist "%REPORT%" (
    echo [ワンポチ] レポートが生成されました: %REPORT%
) else (
    echo [警告] レポートが生成されていません。UE のログを確認してください:
    echo        %UPROJECT%\..\Saved\Logs\
)
endlocal & exit /b %EXITCODE%
