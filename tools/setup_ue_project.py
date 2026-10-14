# -*- coding: utf-8 -*-
"""UE プロジェクトを「ワンポチツールが使える状態」にする（A-19 / docs/99 P-29）。

ダウンロードした人が、**自分の** UE プロジェクトに Remote Control の設定を入れるためのツール。
既存の内容は残したまま、足りないものだけを足す。

やること:

    1. `<プロジェクト>/Config/DefaultRemoteControl.ini` に
       Remote Control の Python 実行許可を書く
    2. `<プロジェクト>.uproject` の Plugins に
       `PythonScriptPlugin` / `RemoteControl` を明示的に有効化する（`--no-plugins` で省略）

やらないこと:

    - **緩めない。** `bAllowAnyRemoteFunctionCall` などの安全側の設定がすでに別の値でも、
      書き換えずに警告だけ出す（ほかの用途で使っているかもしれないため）
    - `Saved/` には触らない（UE が管理するユーザー単位の設定。上書きされていれば警告する）
    - 書き換える前に、必ず `<ファイル名>.bak-<日時>` を横に残す

使い方:

    python tools/setup_ue_project.py <.uproject またはプロジェクトフォルダ>
    python tools/setup_ue_project.py <...> --dry-run     # 変更内容を見るだけ（何も書かない）
    python tools/setup_ue_project.py <...> --yes         # 確認を省略する
    python tools/setup_ue_project.py <...> --no-plugins  # .uproject は触らない

Windows では `tools/setup_ue_project.bat` に `.uproject` をドラッグ&ドロップしてもよい。

終了コード:
    0 ... 完了 / すでに設定済み / `--dry-run`
    1 ... 確認で中止した
    2 ... 入力の誤り・読み書きの失敗（何も書き換えていない）

**書いたあとに UE エディタを再起動すること。** `.ini` は起動時にしか読まれない（P-29）。
"""

import argparse
import datetime
import difflib
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import check_rc_config  # noqa: E402  設定キーの正と、cp932 対策の出力ヘルパを共有する

SECTION = check_rc_config.SECTION

#: これが入っていないと Python が実行できない。**値が違えば直す**（このツールの目的）
REQUIRED_SCALARS = (
    ("bEnableRemotePythonExecution", "True"),
)

#: 安全側の推奨値。**無いときだけ足す**。別の値になっていても書き換えない（緩めない / 勝手に締めない）
RECOMMENDED_SCALARS = (
    ("bRestrictServerAccess", "True"),
    ("bAllowAnyRemoteFunctionCall", "False"),
    ("bAllowConsoleCommandRemoteExecution", "False"),
    ("bSecuritySettingsReviewed", "True"),
)

#: ExecutePythonCommandEx を名指しで許可する1行（許可範囲を1関数に絞る）
ALLOW_KEY = "CustomAllowedRemoteFunctionCalls"
ALLOW_LINE = (
    '+CustomAllowedRemoteFunctionCalls=(ClassPath="/Script/PythonScriptPlugin.PythonScriptLibrary",'
    'FunctionName=("ExecutePythonCommandEx"),bAllowChildClasses=False)'
)
#: 書式が違っても「すでに許可されている」と判断するための目印（両方を含む行があれば足さない）
ALLOW_MARKERS = ("PythonScriptLibrary", "ExecutePythonCommandEx")

#: `.uproject` で明示的に有効化するプラグイン（名前は `.uplugin` のファイル名と同じ）
PLUGINS = ("PythonScriptPlugin", "RemoteControl")

NEW_FILE_HEADER = (
    "; ワンポチツール / Remote Control 設定（tools/setup_ue_project.py が作成）",
    "; -----------------------------------------------------------------------------",
    "; Blender の「ワンポチ」ボタンは、UE の Remote Control 経由で Python を実行する。",
    "; この許可を Saved/Config（ユーザー単位）に置くとエディタ再起動で失われるため、",
    "; プロジェクト側のこのファイルに固定する。",
    ";",
    "; このファイル名でなければならない理由:",
    ";   URemoteControlSettings は UCLASS(config = RemoteControl) なので",
    ";   DefaultEngine.ini に書いても無視される（docs/99 P-29）。",
    ";",
    "; 緩めていないもの:",
    ";   bAllowAnyRemoteFunctionCall は False のまま（何でも呼べる状態にしない）",
    ";   許可する関数は ExecutePythonCommandEx の1件だけ",
    "",
)


