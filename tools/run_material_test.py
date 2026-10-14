"""マテリアル（P5）の実機検証 — マスターマテリアル生成 + MI の自動生成と割当。

    python tools/run_material_test.py --via-rc     # 起動中の UE エディタに依頼する

**何を確かめるか**（docs/13 P5 の DoD ① ②）:

1. `M_OnePochi_Base`（仮マスターマテリアル）が生成される
2. `material.mode = createInstances` で **`MI_*` が生成され、スロットに割り当たる**

検証は S-03（マルチスロット / `M_Slot_A` `M_Slot_B` `M_Slot_C`）で行う。
出力先は **`/Game/_Dev/Samples/S03_Material`**（docs/99 P-8: サンプルごとに分ける）。

**存在確認だけで合格にしない**（K-8 / docs/99 P-17 の教訓）。
スロットに実際に割り当たっているマテリアルのアセットパスを UE 側で実測し、
`MI_` 名になっていることまで突き合わせる。
"""

import argparse
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import run_roundtrip_test as rt  # noqa: E402

SAMPLE = "03_static_multislot"
UE_PATH = "/Game/_Dev/Samples/S03_Material"
ASSET_PATH = UE_PATH + "/SM_Crate_01"
MASTER_PATH = "/Game/_Dev/Materials/M_OnePochi_Base"
PRESET = "sample_material"

#: S-03 のスロット名 -> 期待する MI 名（`naming.mi_name()` の規約 / docs/04 §1）
EXPECTED_INSTANCES = {
    "M_Slot_A": "MI_Slot_A",
    "M_Slot_B": "MI_Slot_B",
    "M_Slot_C": "MI_Slot_C",
}

MASTER_SCRIPT = '''
import sys, io, json, importlib
sys.argv = ["run_create_master_material.py", "--report", r"{report}"]
sys.path.insert(0, r"{plugin}")
import run_create_master_material as m
importlib.reload(m)
m.main()
'''

MEASURE_SCRIPT = '''
import unreal, json, io

out = {{"master": {{}}, "mesh": {{}}}}

master = unreal.EditorAssetLibrary.load_asset("{master}")
out["master"]["exists"] = master is not None
out["master"]["class"] = type(master).__name__ if master is not None else None
if master is not None:
    try:
        out["master"]["textureParameters"] = [
            str(n) for n in
            unreal.MaterialEditingLibrary.get_texture_parameter_names(master)]
    except Exception as error:
        out["master"]["textureParameters"] = "取得失敗: {{0}}".format(error)

mesh = unreal.EditorAssetLibrary.load_asset("{asset}")
out["mesh"]["exists"] = mesh is not None
if mesh is not None:
    slots = []
    for entry in mesh.get_editor_property("static_materials"):
        material = entry.get_editor_property("material_interface")
        slots.append({{
            "slotName": str(entry.get_editor_property("material_slot_name")),
            "material": material.get_path_name() if material is not None else None,
            "class": type(material).__name__ if material is not None else None,
            "parent": (material.get_editor_property("parent").get_path_name()
                       if (material is not None
                           and isinstance(material, unreal.MaterialInstance)
                           and material.get_editor_property("parent") is not None)
                       else None),
        }})
    out["mesh"]["slots"] = slots

with io.open(r"{report}", "w", encoding="utf-8") as handle:
    handle.write(json.dumps(out, ensure_ascii=False, indent=2))
print("[OnePochi] material measure done")
'''


