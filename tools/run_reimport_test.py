"""再インポート時の設定保持（T-7 / `reimportPolicy`）の実機検証（N-2）。

    python tools/run_reimport_test.py --via-rc     # 起動中の UE エディタに依頼する

**何を確かめるか**（docs/13 P4 の DoD）:

1. 1 回目のインポートでアセットができる
2. **UE 側で手修正する**（Nanite を ON / ライトマップ解像度を 128 / 手でソケットを1個足す）
3. `reimportPolicy.mode = preserveSettings` で再インポート → **手修正が残る**
4. `override: ["nanite"]` で再インポート → **Nanite だけマニフェストの値に戻り、他は残る**

手修正を「UE 側で人がやったこと」の代理として扱う。
ソケットはマニフェストが**宣言していない名前**を足すので、
`postprocess.remove_existing_sockets()` の対象外になる（docs/99 P-17）。
これが消えてしまうなら、アーティストが UE で足したものを壊していることになる。

出力先は **`/Game/_Dev/Samples/S01_Reimport`**（docs/99 P-8: サンプルごとに分ける）。
UE エディタは**閉じずに**実行する（P-16 / `--via-rc`）。
"""

import argparse
import io
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import run_roundtrip_test as rt  # noqa: E402  （エクスポートと RC の経路を使い回す）

SAMPLE = "01_static_minimal"
UE_PATH = "/Game/_Dev/Samples/S01_Reimport"
ASSET_PATH = UE_PATH + "/SM_Crate_01"

#: 手で足すソケット。マニフェストが宣言しない名前にする（P-17）
HAND_SOCKET = "HandMade_DoNotTouch"
HAND_LIGHTMAP_RESOLUTION = 128

STEP_FILES = {
    "preserve": "onepochi_reimport_preserve.json",
    "override": "onepochi_reimport_override.json",
}


def _p(message=""):
    print(message)


# --- マニフェストの派生 -------------------------------------------------------


def write_variant(manifest_path, filename, policy):
    """`reimportPolicy` だけを差し替えたマニフェストを**同じフォルダに**書く。

    同じフォルダに置くのが重要: `sourceFile` はマニフェストからの相対パスなので、
    別の場所に書くと FBX が見つからなくなる（`manifest_io.resolve_source_file`）。
    """
    with io.open(manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    manifest["reimportPolicy"] = policy
    for asset in manifest.get("assets", []):
        asset.pop("reimportPolicy", None)   # アセット側の宣言がマニフェスト側に勝つため
    target = os.path.join(os.path.dirname(manifest_path), filename)
    with io.open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(manifest, ensure_ascii=False, indent=2))
    return target, manifest


def declared_nanite(manifest):
    assets = manifest.get("assets") or [{}]
    block = (assets[0].get("staticMesh") or {}).get("nanite") or {}
    return bool(block.get("enabled"))


# --- UE 側の操作 --------------------------------------------------------------


MUTATE_SCRIPT = '''
import unreal, json, io

path = "{asset}"
mesh = unreal.EditorAssetLibrary.load_asset(path)
out = {{"loaded": mesh is not None}}

if mesh is not None:
    settings = mesh.get_editor_property("nanite_settings")
    settings.set_editor_property("enabled", True)
    mesh.set_editor_property("nanite_settings", settings)
    mesh.set_editor_property("light_map_resolution", {lightmap})

    socket = unreal.StaticMeshSocket()
    socket.set_editor_property("socket_name", "{socket}")
    socket.set_editor_property("relative_location", unreal.Vector(10.0, 20.0, 30.0))
    mesh.add_socket(socket)

    unreal.EditorAssetLibrary.save_asset(path, only_if_is_dirty=False)

    out["naniteEnabled"] = bool(
        mesh.get_editor_property("nanite_settings").get_editor_property("enabled"))
    out["lightMapResolution"] = int(mesh.get_editor_property("light_map_resolution"))
    out["socketNames"] = [str(s.get_editor_property("socket_name"))
                          for s in mesh.get_sockets_by_tag("")]

with io.open(r"{report}", "w", encoding="utf-8") as handle:
    handle.write(json.dumps(out, ensure_ascii=False, indent=2))
print("[OnePochi] mutate: " + json.dumps(out, ensure_ascii=False))
'''