class SetupError(Exception):
    """利用者へそのまま見せてよい日本語のエラー。"""


# --- 入力の解決 ---------------------------------------------------------------


def resolve_uproject(path):
    """`.uproject` のパスを返す。フォルダが渡されたら中の1個を探す。"""
    if not path or not path.strip().strip('"'):
        raise SetupError("UE プロジェクトのパスを指定してください（.uproject またはプロジェクトフォルダ）")
    path = os.path.abspath(path.strip().strip('"'))
    if os.path.isfile(path):
        if not path.lower().endswith(".uproject"):
            raise SetupError("`.uproject` ではないファイルが指定されました: {0}".format(path))
        return path
    if os.path.isdir(path):
        found = sorted(name for name in os.listdir(path) if name.lower().endswith(".uproject"))
        if len(found) == 1:
            return os.path.join(path, found[0])
        if not found:
            raise SetupError("フォルダの中に .uproject が見つかりません: {0}".format(path))
        raise SetupError("フォルダの中に .uproject が複数あります。ファイルを直接指定してください: {0}"
                         .format(", ".join(found)))
    raise SetupError("パスが見つかりません: {0}".format(path))


# --- ファイル入出力 -----------------------------------------------------------


def read_text(path):
    """(本文, BOM の有無) を返す。ファイルが無ければ (None, False)。"""
    if not os.path.isfile(path):
        return None, False
    with open(path, "rb") as handle:
        raw = handle.read()
    try:
        return raw.decode("utf-8-sig"), raw.startswith(b"\xef\xbb\xbf")
    except UnicodeDecodeError:
        raise SetupError("UTF-8 として読めませんでした。文字化けを防ぐため変更しません: {0}".format(path))


def write_text(path, text, bom):
    """一時ファイルに書いてから置き換える（書き込み途中で壊さない）。"""
    data = text.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    temp = path + ".tmp"
    with open(temp, "wb") as handle:
        handle.write(data)
    os.replace(temp, path)


# --- DefaultRemoteControl.ini のマージ ---------------------------------------


def _as_bool(value):
    text = value.strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    return None


def _same_bool(actual, expected):
    return _as_bool(actual) is not None and _as_bool(actual) == _as_bool(expected)


def _scan_section(lines):
    """対象セクションの `(行番号, キー, 値)` と、挿入位置（最後の出現の末尾）を返す。

    同じセクションが複数回出ても UE は合算して読むので、全部を拾う。
    """
    entries = []
    insert_at = None
    current = None
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1]
            if current == SECTION:
                insert_at = index + 1
            continue
        if current != SECTION or not stripped:
            continue
        insert_at = index + 1
        if stripped.startswith(";") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        entries.append((index, key.strip(), value.strip()))
    return entries, insert_at


def merge_remote_control_ini(text):
    """`DefaultRemoteControl.ini` に必要な設定を足す。

    `text` が None のときは新規作成。`(新しい本文, 変更点, 警告)` を返す。
    **UE は同じキーが複数あると最後を採用する**ので、値の判定も書き換えも最後の行に対して行う。
    """
    changes = []
    warnings = []
    if text is None:
        lines = list(NEW_FILE_HEADER)
        newline = "\n"
    else:
        lines = text.splitlines()
        newline = "\r\n" if "\r\n" in text else "\n"

    entries, insert_at = _scan_section(lines)

    def last_of(name):
        found = [item for item in entries if item[1].lstrip("+-.!") == name and
                 not item[1].startswith("-")]
        return found[-1] if found else None

    to_insert = []

    for name, expected in REQUIRED_SCALARS:
        current = last_of(name)
        if current is None:
            to_insert.append("{0}={1}".format(name, expected))
            changes.append("追加: {0}={1}".format(name, expected))
        elif not _same_bool(current[2], expected):
            lines[current[0]] = "{0}={1}".format(name, expected)
            changes.append("変更: {0}  {1} → {2}".format(name, current[2], expected))

    for name, expected in RECOMMENDED_SCALARS:
        current = last_of(name)
        if current is None:
            to_insert.append("{0}={1}".format(name, expected))
            changes.append("追加: {0}={1}".format(name, expected))
        elif not _same_bool(current[2], expected):
            warnings.append(
                "{0} が {1} です（推奨は {2}）。ほかの用途で使っているかもしれないので変更していません"
                .format(name, current[2], expected))

    allowed = any(
        item[1].lstrip("+-.!") == ALLOW_KEY and not item[1].startswith("-") and
        all(marker in item[2] for marker in ALLOW_MARKERS)
        for item in entries)
    if not allowed:
        to_insert.append(ALLOW_LINE)
        changes.append("追加: +CustomAllowedRemoteFunctionCalls（ExecutePythonCommandEx だけを許可）")

    if to_insert:
        if insert_at is None:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append("[{0}]".format(SECTION))
            lines.extend(to_insert)
        else:
            lines[insert_at:insert_at] = to_insert

    if not changes:
        return text, changes, warnings
    return newline.join(lines) + newline, changes, warnings