def _run_script(ue_bridge, connection, source, script_path, report_path, label):
    with io.open(script_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(source)
    statement = ("exec(compile(open(r'{0}', encoding='utf-8').read(), "
                 "'onepochi_material', 'exec'))").format(
                     script_path.replace("\\", "/"))
    if not rt.rc_execute(ue_bridge, connection, statement, timeout=180.0,
                         label=label):
        return None
    if not os.path.isfile(report_path):
        return None
    with io.open(report_path, encoding="utf-8") as handle:
        return json.load(handle)


def check(label, actual, expected, checks):
    ok = actual == expected
    checks.append({"check": label, "expected": expected, "actual": actual,
                   "ok": ok})
    print("  {0} {1}: 期待 {2} / 実測 {3}".format(
        "✔" if ok else "✖", label, expected, actual))
    return ok


def main(argv=None):
    parser = argparse.ArgumentParser(description="マテリアル（P5）の実機検証")
    parser.add_argument("--via-rc", action="store_true")
    parser.add_argument("--report", default=os.path.join(
        ROOT, "docs", "tmp", "material_test_report.json"))
    parser.add_argument("--skip-export", action="store_true")
    args = parser.parse_args(argv)

    if not args.via_rc:
        print("✖ このテストは --via-rc 専用です（UE エディタを起動したまま実行）")
        return 2

    scratch = os.path.join(ROOT, "docs", "tmp")
    if not os.path.isdir(scratch):
        os.makedirs(scratch)

    ue_bridge = rt.rc_bridge()
    if ue_bridge is None:
        return 2
    connection = rt.rc_settings(ue_bridge)
    paths = rt.sample_paths(SAMPLE)

    results = {"sample": SAMPLE, "assetPath": ASSET_PATH, "checks": [],
               "steps": []}
    checks = results["checks"]

    # --- 1. マスターマテリアル（P5 DoD ①） --------------------------------
    print("[1/4] マスターマテリアルを用意する: {0}".format(MASTER_PATH))
    if not rt.rc_reset_modules(ue_bridge, connection):
        return 2
    master_report_path = os.path.join(scratch, "material_report.json")
    master_report = _run_script(
        ue_bridge, connection,
        MASTER_SCRIPT.format(report=master_report_path.replace("\\", "/"),
                             plugin=rt.UE_PLUGIN_DIR.replace("\\", "/")),
        os.path.join(scratch, "_master.py"), master_report_path,
        "マスターマテリアルの生成に失敗しました")
    results["steps"].append({"step": "master", "report": master_report})
    if master_report is None:
        print("✖ マスターマテリアルのレポートが取れませんでした")
        _write(args.report, results)
        return 1
    check("マスターマテリアルが存在する", bool(master_report.get("exists")), True,
          checks)

    # --- 2. createInstances で書き出す ------------------------------------
    if not args.skip_export:
        blender = rt.find_blender()
        if blender is None:
            print("✖ blender.exe が見つかりません（BLENDER_EXE で指定できます）")
            return 2
        print("[2/4] Blender で書き出し（preset={0}） -> {1}".format(PRESET, UE_PATH))
        saved = rt.PRESETS.get(SAMPLE)
        rt.PRESETS[SAMPLE] = PRESET          # このテストだけプリセットを差し替える
        try:
            ok, log = rt.export_sample(blender, SAMPLE, UE_PATH)
        finally:
            if saved is None:
                rt.PRESETS.pop(SAMPLE, None)
            else:
                rt.PRESETS[SAMPLE] = saved
        if not ok:
            print("✖ 書き出しに失敗しました")
            print(log[-2000:])
            return 1

    with io.open(paths["manifest"], encoding="utf-8") as handle:
        manifest = json.load(handle)
    policy = manifest.get("material") or {}
    declared = [(e.get("slotName"), e.get("ueMaterialPath"))
                for e in (manifest.get("assets") or [{}])[0].get("materialSlots") or []]
    results["steps"].append({"step": "export", "material": policy,
                             "slots": declared})
    check("マニフェストの material.mode", policy.get("mode"), "createInstances",
          checks)
    for slot_name, expected_mi in sorted(EXPECTED_INSTANCES.items()):
        path = dict(declared).get(slot_name)
        check("{0} の ueMaterialPath が MI_ 名で解決されている".format(slot_name),
              (path or "").rsplit("/", 1)[-1], expected_mi, checks)

    # --- 3. 取り込み -------------------------------------------------------
    print("[3/4] UE をクリーンにして取り込む")
    if not rt.rc_clean_assets(ue_bridge, connection, [UE_PATH]):
        return 2
    report = rt.import_sample_via_rc(ue_bridge, connection, SAMPLE)
    entry = ((report or {}).get("assets") or [{}])[0]
    material_record = entry.get("material") or {}
    results["steps"].append({"step": "import", "action": entry.get("action"),
                             "material": material_record})
    check("取り込みが成功した", (report or {}).get("result"), "success", checks)
    check("MI の生成件数", len(material_record.get("created") or []),
          len(EXPECTED_INSTANCES), checks)
    check("割当に失敗したスロットが無い", material_record.get("skipped") or [], [],
          checks)

    # --- 4. UE 側で実測（存在確認だけで合格にしない / K-8） ---------------
    print("[4/4] UE 側でスロットの割当を実測する")
    measure_path = os.path.join(scratch, "material_measure.json")
    measured = _run_script(
        ue_bridge, connection,
        MEASURE_SCRIPT.format(master=MASTER_PATH, asset=ASSET_PATH,
                              report=measure_path.replace("\\", "/")),
        os.path.join(scratch, "_measure_material.py"), measure_path,
        "マテリアルの実測に失敗しました")
    results["steps"].append({"step": "measure", "result": measured})
    if measured is None:
        print("✖ 実測レポートが取れませんでした")
        _write(args.report, results)
        return 1

    print("  マスターのテクスチャパラメータ: {0}".format(
        measured.get("master", {}).get("textureParameters")))
    slots = {s["slotName"]: s for s in measured.get("mesh", {}).get("slots") or []}
    for slot_name, expected_mi in sorted(EXPECTED_INSTANCES.items()):
        slot = slots.get(slot_name) or {}
        assigned = (slot.get("material") or "").rsplit(".", 1)[-1]
        check("{0} に割り当たっているマテリアル".format(slot_name), assigned,
              expected_mi, checks)
        check("{0} の割当が MaterialInstanceConstant".format(slot_name),
              slot.get("class"), "MaterialInstanceConstant", checks)
        parent = (slot.get("parent") or "").rsplit(".", 1)[-1]
        check("{0} の MI の親がマスターマテリアル".format(slot_name), parent,
              MASTER_PATH.rsplit("/", 1)[-1], checks)

    failed = [c for c in checks if not c["ok"]]
    results["result"] = "failed" if failed else "success"
    _write(args.report, results)

    print("")
    print("結果: {0}（成功 {1} / 失敗 {2}）".format(
        results["result"], len(checks) - len(failed), len(failed)))
    print("レポート: {0}".format(args.report))
    return 1 if failed else 0


def _write(path, payload):
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.exit(main())
