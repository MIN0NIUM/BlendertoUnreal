"""UE エディタのメニューから取り込みを実行する（P6-4）。

**Editor Utility Widget（Blueprint アセット）ではなく `ToolMenus` にした理由**:
Blueprint アセットを Python から組み立てるのは手順が不確実で、壊れたときに
原因が追いにくい。`ToolMenus` は実機でプロパティ名まで確認済み（下記）なので、
**確実なほうから倒す**。UEW が必要になったらこの関数を呼ぶだけにできる。

実機で確定した API（2026-10-11 / UE 5.8.3 / `docs/tmp/menu_probe.json`）:

    unreal.ToolMenus.get()                         -> ToolMenus
    .find_menu("LevelEditor.MainMenu.Tools")       -> ToolMenu（実在する）
    ToolMenu.add_section(section_name, label=...)  -> None
    ToolMenu.add_menu_entry(section_name, args: ToolMenuEntry) -> None
    ToolMenuEntry.set_string_command(type, custom_type, string) -> None
    unreal.ToolMenuStringCommandType.PYTHON        （実在する）
    unreal.MultiBlockType.MENU_ENTRY               （実在する）
    unreal.EditorDialog.show_message(title, message, message_type) -> AppReturnType

**通知 API は無い**（`EditorNotificationLibrary` / `AsyncTaskNotification` は
この環境の Python には公開されていない / 実測）。そのため結果は
`unreal.log` と**モーダルダイアログ**で出す。
ダイアログは人が押したときだけ出す（`interactive=True`）。
自動実行から呼ぶときは `interactive=False` にすること
（**モーダルはエディタを止めるので、Remote Control から出すと応答が返らない**）。

取り込むマニフェストは **`%LOCALAPPDATA%/OnePochi/runs/latest.json`** から解決する。
これは Blender 側の書き出し（`bl/pipeline.py` → `core/runs.py`）が毎回更新する。
"""

import io
import json
import os

try:
    import unreal
except ImportError:          # 単体テスト用（UE の外では import できない）
    unreal = None

from . import importer

#: メニューの登録先と識別子
MENU_PATH = "LevelEditor.MainMenu.Tools"
SECTION_NAME = "OnePochi"
SECTION_LABEL = "ワンポチツール"
ENTRY_NAME = "OnePochiImportLatest"
ENTRY_LABEL = "最後に書き出したものを取り込む"
ENTRY_TOOLTIP = ("Blender で最後に「ワンポチ」したマニフェストを取り込みます"
                 "（%LOCALAPPDATA%/OnePochi/runs/latest.json）")
OWNER_NAME = "OnePochi"

#: 実行ログの置き場（`core/runs.py` と同じ規則）。
#: **UE 側から Blender アドオンを import できない**ので、
#: ここだけは同じ規則を書き写している。**変えるときは両方直す**
RUNS_DIR_ENV = "ONEPOCHI_RUNS_DIR"
LATEST_NAME = "latest.json"


def runs_root(root=None):
    if root:
        return root
    env = os.environ.get(RUNS_DIR_ENV)
    if env:
        return env
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return os.path.join(base, "OnePochi", "runs")
    return os.path.join(os.path.expanduser("~"), ".onepochi", "runs")


def read_latest(root=None):
    """`runs/latest.json` を読む。無ければ None。"""
    path = os.path.join(runs_root(root), LATEST_NAME)
    if not os.path.isfile(path):
        return None
    try:
        with io.open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:  # noqa: BLE001
        return None


def _log(message):
    text = "[OnePochi] {0}".format(message)
    print(text)
    if unreal is not None:
        try:
            unreal.log(text)
        except Exception:  # noqa: BLE001
            pass


def _log_error(message):
    text = "[OnePochi] {0}".format(message)
    print(text)
    if unreal is not None:
        try:
            unreal.log_error(text)
        except Exception:  # noqa: BLE001
            pass


def _dialog(title, message):
    """モーダルダイアログ。**人が押したときだけ**呼ぶ。"""
    if unreal is None or not hasattr(unreal, "EditorDialog"):
        return None
    try:
        return unreal.EditorDialog.show_message(
            title, message, unreal.AppMsgType.OK)
    except Exception as error:  # noqa: BLE001
        _log_error("ダイアログを表示できませんでした: {0}".format(error))
        return None