def mutate_in_ue(ue_bridge, connection, scratch_dir):
    """UE 側のアセットを「人が手で直した」状態にする。"""
    report_path = os.path.join(scratch_dir, "mutate.json").replace("\\", "/")
    script_path = os.path.join(scratch_dir, "_mutate.py")
    source = MUTATE_SCRIPT.format(asset=ASSET_PATH,
                                  lightmap=HAND_LIGHTMAP_RESOLUTION,
                                  socket=HAND_SOCKET, report=report_path)
    with io.open(script_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(source)

    statement = ("exec(compile(open(r'{0}', encoding='utf-8').read(), "
                 "'onepochi_mutate', 'exec'))").format(
                     script_path.replace("\\", "/"))
    if not rt.rc_execute(ue_bridge, connection, statement, timeout=120.0,
                         label="UE 側の手修正に失敗しました"):
        return None
    if not os.path.isfile(report_path):
        return None
    with io.open(report_path, encoding="utf-8") as handle:
        return json.load(handle)


def import_manifest_via_rc(ue_bridge, connection, manifest_path, report_path):
    try:
        result = ue_bridge.request_import(
            connection["plugin_dir"], manifest_path, report_path,
            dry_run=False, allow_failed=False,
            base_url=connection["base_url"], timeout=connection["timeout"],
        )
    except ue_bridge.BridgeError as error:
        for line in error.lines():
            _p("  " + line)
        return None
    for message in rt._interesting(result.get("messages", [])):
        _p("  ‥ {0}".format(message))
    return result.get("report")


def first_asset(report):
    assets = (report or {}).get("assets") or []
    return assets[0] if assets else {}


# --- 判定 ---------------------------------------------------------------------


def check(label, actual, expected, lines):
    ok = actual == expected
    lines.append({"check": label, "expected": expected, "actual": actual,
                  "ok": ok})
    _p("  {0} {1}: 期待 {2} / 実測 {3}".format(
        "✔" if ok else "✖", label, expected, actual))
    return ok


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="再インポート時の設定保持を実機で検証する")
    parser.add_argument("--via-rc", action="store_true",
                        help="起動中の UE エディタに Remote Control で依頼する")
    parser.add_argument("--report", default=os.path.join(
        ROOT, "docs", "tmp", "reimport_report.json"))
    parser.add_argument("--skip-export", action="store_true",
                        help="既存のマニフェストを使う（Blender を起動しない）")
    args = parser.parse_args(argv)

    if not args.via_rc:
        _p("✖ このテストは --via-rc 専用です。")
        _p("  UE エディタを起動したまま実行してください（docs/99 P-16）。")
        return 2

    scratch_dir = os.path.join(ROOT, "docs", "tmp")
    if not os.path.isdir(scratch_dir):
        os.makedirs(scratch_dir)

    ue_bridge = rt.rc_bridge()
    if ue_bridge is None:
        return 2
    connection = rt.rc_settings(ue_bridge)
    paths = rt.sample_paths(SAMPLE)

    results = {"sample": SAMPLE, "assetPath": ASSET_PATH, "steps": [],
               "checks": []}
    checks = results["checks"]

    # --- 0. 書き出し --------------------------------------------------------
    if not args.skip_export:
        blender = rt.find_blender()
        if blender is None:
            _p("✖ blender.exe が見つかりません（BLENDER_EXE で指定できます）")
            return 2
        _p("[1/6] Blender で書き出し -> {0}".format(UE_PATH))
        ok, log = rt.export_sample(blender, SAMPLE, UE_PATH)
        if not ok:
            _p("✖ 書き出しに失敗しました")
            _p(log[-2000:])
            return 1
    if not os.path.isfile(paths["manifest"]):
        _p("✖ マニフェストがありません: {0}".format(paths["manifest"]))
        return 1

    preserve_path, manifest = write_variant(
        paths["manifest"], STEP_FILES["preserve"],
        {"mode": "preserveSettings", "override": []})
    override_path, _ = write_variant(
        paths["manifest"], STEP_FILES["override"],
        {"mode": "preserveSettings", "override": ["nanite"]})
    nanite_declared = declared_nanite(manifest)
    _p("  マニフェストが宣言する Nanite: {0}".format(nanite_declared))

    # --- 1. クリーンな状態から 1 回目 --------------------------------------
    _p("[2/6] UE 側をクリーンにする")
    if not rt.rc_reset_modules(ue_bridge, connection):
        return 2
    if not rt.rc_clean_assets(ue_bridge, connection, [UE_PATH]):
        return 2

    _p("[3/6] 1 回目のインポート")
    report1 = import_manifest_via_rc(
        ue_bridge, connection, paths["manifest"],
        os.path.join(scratch_dir, "reimport_pass1.json"))
    entry1 = first_asset(report1)
    results["steps"].append({"step": "import1", "action": entry1.get("action"),
                             "measured": entry1.get("measured")})
    if not check("1回目は created", entry1.get("action"), "created", checks):
        _p("  （クリーンにできていない可能性があります）")

    # --- 2. UE 側で手修正 ---------------------------------------------------
    _p("[4/6] UE 側で手修正（Nanite ON / ライトマップ {0} / ソケット {1}）".format(
        HAND_LIGHTMAP_RESOLUTION, HAND_SOCKET))
    mutated = mutate_in_ue(ue_bridge, connection, scratch_dir)
    results["steps"].append({"step": "mutate", "result": mutated})
    if not mutated or not mutated.get("loaded"):
        _p("✖ 手修正ができませんでした（アセットを読めていない）")
        _write(args.report, results)
        return 1
    check("手修正後の Nanite", mutated.get("naniteEnabled"), True, checks)
    check("手修正後のライトマップ解像度", mutated.get("lightMapResolution"),
          HAND_LIGHTMAP_RESOLUTION, checks)
    check("手修正後のソケット", HAND_SOCKET in (mutated.get("socketNames") or []),
          True, checks)

    # --- 3. preserveSettings で再インポート --------------------------------
    _p("[5/6] preserveSettings で再インポート（手修正が残るはず）")
    report2 = import_manifest_via_rc(
        ue_bridge, connection, preserve_path,
        os.path.join(scratch_dir, "reimport_pass2.json"))
    entry2 = first_asset(report2)
    measured2 = entry2.get("measured") or {}
    results["steps"].append({"step": "reimport_preserve",
                             "action": entry2.get("action"),
                             "reimport": entry2.get("reimport"),
                             "measured": measured2})
    check("2回目は reimported", entry2.get("action"), "reimported", checks)
    check("Nanite が維持される", measured2.get("naniteEnabled"), True, checks)
    check("ライトマップ解像度が維持される", measured2.get("lightMapResolution"),
          HAND_LIGHTMAP_RESOLUTION, checks)
    check("手で足したソケットが残る",
          HAND_SOCKET in (measured2.get("socketNamesAll") or []), True, checks)

    # --- 4. override: ["nanite"] で再インポート ----------------------------
    _p('[6/6] override: ["nanite"] で再インポート（Nanite だけ戻るはず）')
    report3 = import_manifest_via_rc(
        ue_bridge, connection, override_path,
        os.path.join(scratch_dir, "reimport_pass3.json"))
    entry3 = first_asset(report3)
    measured3 = entry3.get("measured") or {}
    results["steps"].append({"step": "reimport_override",
                             "action": entry3.get("action"),
                             "reimport": entry3.get("reimport"),
                             "measured": measured3})
    check("Nanite がマニフェストの値で上書きされる",
          measured3.get("naniteEnabled"), nanite_declared, checks)
    check("ライトマップ解像度は維持される（override していない）",
          measured3.get("lightMapResolution"), HAND_LIGHTMAP_RESOLUTION, checks)
    check("ソケットも維持される",
          HAND_SOCKET in (measured3.get("socketNamesAll") or []), True, checks)

    failed = [c for c in checks if not c["ok"]]
    results["result"] = "failed" if failed else "success"
    _write(args.report, results)

    _p("")
    _p("結果: {0}（成功 {1} / 失敗 {2}）".format(
        results["result"], len(checks) - len(failed), len(failed)))
    _p("レポート: {0}".format(args.report))
    return 1 if failed else 0


def _write(path, payload):
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.exit(main())
