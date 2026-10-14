"""実行ログの保持（docs/13 P4-5 / docs/02 §13）。

書き出し 1 回ぶんを `%LOCALAPPDATA%/OnePochi/runs/<run_id>/` に集約し、
**既定 30 日より古いものを掃除する**。

なぜ必要か:

- マニフェストと FBX はアセットの隣（`<出力先>/fbx/`）に出る。これは**上書きされる**ため、
  「前回うまくいっていたときの値」が残らない。事故の原因調査ができない
- UE 側のボタン（P6-4）が「最後に書き出したマニフェスト」を知る必要がある
  → `runs/latest.json` を唯一の出所にする

このモジュールは **bpy / unreal に依存しない**（単体テスト可能）。
時刻は引数で注入できるようにして、掃除の判定をテストできるようにしている。
"""

import datetime
import io
import json
import os
import shutil

#: 既定の保持日数（docs/13 P4-5）
DEFAULT_RETENTION_DAYS = 30

#: `runs/` の下に作るファイル名
MANIFEST_COPY_NAME = "onepochi.json"
RESULT_NAME = "result.json"
LATEST_NAME = "latest.json"

#: 環境変数で置き場を差し替えられるようにする（R-1 と同じ思想）
ROOT_ENV = "ONEPOCHI_RUNS_DIR"

#: `make_run_id()` が作る形式: 20261011_013000_a1b2c3
_RUN_ID_LENGTH = len("20261011_013000_a1b2c3")


def runs_root(root=None):
    """実行ログの置き場を返す。

    優先順位:
        1. 引数 root
        2. 環境変数 ONEPOCHI_RUNS_DIR
        3. `%LOCALAPPDATA%/OnePochi/runs`（Windows 以外では `~/.onepochi/runs`）
    """
    if root:
        return root
    env = os.environ.get(ROOT_ENV)
    if env:
        return env
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return os.path.join(base, "OnePochi", "runs")
    return os.path.join(os.path.expanduser("~"), ".onepochi", "runs")


def parse_run_id_time(run_id):
    """run_id の先頭から日時を取り出す。読めなければ None。

    `core.manifest.make_run_id()` の形式（`%Y%m%d_%H%M%S_<6桁>`）に依存する。
    **形式が変わったらここも変える**（単体テストが検出する）。
    """
    if not run_id or len(run_id) < len("20261011_013000"):
        return None
    try:
        return datetime.datetime.strptime(run_id[:15], "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def record(run_id, manifest=None, manifest_path=None, result=None, root=None,
           extra_files=None):
    """1 回ぶんの実行ログを書いて、そのディレクトリを返す。

    manifest      ... マニフェストの辞書（あればコピーを保存する）
    manifest_path ... 実ファイルの場所（`latest.json` が指す先）
    result        ... 任意の結果辞書（issues のサマリなど）
    extra_files   ... 一緒に残したいファイルのパスのリスト（FBX は大きいので既定では残さない）

    **例外を投げない。**ログの保存に失敗しても書き出し自体は成功扱いにしたいので、
    失敗したら None を返す（呼び側が警告を積む）。
    """
    try:
        directory = os.path.join(runs_root(root), run_id)
        if not os.path.isdir(directory):
            os.makedirs(directory)

        if manifest is not None:
            _write_json(os.path.join(directory, MANIFEST_COPY_NAME), manifest)

        payload = dict(result or {})
        payload.setdefault("runId", run_id)
        if manifest_path:
            payload.setdefault("manifestPath", os.path.abspath(manifest_path))
        _write_json(os.path.join(directory, RESULT_NAME), payload)

        for path in extra_files or ():
            if path and os.path.isfile(path):
                shutil.copy2(path, os.path.join(directory,
                                                os.path.basename(path)))

        _write_json(os.path.join(runs_root(root), LATEST_NAME), {
            "runId": run_id,
            "runDir": directory,
            "manifestPath": os.path.abspath(manifest_path) if manifest_path else None,
            "recordedAt": _iso_now(),
        })
        return directory
    except Exception:  # noqa: BLE001
        return None


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


def list_runs(root=None):
    """`runs/` にある run_id を新しい順に返す。"""
    directory = runs_root(root)
    if not os.path.isdir(directory):
        return []
    names = []
    for entry in os.listdir(directory):
        if not os.path.isdir(os.path.join(directory, entry)):
            continue
        if parse_run_id_time(entry) is None:
            continue
        names.append(entry)
    return sorted(names, reverse=True)


def prune(retention_days=DEFAULT_RETENTION_DAYS, root=None, now=None,
          keep_minimum=1):
    """保持期間を過ぎた実行ログを削除し、削除した run_id のリストを返す。

    keep_minimum ... 期限切れでも**最低この件数は残す**（全部消えて調査材料が
                     無くなるのを防ぐ）。既定 1。

    `run_id` から日時を読めないディレクトリは**触らない**（人が置いたものを消さない）。
    """
    now = now or datetime.datetime.now()
    cutoff = now - datetime.timedelta(days=retention_days)
    directory = runs_root(root)
    removed = []

    candidates = list_runs(root)          # 新しい順
    protected = set(candidates[:max(0, keep_minimum)])

    for run_id in candidates:
        if run_id in protected:
            continue
        stamp = parse_run_id_time(run_id)
        if stamp is None or stamp >= cutoff:
            continue
        try:
            shutil.rmtree(os.path.join(directory, run_id))
            removed.append(run_id)
        except Exception:  # noqa: BLE001
            continue
    return removed


def _write_json(path, payload):
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2))


def _iso_now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")
