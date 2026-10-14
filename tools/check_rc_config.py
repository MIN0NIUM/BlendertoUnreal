# -*- coding: utf-8 -*-
"""Remote Control の設定診断（M-1 / R-5 / docs/99 P-29）。

UE の Remote Control 経由の Python 実行が拒否されたときに、**どの設定が原因かを
段階的なプローブで切り分ける**。UE が起動していなくても `.ini` の点検だけは動く。

使い方:

    python tools/check_rc_config.py              # 点検 + プローブ
    python tools/check_rc_config.py --ini-only   # `.ini` の点検だけ（UE 不要）
    python tools/check_rc_config.py --json out.json
    python tools/check_rc_config.py --project "D:/MyProj/MyProj.uproject"
                                                 # 自分のプロジェクトを点検する
                                                 # （省略時は Git 管理外の検証プロジェクト）

終了コード:
    0 ... Python 実行が許可されている（`--via-rc` が使える）
    1 ... 拒否されている（原因の切り分け結果を日本語で出す）
    2 ... UE に到達できない / 実行環境の問題

**切り分けの考え方**（2026-10-13 の実測で確定した段階）:

    ① `GET /remote/info`                     サーバが起動しているか
    ② プロパティ読み（許可対象のオブジェクト）  クライアント許可リスト / パスフレーズが
                                             効いていないか（ここが 200 なら両方シロ）
    ③ 非 Python 関数の呼び出し                関数許可リストの検査が動いているか
    ④ `ExecutePythonCommandEx` の呼び出し     本命。Python 実行が許可されているか

②が 200 なのに④が 400 で
`Object Default__PythonScriptLibrary cannot be accessed remotely` なら、
**原因はクライアント許可リストでもパスフレーズでもなく、Python 実行の許可そのもの**。
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
#: 開発用の検証プロジェクト。**Git 管理外**なので、ダウンロードした環境には無い。
#: その場合は `--project` で点検したいプロジェクトを指定する。
DEV_PROJECT = os.path.join(ROOT, "UEProject", "OnePochiPlayground")

PROJECT = DEV_PROJECT
#: 設定が**実際に読まれる**プロジェクト側ファイル。
#: URemoteControlSettings は UCLASS(config = RemoteControl) なので
#: RemoteControl.ini 階層からのみ読まれる（RemoteControlSettings.h:180）。
DEFAULT_RC_INI = ""
SAVED_RC_INI = ""
#: ここに書いても**無視される**。間違えやすいので誤りとして検出する。
DEFAULT_ENGINE_INI = ""


def set_project(project_dir):
    """点検対象の UE プロジェクト（フォルダ）を切り替える。

    `setup_ue_project.py` も、書き込んだ結果をこの点検で確かめるために使う。
    """
    global PROJECT, DEFAULT_RC_INI, SAVED_RC_INI, DEFAULT_ENGINE_INI
    PROJECT = os.path.abspath(project_dir)
    DEFAULT_RC_INI = os.path.join(PROJECT, "Config", "DefaultRemoteControl.ini")
    SAVED_RC_INI = os.path.join(
        PROJECT, "Saved", "Config", "WindowsEditor", "RemoteControl.ini")
    DEFAULT_ENGINE_INI = os.path.join(PROJECT, "Config", "DefaultEngine.ini")


set_project(DEV_PROJECT)

SECTION = "/Script/RemoteControlCommon.RemoteControlSettings"
HOST = "127.0.0.1"
PORT = 30010

PYTHON_LIBRARY = "/Script/PythonScriptPlugin.Default__PythonScriptLibrary"

#: 点検する設定キー -> (期待値, 必須か, なぜ必要か)
EXPECTED = (
    ("bEnableRemotePythonExecution", "True", True,
     "これが False だと PythonScriptLibrary がオブジェクト単位で拒否される（本命）。"
     "RemoteControlModule.cpp:2563 CanBeAccessedRemotely が見ている唯一の条件"),
    ("CustomAllowedRemoteFunctionCalls", None, True,
     "ExecutePythonCommandEx を名指しで許可する行。"
     "既定の AllowedRemoteFunctionCalls には入っていないので必須"
     "（RemoteControlSettings.cpp:25）"),
    ("bRestrictServerAccess", "True", False,
     "サーバへのアクセスを許可リストとパスフレーズで絞る（True のままにする＝緩めない）。"
     "127.0.0.1 は許可リストの対象外として常に通る"
     "（RemoteControlDefaultPreprocessors.cpp:419）"),
    ("bAllowAnyRemoteFunctionCall", "False", False,
     "True にすると何でも呼べてしまう。**False を維持する**"),
    ("bSecuritySettingsReviewed", "True", False,
     "エディタ側の警告表示用フラグ。UE 5.8 の公開ソースでは実行時のゲートから"
     "参照されていないので、拒否の原因にはならない（2026-10-13 実測）"),
)


def _configure_stdout():
    """パイプに流すときだけ UTF-8 に固定する。

    実コンソール（cmd.exe / PowerShell）では Python が Windows の
    コンソール API を使うので**触らないほうが日本語が正しく出る**。
    リダイレクトやパイプのときはロケール（cp932）に落ちて化けるので UTF-8 にする。
    """
    stream = getattr(sys, "stdout", None)
    if stream is None or not hasattr(stream, "reconfigure"):
        return
    try:
        if not stream.isatty():
            stream.reconfigure(encoding="utf-8")
    except Exception:  # pragma: no cover - 失敗しても出力は続ける
        pass


def _print(text=""):
    try:
        print(text)
    except UnicodeEncodeError:  # pragma: no cover - cmd.exe の既定コードページ対策
        print(str(text).encode("ascii", "replace").decode("ascii"))


# --- `.ini` の点検 ------------------------------------------------------------


def read_section(path, section):
    """`.ini` の指定セクションを `{key: [値, ...]}` で返す。無ければ空 dict。

    配列プロパティ（`+Key=...`）を拾うため **値はリスト**で持つ。
    ファイルが無いことは例外にしない（存在しないのも情報）。
    """
    if not os.path.isfile(path):
        return None
    values = {}
    current = None
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith(";"):
                continue
            if line.startswith("[") and line.endswith("]"):
                current = line[1:-1]
                continue
            if current != section or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().lstrip("+-.!")
            values.setdefault(key, []).append(value.strip())
    return values


def inspect_inis():
    """`.ini` を点検して (表の行, 問題のリスト, 生の値) を返す。"""
    rc_values = read_section(DEFAULT_RC_INI, SECTION)
    saved_values = read_section(SAVED_RC_INI, SECTION)
    engine_values = read_section(DEFAULT_ENGINE_INI, SECTION)

    rows = []
    problems = []
    for key, expected, required, why in EXPECTED:
        in_rc = (rc_values or {}).get(key)
        in_saved = (saved_values or {}).get(key)
        rows.append((key, expected, required, in_rc, in_saved, why))

        if not in_rc:
            if required:
                problems.append(
                    "{0} が Config/DefaultRemoteControl.ini に入っていません。"
                    "Saved 側だけだとエディタ再起動で失われます（P-29）".format(key))
            continue
        if expected is not None and in_rc[-1] != expected:
            problems.append(
                "{0} の DefaultRemoteControl.ini の値が {1} です（期待は {2}）".format(
                    key, in_rc[-1], expected))

        # Saved 側は後から読まれるので、違う値が残っていれば打ち消される。
        if in_saved and expected is not None and in_saved[-1] != expected:
            problems.append(
                "{0} が Saved 側で {1} に上書きされています"
                "（Saved は後から読まれるので勝ちます）".format(key, in_saved[-1]))

    if rc_values is None:
        problems.insert(0, "Config/DefaultRemoteControl.ini が見つかりません: {0}".format(
            DEFAULT_RC_INI))

    if engine_values:
        problems.append(
            "Config/DefaultEngine.ini に [{0}] があります。"
            "このクラスは UCLASS(config = RemoteControl) なので"
            "**Engine.ini からは読まれません**。DefaultRemoteControl.ini に移してください"
            .format(SECTION))

    return rows, problems, {
        "defaultRemoteControl": rc_values,
        "saved": saved_values,
        "defaultEngine": engine_values,
    }


def print_ini_table(rows):
    _print("=" * 78)
    _print("[1] 設定ファイルの点検")
    _print("=" * 78)
    _print("  Config/DefaultRemoteControl.ini                 … 起動時に読まれる / 配布される側")
    _print("  Saved/Config/WindowsEditor/RemoteControl.ini     … ユーザー単位 / 後から読まれるので勝つ")
    _print("  （Config/DefaultEngine.ini に書いても無視される → RemoteControlSettings.h:180）")
    _print("")
    for key, expected, required, in_rc, in_saved, why in rows:
        _print("  {0}{1}".format(key, "" if required else "（任意）"))
        _print("    期待値       : {0}".format(expected if expected else "（行が存在すること）"))
        _print("    DefaultRC    : {0}".format(
            " / ".join(in_rc) if in_rc else "✖ 無し"))
        _print("    Saved        : {0}".format(
            " / ".join(in_saved) if in_saved else "─ 無し（＝打ち消されない）"))
        _print("    なぜ必要か   : {0}".format(why))
        _print("")


# --- RC へのプローブ ----------------------------------------------------------


def request(path, body=None, method="PUT", timeout=10):
    """RC へ1回リクエストする。**例外を投げない**。(status, text) を返す。"""
    url = "http://{0}:{1}{2}".format(HOST, PORT, path)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")
    except Exception as error:  # 接続不可 / タイムアウト
        return None, "{0}: {1}".format(type(error).__name__, error)


def probe():
    """段階的にプローブして結果のリストを返す。"""
    stages = []

    status, text = request("/remote/info", method="GET")
    stages.append({
        "id": "①",
        "name": "サーバ到達（GET /remote/info）",
        "status": status,
        "ok": status == 200,
        "detail": text[:160],
    })

    status, text = request("/remote/object/property", {
        "objectPath": "/Script/UnrealEd.Default__EditorStyleSettings",
        "propertyName": "SelectionColor",
        "access": "READ_ACCESS",
    })
    stages.append({
        "id": "②",
        "name": "プロパティ読み（許可リスト / パスフレーズの検査）",
        "status": status,
        "ok": status == 200,
        "detail": text[:160],
    })

    status, text = request("/remote/object/call", {
        "objectPath": "/Script/Engine.Default__KismetSystemLibrary",
        "functionName": "GetEngineVersion",
        "generateTransaction": False,
    })
    stages.append({
        "id": "③",
        "name": "非 Python 関数の呼び出し（関数許可リストの検査）",
        "status": status,
        # 許可リストに載っていない関数なので **400 が正常**
        "ok": status in (200, 400),
        "detail": text[:200],
    })

    status, text = request("/remote/object/call", {
        "objectPath": PYTHON_LIBRARY,
        "functionName": "ExecutePythonCommandEx",
        "parameters": {"pythonCommand": "import unreal; unreal.log('onepochi rc probe')"},
        "generateTransaction": False,
    })
    stages.append({
        "id": "④",
        "name": "Python 実行（ExecutePythonCommandEx）★本命",
        "status": status,
        "ok": status == 200,
        "detail": text[:200],
    })
    return stages


def print_probe(stages):
    _print("=" * 78)
    _print("[2] Remote Control へのプローブ（http://{0}:{1}）".format(HOST, PORT))
    _print("=" * 78)
    for stage in stages:
        mark = "✔" if stage["ok"] else "✖"
        _print("  {0} {1} {2}".format(mark, stage["id"], stage["name"]))
        _print("      HTTP {0}  {1}".format(
            stage["status"], stage["detail"].replace("\r\n", " ").replace("\n", " ")))
    _print("")


def diagnose(stages, problems):
    """プローブの結果から原因を切り分けて (終了コード, 診断文のリスト) を返す。"""
    by_id = {stage["id"]: stage for stage in stages}
    lines = []

    if by_id["①"]["status"] is None:
        lines.append("✖ UE の Remote Control サーバに到達できません。")
        lines.append("   ・UE エディタが起動しているか確認してください")
        lines.append("   ・Plugins で Remote Control API が有効か、")
        lines.append("     ポートが {0} か確認してください".format(PORT))
        return 2, lines

    if by_id["④"]["ok"]:
        lines.append("✔ Python 実行が許可されています。`--via-rc` が使えます。")
        lines.append("   例: python tools/run_tests.py --with-ue --via-rc")
        if problems:
            lines.append("")
            lines.append("△ ただし設定ファイルに次の問題が残っています"
                         "（エディタ再起動で再発する恐れ / P-29）:")
            for problem in problems:
                lines.append("   ・{0}".format(problem))
        return 0, lines

    detail = by_id["④"]["detail"]
    lines.append("✖ Python 実行が拒否されています。")
    lines.append("")

    if not by_id["②"]["ok"]:
        lines.append("【切り分け】②のプロパティ読みも通っていません。")
        lines.append("  → **クライアント許可リスト / パスフレーズ**が原因の可能性が高いです。")
        lines.append("     AllowlistedClients に 127.0.0.1 が含まれているか、")
        lines.append("     bEnforcePassphraseForRemoteClients を確認してください")
    elif "cannot be accessed remotely" in detail:
        lines.append("【切り分け】②のプロパティ読みは通っているので、")
        lines.append("  **クライアント許可リストとパスフレーズはシロ**です。")
        lines.append("  PythonScriptLibrary が**オブジェクト単位で拒否**されています。")
        lines.append("  → 原因は `bEnableRemotePythonExecution` が"
                     "実行時に効いていないことです")
        lines.append("")
        lines.append("  対処（この順に試す）:")
        lines.append("   1. Config/**DefaultRemoteControl**.ini に設定が入っているか"
                     "（上の表 [1] を見る）")
        lines.append("      DefaultEngine.ini に書いても読まれません"
                     "（UCLASS(config = RemoteControl)）")
        lines.append("   2. **入っているのに拒否されるなら UE エディタを再起動する。**")
        lines.append("      `.ini` は起動時にしか読まれません")
        lines.append("   3. それでも拒否されるなら Project Settings > Plugins >")
        lines.append("      Remote Control で手動で入れ直す（その場では効くが"
                     "再起動で失われる / P-29）")
    elif "not allowed by remote control settings" in detail:
        lines.append("【切り分け】オブジェクトには到達できていますが、"
                     "**関数が許可リストに載っていません**。")
        lines.append("  → CustomAllowedRemoteFunctionCalls に")
        lines.append("     ExecutePythonCommandEx の行があるか確認してください")
    else:
        lines.append("【切り分け】既知のパターンに当てはまりません。"
                     "応答をそのまま docs/99 に記録してください:")
        lines.append("   {0}".format(detail))

    if problems:
        lines.append("")
        lines.append("設定ファイルの問題:")
        for problem in problems:
            lines.append("   ・{0}".format(problem))
    return 1, lines


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="check_rc_config.py",
        description="Remote Control の設定を点検し、拒否の原因を切り分ける")
    parser.add_argument("--ini-only", action="store_true",
                        help="`.ini` の点検だけ行う（UE が起動していなくてよい）")
    parser.add_argument("--json", default=None, help="結果を JSON で書き出す")
    parser.add_argument("--project", default=None,
                        help="点検する UE プロジェクトのフォルダ（または .uproject）。"
                             "省略時は同梱の検証プロジェクト UEProject/OnePochiPlayground"
                             "（Git 管理外のため、ダウンロードした環境では指定が必要）")
    args = parser.parse_args(argv)

    _configure_stdout()
    if args.project:
        target = args.project.strip().strip('"')
        if os.path.isfile(target):
            target = os.path.dirname(os.path.abspath(target))
        set_project(target)
    if not os.path.isdir(PROJECT):
        _print("[エラー] UE プロジェクトが見つかりません: {0}".format(PROJECT))
        _print("  --project \"<.uproject またはプロジェクトフォルダ>\" で指定してください。")
        _print("  設定を入れるには: python tools/setup_ue_project.py \"<.uproject>\"")
        return 2
    _print("")
    _print("ワンポチツール / Remote Control 設定診断")
    _print("  プロジェクト: {0}".format(PROJECT))
    _print("")

    rows, problems, raw = inspect_inis()
    print_ini_table(rows)

    if args.ini_only:
        _print("=" * 78)
        if problems:
            _print("△ 設定ファイルに問題があります:")
            for problem in problems:
                _print("   ・{0}".format(problem))
            code = 1
        else:
            _print("✔ 設定ファイルは期待どおりです（実際に通るかは UE 起動中に確認）")
            code = 0
        _print("")
        return code

    stages = probe()
    print_probe(stages)

    code, lines = diagnose(stages, problems)
    _print("=" * 78)
    _print("[3] 診断")
    _print("=" * 78)
    for line in lines:
        _print("  {0}".format(line))
    _print("")

    if args.json:
        payload = {
            "project": PROJECT,
            "ini": {
                "defaultRemoteControlPath": DEFAULT_RC_INI,
                "savedPath": SAVED_RC_INI,
                "defaultEnginePath": DEFAULT_ENGINE_INI,
                "section": SECTION,
                "values": raw,
            },
            "problems": problems,
            "stages": stages,
            "exitCode": code,
            "diagnosis": lines,
        }
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        _print("  レポート: {0}".format(args.json))
        _print("")
    return code


if __name__ == "__main__":
    sys.exit(main())
