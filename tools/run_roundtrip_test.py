"""Blender → FBX → UE の往復テスト（E2E 回帰テスト）。

    python tools/run_roundtrip_test.py                 # 全サンプル（commandlet）
    python tools/run_roundtrip_test.py 01_static_minimal
    python tools/run_roundtrip_test.py --keep           # UE のアセットを消さない
    python tools/run_roundtrip_test.py --via-rc         # 起動中の UE に Remote Control で依頼

やること:
    1. `/Game/_Dev/Samples` 配下の既存アセットを削除してクリーンにする
    2. サンプルごとに Blender で書き出す（サンプルごとに別の UE パスへ）
    3. UE にインポートさせる
       - 既定: UE を headless（commandlet）で起動する
       - `--via-rc`: **起動中の UE エディタ**に Remote Control API で依頼する
    4. `ue_report.json` の `comparison` を検査する
       — DCC 側の予測値と UE 側の実測値が一致しているか

**UE の終了コードは判定に使わない。**空プロジェクトでも
GameFeatureData の既知エラーで 1 になるため、必ずレポート JSON を見る。

`--via-rc` を使う理由（docs/99 P-16）:
    `UnrealEditor-Cmd.exe` は同じプロジェクトを別インスタンスで開くため、
    **GUI エディタが起動している間は commandlet を走らせられない**。
    Remote Control は同一プロセスなので競合しない。経路は Blender の
    「ワンポチ」ボタンと**完全に同じ**（`core/ue_bridge.py` を共有する）ので、
    本番経路の回帰テストとしても機能する。

    `--via-rc` には UE 側のセキュリティ設定が必要（docs/12 §2.6 / A-2）。
    足りない場合は `ue_bridge.classify_failure()` が日本語で何を ON にすれば
    よいか返すので、そのまま表示する。

環境変数:
    BLENDER_EXE ... blender.exe のパス（未設定なら自動検出）
    UE_ROOT     ... UE のインストール先（既定 C:\\Program Files\\Epic Games\\UE_5.8）
    ONEPOCHI_RC_URL ... Remote Control の URL（未設定ならプリセットの ue.remoteControlUrl）
"""

import argparse
import glob
import io
import json
import os
import shutil
import subprocess
import sys


