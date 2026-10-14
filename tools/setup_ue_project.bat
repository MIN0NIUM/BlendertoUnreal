@echo off
chcp 65001 > nul
rem ===========================================================================
rem  ワンポチツール: UE プロジェクトに Remote Control の設定を入れる
rem
rem  使い方:
rem    setup_ue_project.bat "D:\MyProj\MyProj.uproject"   ... 指定のプロジェクトへ
rem    setup_ue_project.bat                               ... パスを聞かれる
rem    .uproject をこの .bat へドラッグ&ドロップしてもよい
rem
rem  追加の引数はそのまま渡る:
rem    setup_ue_project.bat "D:\MyProj\MyProj.uproject" --dry-run     ... 見るだけ
rem    setup_ue_project.bat "D:\MyProj\MyProj.uproject" --no-plugins  ... .uproject は触らない
rem
rem  書き換える前に内容を表示し、確認してから書く。元のファイルは .bak-日時 で残す。
rem  書いたあとは UE エディタを再起動すること（.ini は起動時にしか読まれない）。
rem ===========================================================================
setlocal

set "SCRIPT_DIR=%~dp0"

set "PY=python"
where python >nul 2>nul || set "PY=py -3"

if "%~1"=="" (
    echo UE プロジェクトの .uproject ^(またはプロジェクトフォルダ^) のパスを入力してください。
    set /p "PROJECT=パス: "
    set "PROJECT=%PROJECT:"=%"
    %PY% "%SCRIPT_DIR%setup_ue_project.py" "%PROJECT%"
) else (
    %PY% "%SCRIPT_DIR%setup_ue_project.py" %*
)
set "EXITCODE=%ERRORLEVEL%"

if not defined ONEPOCHI_NO_PAUSE pause
endlocal & exit /b %EXITCODE%
