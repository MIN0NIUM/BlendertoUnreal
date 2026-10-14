"""UE エディタから叩く起動スクリプト（トリガー層のひとつ）。

UE のコンソールから:
    py "C:/.../tools/ue_plugin/run_import.py" --manifest "C:/.../fbx/onepochi.json"

`py` への引数渡しが使えない環境でも動くように、引数が無い場合は
**ジョブファイル** を順に探す。最初に見つかったものを使う。

    1. <このスクリプトと同じフォルダ>/job.json      ← 推奨。確実に読める
    2. %LOCALAPPDATA%/OnePochi/job.json
    3. <UE プロジェクト>/Saved/onepochi_job.json

ジョブファイルの内容:
    {
      "manifest": "C:/.../fbx/onepochi.json",
      "report":   "C:/.../fbx/ue_report.json",
      "dryRun": false,
      "allowFailed": false
    }

**探索結果は必ずログに出す。**「指定されていません」で黙って終わると
原因の切り分けができなくなるため（docs/99 の教訓）。
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

LOCAL_APP_DATA = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")


def _project_saved_dir():
    """UE プロジェクトの Saved フォルダ（取得できなければ None）。"""
    try:
        import unreal
        return os.path.abspath(unreal.Paths.project_saved_dir())
    except Exception:
        return None


def job_candidates():
    """ジョブファイルの候補パスを優先順に返す。"""
    candidates = [
        os.path.join(HERE, "job.json"),
        os.path.join(LOCAL_APP_DATA, "OnePochi", "job.json"),
    ]
    saved = _project_saved_dir()
    if saved:
        candidates.append(os.path.join(saved, "onepochi_job.json"))
    return candidates


def _emit(message):
    """UE のログと標準出力の両方に出す。"""
    print("[OnePochi] {0}".format(message))
    try:
        import unreal
        unreal.log("[OnePochi] {0}".format(message))
    except Exception:
        pass


def parse_argv(argv):
    job = {}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in ("--manifest", "--report"):
            if index + 1 < len(argv):
                job[token[2:]] = argv[index + 1]
                index += 2
                continue
        elif token == "--dry-run":
            job["dryRun"] = True
        elif token == "--allow-failed":
            job["allowFailed"] = True
        index += 1
    return job


def read_job_file(path):
    """ジョブファイルを読む。(dict or None, 理由) を返す。"""
    try:
        exists = os.path.isfile(path)
    except OSError as error:
        return None, "存在確認に失敗 ({0})".format(error)
    if not exists:
        return None, "なし"
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except Exception as error:  # noqa: BLE001
        return None, "読み込み失敗 ({0}: {1})".format(type(error).__name__, error)
    if not isinstance(data, dict):
        return None, "JSON オブジェクトではない"
    if not data.get("manifest"):
        return None, "manifest キーが無い"
    return data, "採用"


def resolve_job():
    """引数 → ジョブファイル候補の順に解決する。診断ログを必ず出す。"""
    argv = sys.argv[1:] if len(sys.argv) > 1 else []
    _emit("sys.argv = {0!r}".format(sys.argv))

    job = parse_argv(argv)
    if job.get("manifest"):
        _emit("引数からマニフェストを取得しました: {0}".format(job["manifest"]))
        return job

    for path in job_candidates():
        data, reason = read_job_file(path)
        _emit("ジョブファイル候補: {0} -> {1}".format(path, reason))
        if data is not None:
            merged = dict(data)
            merged.update(job)  # 引数で部分的に上書きできる
            return merged

    return job


def default_report_path(manifest_path):
    return os.path.join(os.path.dirname(os.path.abspath(manifest_path)), "ue_report.json")


def main():
    job = resolve_job()
    manifest = job.get("manifest")

    if not manifest:
        _emit("マニフェストのパスを特定できませんでした。")
        _emit('  方法1: py "{0}" --manifest <onepochi.json のパス>'.format(
            os.path.join(HERE, "run_import.py")))
        _emit("  方法2: 上の候補パスのいずれかに job.json を置く")
        return 2

    report_path = job.get("report") or default_report_path(manifest)
    _emit("マニフェスト: {0}".format(manifest))
    _emit("レポート:     {0}".format(report_path))

    from onepochi_ue import importer

    report = importer.import_from_manifest(
        manifest,
        allow_failed=bool(job.get("allowFailed")),
        dry_run=bool(job.get("dryRun")),
        report_path=report_path,
    )

    _emit("result={0} report={1}".format(report.get("result"), report_path))
    for error in report.get("errors", []):
        _emit("ERROR: {0}".format(error))
    return 0 if report.get("result") in ("success", "dryRun") else 1


if __name__ == "__main__":
    main()