def _force_utf8_stdout():
    """Windows の既定コンソール（cp932）でも日本語を出せるようにする。

    サブプロセスとして起動されると stdout が cp932 になり、`✔` などの文字で
    UnicodeEncodeError を出して**何も表示されずに死ぬ**。呼び出し側に
    `-X utf8` を要求せずに済むよう、ここで自衛する（docs/99 の既知の落とし穴を参照）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_stdout()

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

#: アドオンの親（`onepochi.core.ue_bridge` を取り込むため）。
#: `core/ue_bridge.py` は bpy に依存しないので、ここから直接使える。
ADDON_ROOT = os.path.join(HERE, "dcc_blender", "addons")
if ADDON_ROOT not in sys.path:
    sys.path.insert(0, ADDON_ROOT)

UE_PLUGIN_DIR = os.path.join(HERE, "ue_plugin")

#: サンプル名 -> UE の出力先。
#: サンプルは3つとも同じオブジェクト名 `Crate_01` なので、
#: **出力先を分けないと同じアセットに上書きされて計測が壊れる**。
SAMPLES = {
    "01_static_minimal": "/Game/_Dev/Samples/S01_Minimal",
    "02_static_collision": "/Game/_Dev/Samples/S02_Collision",
    "03_static_multislot": "/Game/_Dev/Samples/S03_Multislot",
    "04_skeletal_minimal": "/Game/_Dev/Samples/S04_Skeletal",
    "05_skeletal_shared_skeleton": "/Game/_Dev/Samples/S05_SharedSkeleton",
    "06_skeletal_morph": "/Game/_Dev/Samples/S06_Morph",
    "07_static_lod_fixed": "/Game/_Dev/Samples/S07_LOD",
    "08_static_socket": "/Game/_Dev/Samples/S08_Socket",
    "10_static_batch": "/Game/_Dev/Samples/S10_Batch",
    "11_static_texture": "/Game/_Dev/Samples/S11_Texture",
}

#: サンプル名 -> 書き出しの起点にするコレクション名。
#: **LOD はコレクション方式**（docs/04 §2.2）なので、選択ではなく
#: アセット用コレクションを起点にする必要がある（`--collection`）。
#:
#: **値にリストを書くとバッチ書き出し**（`--collections` / 1 マニフェストに
#: 複数アセット / N-5）になる。S-10 は 2 アセットが 1 本のマニフェストに入る
#: ので、`ue_report.json` の `assets` も 2 件返ってくる
#: （`check_report()` は `for asset in report["assets"]` で回しているので対応済み）
COLLECTIONS = {
    "07_static_lod_fixed": "Crate_01",
    "10_static_batch": ["Crate_01", "Barrel_01"],
}

#: **原本の `07_static_lod` は未登録のまま。**実測で3つの問題があるため（docs/14 §9）:
#:   1. LOD が `LOD0` / `LOD1` / `LOD2` コレクションではなく
#:      `Crate_01_LOD0/1/2` という**オブジェクト名**で表現されている
#:      → docs/04 §2.2 の規約と違うため、3つが1アセットに合成される
#:   2. 三角形数が LOD0 1280 / LOD1 **80** / LOD2 **320** で、
#:      LOD1 と LOD2 が入れ替わっている
#:   3. 未使用マテリアル `Material` が残っていて V-003c で止まる
#: 代わりに **`07_static_lod_fixed`**（`tools/make_sample_lod.py` が生成した
#: 規約どおりのサンプル / 1280 / 320 / 80）を登録している。
#: 原本を直すかどうかはユーザーの判断（A-10）。

#: サンプル名 -> assetType。省略時は StaticMesh
ASSET_TYPES = {
    "04_skeletal_minimal": "SkeletalMesh",
    "05_skeletal_shared_skeleton": "SkeletalMesh",
    "06_skeletal_morph": "SkeletalMesh",
}

#: サンプル名 -> プリセット。省略時は PRESET
PRESETS = {
    "04_skeletal_minimal": "character",
    "05_skeletal_shared_skeleton": "character",
    "06_skeletal_morph": "character",
    # S-11 はテクスチャの取り込みを確かめるサンプルなので、
    # `importTextures: true` を持つ専用プリセットを使う（M-3）。
    # `sample_debug` は `importTextures: false` なので、これを忘れると
    # **テクスチャが1枚も取り込まれないまま「成功」になる**
    "11_static_texture": "sample_texture",
}

PRESET = "sample_debug"
UPROJECT = os.path.join(ROOT, "UEProject", "OnePochiPlayground",
                        "OnePochiPlayground.uproject")
CONTENT_TEST_DIR = os.path.join(ROOT, "UEProject", "OnePochiPlayground",
                                "Content", "_Dev")

#: 「どの比較項目の一致を要求するか」は `onepochi.core.verify` が単一の出所。
#: こことボタン経路の結合テストの両方から使う（二重管理を避ける）
from onepochi.core import verify as verify_mod  # noqa: E402


def find_blender():
    candidate = os.environ.get("BLENDER_EXE")
    if candidate and os.path.isfile(candidate):
        return candidate
    patterns = (
        os.path.expanduser(r"~\Documents\Blender\stable\blender-*\blender.exe"),
        r"C:\Program Files\Blender Foundation\Blender *\blender.exe",
    )
    for pattern in patterns:
        matches = sorted(glob.glob(pattern), reverse=True)
        if matches:
            return matches[0]
    return None


def find_ue_cmd():
    root = os.environ.get("UE_ROOT") or r"C:\Program Files\Epic Games\UE_5.8"
    path = os.path.join(root, "Engine", "Binaries", "Win64", "UnrealEditor-Cmd.exe")
    return path if os.path.isfile(path) else None


def sample_paths(name):
    folder = os.path.join(ROOT, "samples", "blender", name)
    return {
        "blend": os.path.join(folder, name + ".blend"),
        "manifest": os.path.join(folder, "fbx", "onepochi.json"),
        "report": os.path.join(folder, "fbx", "ue_report.json"),
    }


def export_sample(blender, name, ue_path):
    paths = sample_paths(name)
    command = [
        blender, "--background", paths["blend"],
        "--python", os.path.join(HERE, "dcc_blender", "run_headless.py"), "--",
        "--mode", "export",
        "--preset", PRESETS.get(name, PRESET),
        "--asset-type", ASSET_TYPES.get(name, "StaticMesh"),
        "--ue-path", ue_path,
    ]
    collection = COLLECTIONS.get(name)
    if isinstance(collection, (list, tuple)):
        # バッチ書き出し（1 マニフェストに複数アセット / N-5）
        command.extend(["--collections", ",".join(collection)])
    elif collection:
        command.extend(["--collection", collection])
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    ok = os.path.isfile(paths["manifest"]) and result.returncode == 0
    return ok, result.stdout + result.stderr


def import_sample(ue_cmd, name):
    paths = sample_paths(name)
    job = {
        "manifest": paths["manifest"].replace("\\", "/"),
        "report": paths["report"].replace("\\", "/"),
        "dryRun": False,
        "allowFailed": False,
    }
    job_path = os.path.join(HERE, "ue_plugin", "job.json")
    with io.open(job_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(job, handle, ensure_ascii=False, indent=2)

    if os.path.isfile(paths["report"]):
        os.remove(paths["report"])

    command = [
        ue_cmd, UPROJECT, "-run=pythonscript",
        "-script=" + os.path.join(HERE, "ue_plugin", "run_import.py"),
        "-unattended", "-nopause", "-nosplash", "-stdout",
    ]
    subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                   encoding="utf-8", errors="replace")

    if not os.path.isfile(paths["report"]):
        return None
    with io.open(paths["report"], encoding="utf-8") as handle:
        return json.load(handle)


# --- Remote Control 経路（--via-rc / 起動中の UE エディタに依頼する） -----------
#
# Blender の「ワンポチ」ボタンと**同じ** `core/ue_bridge.py` を使う。
# ここで独自の HTTP を書くと本番経路の回帰テストにならない（方針 D-1）。


def rc_bridge():
    """`onepochi.core.ue_bridge` を取り込む（import できなければ None）。"""
    try:
        from onepochi.core import ue_bridge
    except ImportError as error:
        print("  ✖ ue_bridge を読み込めません: {0}".format(error))
        print("    期待した場所: {0}".format(ADDON_ROOT))
        return None
    return ue_bridge


def rc_settings(ue_bridge):
    """接続設定を決める。プリセットの `ue` ブロック → 環境変数で上書き（D-2）。"""
    settings = {}
    try:
        from onepochi.core import presets as presets_mod
        settings = presets_mod.resolve_preset(PRESET)
    except Exception as error:  # noqa: BLE001
        print("  ‥ プリセット {0} を読めませんでした（既定値を使います）: {1}".format(
            PRESET, error))
    connection = ue_bridge.settings_from_preset(settings)
    override = os.environ.get("ONEPOCHI_RC_URL")
    if override:
        connection["base_url"] = override
    connection["plugin_dir"] = UE_PLUGIN_DIR
    return connection


def rc_execute(ue_bridge, connection, statement, timeout=60.0, label=""):
    """UE 側で Python 文を1つ実行する。成功なら True。

    失敗は `BridgeError` の日本語メッセージをそのまま出す
    （どの設定が足りないかまで書かれている）。
    """
    try:
        status, body = ue_bridge.execute_python(
            statement, base_url=connection["base_url"], timeout=timeout)
    except ue_bridge.BridgeError as error:
        if label:
            print("  ✖ {0}".format(label))
        for line in error.lines():
            print("    " + line)
        return False
    for line in ue_bridge._extract_log(body):
        if "[Error]" in line or "Traceback" in line:
            print("    " + line)
    return 200 <= status < 300


def rc_reset_modules(ue_bridge, connection):
    """UE の Python セッションに残っている ワンポチ のモジュールを捨てる。

    UE エディタは起動したままなので、`onepochi_ue.*` を一度 import すると
    **コードを直しても古いものが使われ続ける**。`importlib.reload` は
    `onepochi_rc` にしか効かない（`onepochi_ue` は `handle()` の中で
    import されるため）。確実なのは `sys.modules` から外すこと。
    """
    statement = ("import sys; "
                 "[sys.modules.pop(k) for k in list(sys.modules) "
                 "if k == 'onepochi_rc' or k.startswith('onepochi_ue')]")
    return rc_execute(ue_bridge, connection, statement, timeout=30.0,
                      label="UE 側モジュールの再読み込みに失敗しました")


def rc_clean_assets(ue_bridge, connection, ue_paths):
    """UE 側のアセットを **UE 自身の API で** 削除する。

    エディタが開いている間に `Content/_Dev` をファイルシステムから消すと、
    メモリ上のパッケージと食い違って事故る（X-6 / 保存時に復活しうる）。
    必ず `EditorAssetLibrary.delete_directory()` を使う。
    """
    quoted = ", ".join('"{0}"'.format(p) for p in ue_paths)
    statement = (
        "import unreal; "
        "_p = [{0}]; "
        "[unreal.EditorAssetLibrary.delete_directory(d) "
        "for d in _p if unreal.EditorAssetLibrary.does_directory_exist(d)]"
    ).format(quoted)
    return rc_execute(ue_bridge, connection, statement, timeout=120.0,
                      label="UE の検証用アセットを削除できませんでした")


def import_sample_via_rc(ue_bridge, connection, name):
    """起動中の UE エディタに Remote Control で取り込みを依頼する。

    戻り値は `check_report()` に渡せるレポート dict（取れなければ None）。
    """
    paths = sample_paths(name)
    try:
        result = ue_bridge.request_import(
            connection["plugin_dir"],
            paths["manifest"],
            paths["report"],
            dry_run=False,
            allow_failed=False,
            base_url=connection["base_url"],
            timeout=connection["timeout"],
        )
    except ue_bridge.BridgeError as error:
        for line in error.lines():
            print("  " + line)
        return None
    for message in _interesting(result.get("messages", [])):
        print("  ‥ {0}".format(message))
    return result.get("report")


#: UE のログから拾いたくない行。`handle()` は print と `unreal.log` の両方に
#: 出すので同じ行が2回来るうえ、最後にレポート dict 全体が返ってくる。
#: **レポートは JSON から読む**ので、ここでは要らない（docs/99 P-7）。
_NOISE = ("Remote Control から依頼を受けました", "マニフェスト:", "レポート:",
          "dryRun=", "result=", "'toolVersion'", "<module ")


def _interesting(messages):
    """表示する価値のあるログ行だけを残す（重複と dict のエコーを捨てる）。"""
    kept = []
    for message in messages:
        if any(needle in message for needle in _NOISE):
            continue
        if message in kept:
            continue
        kept.append(message)
    return kept


def check_report(report):
    """(ok, 行のリスト) を返す。"""
    lines = []
    if report is None:
        return False, ["  ✖ ue_report.json が生成されていません"]

    ok = report.get("result") == "success"
    if not ok:
        lines.append("  ✖ result = {0}".format(report.get("result")))
    for error in report.get("errors", []):
        lines.append("  ✖ {0}".format(error))

    for asset in report.get("assets", []):
        lines.append("  アセット: {0} ({1})".format(asset.get("assetPath"),
                                                    asset.get("action")))
        if asset.get("skeletonCompatibility"):
            verdict = asset["skeletonCompatibility"]
            lines.append("  Skeleton 互換: {0}（{1} / {2}）".format(
                "OK" if verdict.get("compatible") else "NG",
                verdict.get("method"),
                " / ".join(verdict.get("reasons") or [])))
        if asset.get("pipeline", {}).get("failed"):
            lines.append("  ‥ パイプライン設定の失敗: {0}".format(
                asset["pipeline"]["failed"]))

        asset_ok, asset_lines = verify_mod.check_asset(asset)
        ok = ok and asset_ok
        lines.extend("  " + line for line in asset_lines)
    return ok, lines


def main(argv):
    parser = argparse.ArgumentParser(prog="run_roundtrip_test.py")
    parser.add_argument("samples", nargs="*", help="省略時は全サンプル")
    parser.add_argument("--keep", action="store_true",
                        help="UE の検証用アセットを削除しない")
    parser.add_argument("--skip-export", action="store_true",
                        help="Blender の書き出しを行わず既存のマニフェストを使う")
    parser.add_argument("--via-rc", action="store_true",
                        help="UE を起動せず、起動中の UE エディタに "
                             "Remote Control API で依頼する（docs/99 P-16）")
    args = parser.parse_args(argv)

    targets = args.samples or list(SAMPLES)
    unknown = [t for t in targets if t not in SAMPLES]
    if unknown:
        print("不明なサンプル: {0}".format(unknown))
        print("指定できるのは: {0}".format(list(SAMPLES)))
        return 2

    blender = find_blender()
    if not args.skip_export and blender is None:
        print("blender.exe が見つかりません。BLENDER_EXE を設定してください")
        return 2
    if not os.path.isfile(UPROJECT):
        print(".uproject が見つかりません: {0}".format(UPROJECT))
        return 2

    ue_bridge = None
    connection = None
    ue_cmd = None
    if args.via_rc:
        ue_bridge = rc_bridge()
        if ue_bridge is None:
            return 2
        connection = rc_settings(ue_bridge)
        print("Blender: {0}".format(blender))
        print("UE:      Remote Control {0}（起動中のエディタに依頼）".format(
            connection["base_url"]))
        if not ue_bridge.is_reachable(connection["base_url"]):
            print("  ✖ UE に接続できません: {0}".format(connection["base_url"]))
            print("    1. Unreal Editor が起動しているか")
            print("    2. Remote Control API プラグインが有効か")
            print("    3. ポート番号（既定 30010）")
            return 2
        if not rc_reset_modules(ue_bridge, connection):
            return 1
    else:
        ue_cmd = find_ue_cmd()
        if ue_cmd is None:
            print("UnrealEditor-Cmd.exe が見つかりません。UE_ROOT を設定してください")
            return 2
        print("Blender: {0}".format(blender))
        print("UE:      {0}".format(ue_cmd))

    if not args.keep:
        if args.via_rc:
            # エディタが開いているのでファイルを直接消さない（X-6）
            doomed = [SAMPLES[name] for name in targets]
            print("検証用アセットを UE 側で削除: {0}".format(", ".join(doomed)))
            if not rc_clean_assets(ue_bridge, connection, doomed):
                return 1
        elif os.path.isdir(CONTENT_TEST_DIR):
            print("検証用アセットを削除: {0}".format(CONTENT_TEST_DIR))
            shutil.rmtree(CONTENT_TEST_DIR, ignore_errors=True)

    failures = []
    for name in targets:
        ue_path = SAMPLES[name]
        print("\n" + "=" * 68)
        print("[{0}] -> {1}".format(name, ue_path))
        print("=" * 68)

        if not args.skip_export:
            ok, output = export_sample(blender, name, ue_path)
            if not ok:
                print("  ✖ Blender の書き出しに失敗しました")
                for line in output.splitlines():
                    if "[ワンポチ]" in line or "[V-" in line:
                        print("    " + line)
                failures.append(name + " (export)")
                continue
            print("  ✔ 書き出し成功")

        if args.via_rc:
            report = import_sample_via_rc(ue_bridge, connection, name)
        else:
            report = import_sample(ue_cmd, name)
        ok, lines = check_report(report)
        for line in lines:
            print(line)
        if not ok:
            failures.append(name + " (import)")

    print("\n" + "=" * 68)
    if failures:
        print("往復テスト失敗 {0} 件: {1}".format(len(failures), ", ".join(failures)))
        return 1
    print("往復テスト成功: {0} サンプルすべてで DCC の予測値と UE の実測値が一致".format(
        len(targets)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
