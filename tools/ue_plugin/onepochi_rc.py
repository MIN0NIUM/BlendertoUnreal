"""UE 側の受け口（Remote Control API からの呼び出し先 / docs/02_仕様書.md §2.1）。

Blender から Remote Control API 経由で1行の Python 文が送られてきて、
最終的にここの `handle()` が呼ばれる。

    onepochi_rc.handle(manifest, report, dry_run, allow_failed)

**ここは「薄い受け口」に徹する。**取り込みの本体は `onepochi_ue.importer`
にあり、起動方法（commandlet / Remote Control / UE のコンソール）に依存しない
純粋な関数になっている（設計方針 D-1）。

**例外を外に投げない。**失敗しても必ずレポート JSON を書いて返す。
HTTP のレスポンスだけが頼りになると、Blender 側で原因が分からなくなるため
（docs/99 P-6 / P-7 の教訓）。
"""

import io
import json
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _emit(message):
    """UE の Output Log と標準出力の両方に出す。"""
    text = "[OnePochi] {0}".format(message)
    print(text)
    try:
        import unreal
        unreal.log(text)
    except Exception:  # noqa: BLE001
        pass


def _default_report_path(manifest_path):
    return os.path.join(os.path.dirname(os.path.abspath(manifest_path)),
                        "ue_report.json")


def _write_failure_report(report_path, errors):
    """取り込みに入る前に落ちた場合でも、同じ形のレポートを書く。

    Blender 側は `result` と `errors` だけ見れば済むようにする。
    """
    report = {
        "result": "failed",
        "trigger": "remoteControl",
        "errors": list(errors),
        "warnings": [],
        "assets": [],
    }
    try:
        directory = os.path.dirname(os.path.abspath(report_path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with io.open(report_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    except Exception as error:  # noqa: BLE001
        _emit("レポートの書き出しにも失敗しました: {0}: {1}".format(
            type(error).__name__, error))
    return report


def handle(manifest, report="", dry_run=False, allow_failed=False):
    """マニフェストを取り込む。戻り値はレポート dict。

    `report` が空文字のときはマニフェストの隣に `ue_report.json` を書く
    （Remote Control からは引数を省略できないため、空文字を「省略」とみなす）。
    """
    report_path = report or _default_report_path(manifest)

    _emit("Remote Control から依頼を受けました")
    _emit("  マニフェスト: {0}".format(manifest))
    _emit("  レポート:     {0}".format(report_path))
    _emit("  dryRun={0} allowFailed={1}".format(bool(dry_run), bool(allow_failed)))

    if not manifest or not os.path.isfile(manifest):
        message = "マニフェストが見つかりません: {0}".format(manifest)
        _emit("ERROR: " + message)
        return _write_failure_report(report_path, [message])

    try:
        from onepochi_ue import importer
    except Exception as error:  # noqa: BLE001
        message = ("UE 側モジュール onepochi_ue の読み込みに失敗しました: "
                   "{0}: {1}".format(type(error).__name__, error))
        _emit("ERROR: " + message)
        _emit(traceback.format_exc())
        return _write_failure_report(report_path, [message, "sys.path[0]={0}".format(HERE)])

    try:
        result = importer.import_from_manifest(
            manifest,
            allow_failed=bool(allow_failed),
            dry_run=bool(dry_run),
            report_path=report_path,
        )
    except Exception as error:  # noqa: BLE001
        message = "取り込み中に予期しない例外: {0}: {1}".format(
            type(error).__name__, error)
        _emit("ERROR: " + message)
        _emit(traceback.format_exc())
        return _write_failure_report(report_path,
                                     [message, traceback.format_exc()])

    result.setdefault("trigger", "remoteControl")
    _emit("result={0}".format(result.get("result")))
    for error in result.get("errors", []):
        _emit("ERROR: {0}".format(error))
    for warning in result.get("warnings", []):
        _emit("WARN:  {0}".format(warning))
    return result


def ping():
    """疎通確認用。Remote Control の設定が通っているかだけを確かめる。"""
    _emit("ping OK")
    return True