def resolve_latest_manifest(root=None):
    """(マニフェストのパス, 日本語のエラー) を返す。"""
    latest = read_latest(root)
    if latest is None:
        return None, (
            "まだ書き出しの記録がありません。\n"
            "Blender の「ワンポチ」ボタンで一度書き出してください。\n"
            "（記録の場所: {0}）".format(os.path.join(runs_root(root), LATEST_NAME)))
    path = latest.get("manifestPath")
    if not path:
        return None, "書き出しの記録にマニフェストのパスが入っていません: {0}".format(latest)
    if not os.path.isfile(path):
        return None, (
            "記録されているマニフェストが見つかりません:\n{0}\n"
            "書き出し先のフォルダを移動・削除していないか確認してください。".format(path))
    return path, None


def import_latest(interactive=True, root=None):
    """最後に書き出したマニフェストを取り込む。

    戻り値はレポート辞書（`importer.import_from_manifest` のもの）か None。
    **例外を投げない。**メニューから呼ばれるので、失敗しても
    エディタにトレースバックを撒かないこと（日本語でログとダイアログに出す）。
    """
    manifest_path, error = resolve_latest_manifest(root)
    if manifest_path is None:
        _log_error(error)
        if interactive:
            _dialog("ワンポチツール", error)
        return None

    _log("取り込み開始: {0}".format(manifest_path))
    report_path = os.path.join(os.path.dirname(manifest_path), "ue_report.json")
    try:
        report = importer.import_from_manifest(manifest_path,
                                               report_path=report_path)
    except Exception as error:  # noqa: BLE001
        message = "取り込み中に予期しないエラーが発生しました: {0}: {1}".format(
            type(error).__name__, error)
        _log_error(message)
        if interactive:
            _dialog("ワンポチツール", message)
        return None

    lines = ["結果: {0}".format(report.get("result"))]
    for asset in report.get("assets") or []:
        lines.append("{0}  {1}".format(asset.get("action"),
                                       asset.get("assetPath") or
                                       asset.get("requestedName")))
    for message in (report.get("errors") or [])[:5]:
        lines.append("エラー: {0}".format(message))
    summary = "\n".join(lines)
    _log(summary.replace("\n", " / "))
    if interactive:
        _dialog("ワンポチツール", summary + "\n\nレポート: " + report_path)
    return report


def register_menu(force=False):
    """`Tools` メニューに項目を登録する。

    戻り値: (登録できたか, 日本語のメッセージ)。**例外を投げない。**
    `init_unreal.py` から呼ばれるので、失敗してもエディタの起動を妨げないこと。
    """
    if unreal is None:
        return False, "unreal モジュールが無い環境ではメニューを登録できません"
    if not hasattr(unreal, "ToolMenus"):
        return False, "unreal.ToolMenus がこの UE に存在しません"

    try:
        menus = unreal.ToolMenus.get()
        menu = menus.find_menu(MENU_PATH)
        if menu is None:
            return False, "メニューが見つかりません: {0}".format(MENU_PATH)

        menu.add_section(SECTION_NAME, SECTION_LABEL)

        entry = unreal.ToolMenuEntry(
            name=ENTRY_NAME,
            type=unreal.MultiBlockType.MENU_ENTRY,
        )
        entry.set_label(ENTRY_LABEL)
        entry.set_tool_tip(ENTRY_TOOLTIP)
        entry.set_string_command(
            unreal.ToolMenuStringCommandType.PYTHON, "",
            # **1行で書く**（UE は文字列をそのまま exec する）。
            # `interactive=True` なので結果はダイアログで出る
            "import onepochi_ue.menu as _m; _m.import_latest()")
        menu.add_menu_entry(SECTION_NAME, entry)
        menus.refresh_all_widgets()
    except Exception as error:  # noqa: BLE001
        return False, "メニューの登録に失敗しました: {0}: {1}".format(
            type(error).__name__, error)

    return True, "メニューを登録しました: {0} > {1} > {2}".format(
        MENU_PATH, SECTION_LABEL, ENTRY_LABEL)