# --- .uproject のマージ -------------------------------------------------------


def merge_uproject(text, plugins=PLUGINS):
    """`.uproject` の Plugins に有効化を足す。`(新しい本文, 変更点)` を返す。

    変更が無いときは**本文をそのまま返す**（再整形で差分を出さない）。
    """
    try:
        data = json.loads(text)
    except ValueError as error:
        raise SetupError(".uproject が JSON として読めません: {0}".format(error))
    if not isinstance(data, dict):
        raise SetupError(".uproject の形式が想定と違います（最上位がオブジェクトではありません）")
    entries = data.setdefault("Plugins", [])
    if not isinstance(entries, list):
        raise SetupError(".uproject の Plugins が配列ではありません")

    changes = []
    for name in plugins:
        found = [item for item in entries if isinstance(item, dict) and item.get("Name") == name]
        if not found:
            entries.append({"Name": name, "Enabled": True})
            changes.append("追加: Plugins に {0}（Enabled: true）".format(name))
        elif found[-1].get("Enabled") is not True:
            found[-1]["Enabled"] = True
            changes.append("変更: {0} の Enabled を true に".format(name))

    if not changes:
        return text, changes
    newline = "\r\n" if "\r\n" in text else "\n"
    body = json.dumps(data, indent="\t", ensure_ascii=False).replace("\n", newline)
    if text.endswith(("\n", "\r")):
        body += newline
    return body, changes


# --- Saved 側の上書き検出 -----------------------------------------------------


def saved_override_warnings(project_dir):
    """`Saved/` 側（後から読まれて勝つ）が推奨と違う値を持っていないかを返す。"""
    path = os.path.join(project_dir, "Saved", "Config", "WindowsEditor", "RemoteControl.ini")
    values = check_rc_config.read_section(path, SECTION)
    if not values:
        return []
    warnings = []
    for name, expected in REQUIRED_SCALARS + RECOMMENDED_SCALARS:
        actual = values.get(name)
        if actual and not _same_bool(actual[-1], expected):
            warnings.append(
                "Saved 側の {0} が {1} です。Saved は後から読まれるので、書いた設定を打ち消します。\n"
                "      UE を閉じて {2} のその行を消してください".format(name, actual[-1], path))
    return warnings


# --- 計画の作成と適用 ---------------------------------------------------------


def build_plans(uproject, with_plugins=True):
    """書き換え計画を作る。**ここでは何も書かない。** `(計画のリスト, 警告)` を返す。"""
    project_dir = os.path.dirname(uproject)
    plans = []
    warnings = []

    ini_path = os.path.join(project_dir, "Config", "DefaultRemoteControl.ini")
    old, bom = read_text(ini_path)
    new, changes, ini_warnings = merge_remote_control_ini(old)
    warnings.extend(ini_warnings)
    plans.append({"path": ini_path, "old": old, "new": new, "bom": bom, "changes": changes})

    if with_plugins:
        old, bom = read_text(uproject)
        new, changes = merge_uproject(old)
        plans.append({"path": uproject, "old": old, "new": new, "bom": bom, "changes": changes})

    warnings.extend(saved_override_warnings(project_dir))
    return plans, warnings


