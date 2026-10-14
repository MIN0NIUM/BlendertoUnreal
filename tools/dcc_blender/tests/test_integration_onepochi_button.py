"""真のワンポチ（P2）の結合テスト — **UI ボタンそのもの**を叩く。

実行（UE エディタを起動したまま）:
    blender.exe --background <sample.blend> \
        --python tools/dcc_blender/tests/test_integration_onepochi_button.py -- \
        --ue-path /Game/_Dev/Samples/S01_OneClick [--preset sample_debug] \
        [--asset-type StaticMesh] [--report out.json]

なぜ `run_headless.py --mode export` ではダメか:
    あれは `pipeline.export_asset()` を直接呼ぶので、**UE への依頼
    （`ui._send_to_ue`）を通らない**。P2 の DoD は「ボタン1回で UE に
    アセットが生成される」なので、オペレータ `onepochi.export` を
    実際に実行しないと検証したことにならない。

    したがってこのテストは
        bpy.ops.onepochi.export()           ← アーティストが押すボタン
          └ pipeline.export_asset()
          └ ui._send_to_ue()
             └ core.ue_bridge.request_import()
                └ HTTP PUT /remote/object/call
                   └ onepochi_rc.handle()
                      └ onepochi_ue.importer.import_from_manifest()
    を**一度の呼び出しで**通す。

前提:
    - UE エディタが起動していること（Remote Control の設定込み / docs/12 §2.6）
    - UE が起動していない場合は**失敗ではなく skip**（終了コード 0）にする。
      CI で UE 無しでも回せるようにするため
"""

import argparse
import json
import os
import sys

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
#: アドオンの親 = tools/dcc_blender/addons（Blender の Script Directories 規約）
ADDON_ROOT = os.path.join(os.path.dirname(HERE), "addons")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
if ADDON_ROOT not in sys.path:
    sys.path.insert(0, ADDON_ROOT)

import onepochi  # noqa: E402
from onepochi.core import ue_bridge  # noqa: E402
from onepochi.core import verify as verify_mod  # noqa: E402


class Checker(object):
    def __init__(self):
        self.failures = []
        self.checks = 0

    def check(self, condition, message):
        self.checks += 1
        if not condition:
            self.failures.append(message)
        return bool(condition)

    def equal(self, actual, expected, message):
        return self.check(
            actual == expected,
            "{0}（期待: {1} / 実際: {2}）".format(message, expected, actual))


def parse_args(argv):
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = []
    parser = argparse.ArgumentParser(prog="test_integration_onepochi_button.py")
    parser.add_argument("--ue-path", required=True,
                        help="UE の出力先（サンプルごとに分けること / docs/99 P-8）")
    parser.add_argument("--preset", default="sample_debug")
    parser.add_argument("--asset-type", default="StaticMesh")
    parser.add_argument("--report", default=None)
    return parser.parse_args(argv)


def ensure_fbx_addon():
    if hasattr(bpy.types, "EXPORT_SCENE_OT_fbx"):
        return True
    try:
        import addon_utils
        addon_utils.enable("io_scene_fbx", default_set=False, persistent=False)
    except Exception:  # noqa: BLE001
        pass
    return hasattr(bpy.types, "EXPORT_SCENE_OT_fbx")


def main():
    args = parse_args(sys.argv)
    ensure_fbx_addon()

    # UE が居なければ skip（失敗にしない）
    if not ue_bridge.is_reachable():
        print("[ワンポチ/P2] UE に接続できないため skip します（{0}）".format(
            ue_bridge.DEFAULT_BASE_URL))
        print("[ワンポチ/P2] UE エディタを起動してから実行してください")
        sys.exit(0)

    # アドオンを「インストールせずに」登録する（bootstrap_addon と同じ手順）
    try:
        onepochi.unregister()
    except Exception:  # noqa: BLE001
        pass
    onepochi.register()

    checker = Checker()
    settings = bpy.context.scene.onepochi
    checker.check(settings is not None, "scene.onepochi が登録されていない")

    settings.preset = args.preset
    settings.asset_type = args.asset_type
    settings.ue_path_override = args.ue_path
    settings.auto_import_to_ue = True          # ★ これがワンポチの本体

    checker.check(hasattr(bpy.ops.onepochi, "export"),
                  "オペレータ onepochi.export が登録されていない")

    # ★ ボタン1回に相当する呼び出し
    status = bpy.ops.onepochi.export()
    checker.check("FINISHED" in status,
                  "onepochi.export が FINISHED を返さなかった: {0}".format(status))

    checker.check(bool(settings.last_manifest_path),
                  "マニフェストが書き出されていない")
    checker.equal(settings.last_ue_result, "success",
                  "UE への取り込みが成功していない（{0}）".format(
                      settings.last_ue_message))

    report_path = settings.last_ue_report_path
    ue_report = None
    if checker.check(bool(report_path) and os.path.isfile(report_path),
                     "ue_report.json が UI に渡っていない: {0}".format(report_path)):
        with open(report_path, "r", encoding="utf-8-sig") as handle:
            ue_report = json.load(handle)
        checker.equal(ue_report.get("result"), "success",
                      "ue_report.json の result が success でない")
        assets = ue_report.get("assets") or []
        checker.check(len(assets) >= 1, "UE 側でアセットが作られていない")
        for asset in assets:
            checker.check(str(asset.get("assetPath") or "").startswith(args.ue_path),
                          "アセットの出力先が指定と違う: {0}".format(
                              asset.get("assetPath")))
            # 2回目以降は `reimported` になる（既存アセットの上書き）
            checker.check(asset.get("action") in ("created", "reimported"),
                          "action が created / reimported でない: {0}".format(
                              asset.get("action")))
            # 一致を要求する項目は `core.verify` が決める（往復テストと共通）。
            # `vertexCount` のように FBX の往復で必ず増えるものは除外される
            asset_ok, asset_lines = verify_mod.check_asset(asset)
            for line in asset_lines:
                print("  " + line)
            checker.check(asset_ok, "比較に不一致があります: {0}".format(
                asset.get("assetPath")))

    report = {
        "blendFile": bpy.data.filepath,
        "blenderVersion": bpy.app.version_string,
        "uePath": args.ue_path,
        "preset": args.preset,
        "assetType": args.asset_type,
        "manifestPath": settings.last_manifest_path,
        "ueResult": settings.last_ue_result,
        "ueMessage": settings.last_ue_message,
        "ueReportPath": report_path,
        "ueAssets": [a.get("assetPath") for a in (ue_report or {}).get("assets", [])],
        "checks": checker.checks,
        "failures": checker.failures,
        "ok": not checker.failures,
    }

    print("\n" + "=" * 68)
    print("[ワンポチ/P2] ボタン経路の結合テスト: {0}".format(
        os.path.basename(bpy.data.filepath) or "(未保存)"))
    print("  チェック {0} 件 / 失敗 {1} 件".format(checker.checks,
                                                   len(checker.failures)))
    for asset_path in report["ueAssets"]:
        print("  UE アセット: {0}".format(asset_path))
    for failure in checker.failures:
        print("  ✖ {0}".format(failure))
    if not checker.failures:
        print("  ✔ ボタン1回で UE にアセットが生成されました")
    print("=" * 68)

    if args.report:
        directory = os.path.dirname(os.path.abspath(args.report))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with open(args.report, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        print("[ワンポチ/P2] レポート: {0}".format(args.report))

    sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