def apply_plan(plan, stamp):
    """計画を書き込む。既存ファイルは先に `.bak-<日時>` へ複製する。バックアップ先を返す。"""
    path = plan["path"]
    backup = None
    if plan["old"] is not None:
        backup = "{0}.bak-{1}".format(path, stamp)
        shutil.copy2(path, backup)
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    write_text(path, plan["new"], plan["bom"])
    return backup


def _diff(plan):
    old = (plan["old"] or "").splitlines()
    new = plan["new"].splitlines()
    return list(difflib.unified_diff(
        old, new,
        fromfile="(新規作成)" if plan["old"] is None else plan["path"],
        tofile=plan["path"], lineterm="", n=1))


def _print(text=""):
    check_rc_config._print(text)


def main(argv=None, now=None):
    parser = argparse.ArgumentParser(
        prog="setup_ue_project.py",
        description="UE プロジェクトに、ワンポチツールが使う Remote Control の設定を入れる")
    parser.add_argument("project", nargs="?", default=None,
                        help=".uproject、またはそれが入っているプロジェクトフォルダ")
    parser.add_argument("--dry-run", action="store_true", help="変更内容を表示するだけで何も書かない")
    parser.add_argument("--yes", "-y", action="store_true", help="確認を省略して書き込む")
    parser.add_argument("--no-plugins", action="store_true", help=".uproject（プラグインの有効化）を触らない")
    args = parser.parse_args(argv)

    check_rc_config._configure_stdout()
    try:
        uproject = resolve_uproject(args.project)
        plans, warnings = build_plans(uproject, with_plugins=not args.no_plugins)
    except SetupError as error:
        _print("[エラー] {0}".format(error))
        return 2

    project_dir = os.path.dirname(uproject)
    _print("")
    _print("ワンポチツール / UE プロジェクトの設定")
    _print("  プロジェクト: {0}".format(uproject))
    _print("")

    changed = [plan for plan in plans if plan["changes"]]
    for plan in plans:
        state = "新規作成" if plan["old"] is None else ("変更あり" if plan["changes"] else "変更なし（設定済み）")
        _print("  {0}  [{1}]".format(os.path.relpath(plan["path"], project_dir), state))
        for change in plan["changes"]:
            _print("      ・{0}".format(change))
    _print("")

    for warning in warnings:
        _print("  △ {0}".format(warning))
    if warnings:
        _print("")

    if not changed:
        _print("✔ すでに設定済みです。何も書き換えません。")
        _print("  確認: python tools/check_rc_config.py --project \"{0}\"".format(project_dir))
        _print("")
        return 0

    _print("--- 変更内容 " + "-" * 60)
    for plan in changed:
        for line in _diff(plan):
            _print("  " + line)
    _print("")

    if args.dry_run:
        _print("（--dry-run のため、何も書き込んでいません）")
        _print("")
        return 0

    if not args.yes:
        if not sys.stdin or not sys.stdin.isatty():
            _print("[エラー] 対話できない環境です。内容を確認したうえで --yes を付けて再実行してください。")
            return 2
        answer = input("上の内容で書き換えます。元のファイルは .bak-日時 で残します。続けますか？ [y/N]: ")
        if answer.strip().lower() not in ("y", "yes"):
            _print("中止しました。何も書き換えていません。")
            return 1

    stamp = (now or datetime.datetime.now()).strftime("%Y%m%d-%H%M%S")
    try:
        for plan in changed:
            backup = apply_plan(plan, stamp)
            _print("  ✔ 書き込みました: {0}".format(plan["path"]))
            if backup:
                _print("      元のファイル: {0}".format(backup))
    except OSError as error:
        _print("[エラー] 書き込みに失敗しました: {0}".format(error))
        return 2

    _print("")
    _print("次にやること:")
    _print("  1. UE エディタを再起動する（.ini は起動時にしか読まれません / docs/99 P-29）")
    _print("  2. UE が起動したら確認する:")
    _print("       python tools/check_rc_config.py --project \"{0}\"".format(project_dir))
    _print("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
