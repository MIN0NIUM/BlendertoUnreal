"""S-09（バッチ書き出しサンプル）の `.blend` を headless Blender で生成・検証する。

**目的**: 「1 回の書き出しで `manifest.assets` が 2 件になる」（N-5）を機械的に
検証できる最小のシーンを作る。既存のサンプル `.blend` は**一切読まない /
書き換えない**（`tools/make_sample_lod.py` と同じ作法）。

生成する構成（[docs/04 §2.2](../docs/04_命名規約.md)）::

    Crate_01/                   ← アセット用コレクション（名前 = アセット名）
    └── LOD0/      └── Crate_01      立方体   12 三角形 / 100x100x100 cm / M_Crate_Body
    Barrel_01/
    └── LOD0.001/  └── Barrel_01     円柱     60 三角形 /  60x 60x150 cm / M_Barrel_Body

**`LOD0.001` は誤記ではない**（docs/99 P-24）。コレクション名も `.blend` 全体で
一意なので、2 つめのアセットの `LOD0` には Blender が**必ず** `.001` を付ける
（代入しても例外は出ず黙って付く / Blender 5.2.2 LTS で実測）。
`naming.strip_duplicate_suffix()` がこれを剥がしてから LOD レベルを判定する。

**なぜ三角形数と寸法を変えるのか**
    バッチでは「2 件のアセットが**それぞれ正しい FBX を指している**」ことを
    確かめたい。2 つが同じ形・同じ寸法だと、`assets[0]` と `assets[1]` の
    中身が入れ替わっても（あるいは同じ FBX を 2 回指していても）
    比較が緑のままになり、**取り違えを検出できない**（docs/99 K-8 / P-23 と同根）。
    三角形数（12 / 60）・寸法（100³ / 60×60×150）・マテリアル名を
    すべて変えてあるので、どれか 1 つでも入れ替われば必ず落ちる。

**オブジェクト名について**: Blender の `bpy.data.objects` の名前はファイル全体で
一意（docs/99 P-21）。ここでは `Crate_01` / `Barrel_01` と別名にしてあるので
衝突しない。アセット用コレクション名 = アセット名 という規約も満たしている。

使い方::

    # 生成（既定の出力先に保存）
    blender --background --factory-startup --python tools/make_sample_batch.py

    # 生成（出力先を指定）
    blender --background --factory-startup --python tools/make_sample_batch.py -- \
        samples/blender/10_static_batch/10_static_batch.blend

    # 検証（別プロセスで読み直す）
    blender --background --factory-startup --python tools/make_sample_batch.py -- \
        --verify samples/blender/10_static_batch/10_static_batch.blend

検証結果は人間向けのテキストと、`VERIFY_JSON_BEGIN` / `VERIFY_JSON_END` で
囲んだ JSON の両方で標準出力に出す（CI から拾えるように）。
"""

import json
import os
import re
import sys

import bpy

# --- 仕様値 -------------------------------------------------------------------

#: (アセット名, マテリアル名, プリミティブ種別, 期待三角形数, 寸法 cm, ベースカラー)
#: 寸法は `scale_bounds()` で**ぴったり**に揃える（プリミティブの近似誤差を消す）
ASSET_PLAN = (
    {
        "name": "Crate_01",
        "material": "M_Crate_Body",
        "primitive": "cube",
        "triangles": 12,
        "dimensions_cm": (100.0, 100.0, 100.0),
        "color": (0.42, 0.26, 0.13, 1.0),
    },
    {
        "name": "Barrel_01",
        "material": "M_Barrel_Body",
        "primitive": "cylinder",
        "triangles": 60,
        "dimensions_cm": (60.0, 60.0, 150.0),
        "color": (0.18, 0.32, 0.45, 1.0),
    },
)

#: 円柱の側面分割数。16 なら 側面 16 quad(32 tri) + 上下の 16 角形(14 tri × 2) = 60 tri
CYLINDER_VERTICES = 16

DEFAULT_OUTPUT = os.path.join(
    "samples", "blender", "10_static_batch", "10_static_batch.blend"
)


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --- 生成 ---------------------------------------------------------------------


def wipe_everything():
    """ファイル内のデータブロックを空にする。

    `--factory-startup` の既定シーンには Cube / Camera / Light と
    既定マテリアル `Material` が入っている。`Material` を残すと
    V-003c（未使用マテリアル）で止まるので**必ず消す**。
    """
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for mesh in list(bpy.data.meshes):
        bpy.data.meshes.remove(mesh, do_unlink=True)
    for material in list(bpy.data.materials):
        bpy.data.materials.remove(material, do_unlink=True)
    for collection in list(bpy.data.collections):
        bpy.data.collections.remove(collection)
    for image in list(bpy.data.images):
        bpy.data.images.remove(image, do_unlink=True)


def setup_scene_units(scene):
    """V-010: METRIC / Unit Scale 1.0 / METERS。"""
    units = scene.unit_settings
    units.system = "METRIC"
    units.scale_length = 1.0
    units.length_unit = "METERS"


def create_material(name, color):
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    principled = material.node_tree.nodes.get("Principled BSDF")
    if principled is not None:
        principled.inputs["Base Color"].default_value = color
    return material


def scale_bounds(mesh, dimensions_cm):
    """頂点座標を書き換えて、バウンディングボックスを指定寸法ぴったりにする。

    プリミティブは分割数によって軸ごとの寸法がわずかにずれる
    （16 角柱の X 幅は直径と一致しない）。マニフェストの `dimensionsCm` を
    **宣言値と完全に一致**させておけば、UE 側のバウンズ比較がぶれない
    （docs/14 §9.3 で LOD1 の X が 190.2cm になっていた件と同じ対策）。
    """
    target = [v / 100.0 for v in dimensions_cm]
    coords = [tuple(v.co) for v in mesh.vertices]
    lows = [min(c[i] for c in coords) for i in range(3)]
    highs = [max(c[i] for c in coords) for i in range(3)]
    centers = [(lows[i] + highs[i]) * 0.5 for i in range(3)]
    extents = [highs[i] - lows[i] for i in range(3)]
    factors = [
        (target[i] / extents[i]) if extents[i] > 1e-9 else 1.0
        for i in range(3)
    ]
    for vertex in mesh.vertices:
        co = vertex.co
        for i in range(3):
            co[i] = (co[i] - centers[i]) * factors[i]


def add_primitive(kind):
    """プリミティブを1つ追加してアクティブオブジェクトを返す。"""
    if kind == "cube":
        bpy.ops.mesh.primitive_cube_add(
            size=1.0, calc_uvs=True, enter_editmode=False, align="WORLD",
            location=(0.0, 0.0, 0.0), rotation=(0.0, 0.0, 0.0),
        )
    elif kind == "cylinder":
        bpy.ops.mesh.primitive_cylinder_add(
            vertices=CYLINDER_VERTICES, radius=0.5, depth=1.0, calc_uvs=True,
            enter_editmode=False, align="WORLD",
            location=(0.0, 0.0, 0.0), rotation=(0.0, 0.0, 0.0),
        )
    else:
        raise ValueError("未知のプリミティブ種別: {0}".format(kind))
    return bpy.context.active_object


def build_asset(plan, scene):
    """アセット用コレクション + LOD0 + 本体メッシュを1組作る。"""
    asset_collection = bpy.data.collections.new(plan["name"])
    scene.collection.children.link(asset_collection)
    lod0 = bpy.data.collections.new("LOD0")
    asset_collection.children.link(lod0)

    obj = add_primitive(plan["primitive"])
    for collection in list(obj.users_collection):
        collection.objects.unlink(obj)
    lod0.objects.link(obj)

    obj.name = plan["name"]
    obj.data.name = plan["name"]
    if obj.name != plan["name"]:
        # docs/99 P-21: 同名があると黙って `.001` が付く。ここで気づけるようにする
        raise RuntimeError(
            "オブジェクト名を {0} にできませんでした（実際: {1}）".format(
                plan["name"], obj.name))

    scale_bounds(obj.data, plan["dimensions_cm"])

    # トランスフォームは単位（V-006 / V-007 / V-009 を出さない）
    obj.location = (0.0, 0.0, 0.0)
    obj.rotation_euler = (0.0, 0.0, 0.0)
    obj.scale = (1.0, 1.0, 1.0)

    material = create_material(plan["material"], plan["color"])
    obj.data.materials.clear()
    obj.data.materials.append(material)

    obj.data.calc_loop_triangles()
    triangles = len(obj.data.loop_triangles)
    if triangles != plan["triangles"]:
        raise RuntimeError(
            "{0} の三角形数が期待と違います（実測 {1} / 期待 {2}）".format(
                plan["name"], triangles, plan["triangles"]))
    return obj


def build(output_path):
    scene = bpy.context.scene
    wipe_everything()
    setup_scene_units(scene)

    objects = [build_asset(plan, scene) for plan in ASSET_PLAN]

    # P-12: メッシュが選択された状態で保存する。
    # 空の選択状態を保存すると、選択経路で書き出し対象が空になる
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]

    directory = os.path.dirname(os.path.abspath(output_path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)

    bpy.ops.wm.save_as_mainfile(
        filepath=os.path.abspath(output_path), compress=False, copy=False
    )
    return [
        {
            "asset": plan["name"],
            "material": plan["material"],
            "triangles": len(bpy.data.objects[plan["name"]].data.loop_triangles),
            "dimensionsCm": [round(v * 100.0, 4)
                             for v in bpy.data.objects[plan["name"]].dimensions],
        }
        for plan in ASSET_PLAN
    ]


# --- 検証 ---------------------------------------------------------------------


def _strip_duplicate_suffix(name):
    """'LOD0.001' -> 'LOD0'。`naming.strip_duplicate_suffix()` と同じ規則。

    検証は**アドオンに依存せず**動く必要があるため（import できない環境でも
    サンプルの構成だけは確かめたい）、ここに同じ規則を小さく持つ。
    """
    return re.sub(r"\.\d{3}$", "", name or "")


def _triangle_count(obj):
    obj.data.calc_loop_triangles()
    return len(obj.data.loop_triangles)


def _detect_asset_collections():
    """アドオンの `collect.find_asset_collections()` が 2 件見つけるか試す。

    バッチ経路（`--all-collections`）はこの自動列挙に乗っているので、
    サンプル側でも**同じ関数**で確かめる（二重定義を避ける）。
    import できない環境では None を返す。
    """
    addons = os.path.join(repo_root(), "tools", "dcc_blender", "addons")
    if addons not in sys.path:
        sys.path.insert(0, addons)
    try:
        from onepochi.bl import collect
    except Exception:  # noqa: BLE001
        return None
    return sorted(c.name for c in collect.find_asset_collections())


def verify(blend_path):
    """保存済みの `.blend` を読み直して構成を検証する。"""
    bpy.ops.wm.open_mainfile(filepath=os.path.abspath(blend_path))

    report = {
        "blendFile": bpy.data.filepath,
        "blenderVersion": bpy.app.version_string,
        "checks": [],
        "ok": True,
    }

    def check(name, ok, detail, fatal=True):
        """`fatal=False` は「実測の記録」。総合判定（ok）には影響させない。"""
        report["checks"].append({
            "name": name, "ok": bool(ok), "detail": detail, "fatal": bool(fatal),
        })
        if not ok and fatal:
            report["ok"] = False
        return ok

    expected_assets = [plan["name"] for plan in ASSET_PLAN]

    # --- コレクション構成 ---
    collection_names = sorted(c.name for c in bpy.data.collections)
    missing = [n for n in expected_assets if n not in collection_names]
    if not check("アセットコレクション {0} が存在する".format(expected_assets),
                 not missing, collection_names):
        print(_format(report))
        return report

    measured = {}
    for plan in ASSET_PLAN:
        asset = bpy.data.collections[plan["name"]]
        child_names = sorted(c.name for c in asset.children)
        # **2 つめのアセットの子は `LOD0.001` になる**（コレクション名は .blend
        # 全体で一意 / docs/99 P-24）。ユーザーには直せないので、
        # ここでは重複サフィックスを剥がして比較する
        stripped = sorted(_strip_duplicate_suffix(n) for n in child_names)
        check("{0} の子コレクションが LOD0 のみ".format(plan["name"]),
              stripped == ["LOD0"], child_names)

        lod0 = next((c for c in asset.children
                     if _strip_duplicate_suffix(c.name) == "LOD0"), None)
        meshes = [o for o in lod0.objects if o.type == "MESH"] if lod0 else []
        entry = {
            "collection": lod0.name if lod0 else None,
            "objects": [o.name for o in meshes],
            "triangles": [_triangle_count(o) for o in meshes],
            "vertices": [len(o.data.vertices) for o in meshes],
            "uvChannels": [len(o.data.uv_layers) for o in meshes],
            "dimensionsCm": [[round(v * 100.0, 4) for v in o.dimensions]
                             for o in meshes],
            "materials": [[s.material.name if s.material else None
                           for s in o.material_slots] for o in meshes],
            "expectedTriangles": plan["triangles"],
            "expectedDimensionsCm": list(plan["dimensions_cm"]),
            "expectedMaterial": plan["material"],
        }
        measured[plan["name"]] = entry

        check("{0} の LOD0 にメッシュが 1 個".format(plan["name"]),
              len(meshes) == 1, entry["objects"])
        check("{0} のオブジェクト名が {0}".format(plan["name"]),
              entry["objects"] == [plan["name"]], entry["objects"])
        check("{0} の三角形数 = {1}".format(plan["name"], plan["triangles"]),
              sum(entry["triangles"]) == plan["triangles"],
              entry["triangles"])
        check("{0} の寸法 = {1} cm".format(plan["name"],
                                           list(plan["dimensions_cm"])),
              entry["dimensionsCm"] == [[round(v, 4)
                                         for v in plan["dimensions_cm"]]],
              entry["dimensionsCm"])
        check("{0} のマテリアルが {1} のみ".format(plan["name"], plan["material"]),
              entry["materials"] == [[plan["material"]]], entry["materials"])
        check("{0} に UV がある（V-025）".format(plan["name"]),
              all(c >= 1 for c in entry["uvChannels"]), entry["uvChannels"])
    report["assets"] = measured

    # --- 2 件が「取り違えを検出できる」だけ違っているか ---
    triangle_totals = [sum(measured[n]["triangles"]) for n in expected_assets]
    check("2 アセットの三角形数が異なる（取り違え検出用）",
          len(set(triangle_totals)) == len(triangle_totals), triangle_totals)
    dimension_sets = [tuple(measured[n]["dimensionsCm"][0])
                      for n in expected_assets]
    check("2 アセットの寸法が異なる（取り違え検出用）",
          len(set(dimension_sets)) == len(dimension_sets),
          [list(d) for d in dimension_sets])

    # --- マテリアル ---
    material_names = sorted(m.name for m in bpy.data.materials)
    expected_materials = sorted(plan["material"] for plan in ASSET_PLAN)
    check("マテリアルは {0} のみ".format(expected_materials),
          material_names == expected_materials, material_names)
    orphans = [m.name for m in bpy.data.materials if m.users == 0]
    check("未使用マテリアルが無い（V-003c）", not orphans, orphans)

    # --- アセット用コレクションの自動列挙（--all-collections の前提）---
    detected = _detect_asset_collections()
    if detected is None:
        check("collect.find_asset_collections() の実測",
              True, "onepochi パッケージを import できなかったため skip",
              fatal=False)
    else:
        check("collect.find_asset_collections() が {0} を列挙する".format(
                  sorted(expected_assets)),
              detected == sorted(expected_assets), detected)
    report["detectedAssetCollections"] = detected

    # --- シーン単位（V-010）---
    units = bpy.context.scene.unit_settings
    check("シーン単位が METRIC / scale_length 1.0",
          units.system == "METRIC" and abs(units.scale_length - 1.0) < 1e-6,
          {"system": units.system, "scaleLength": units.scale_length})

    # --- 保存済みの選択状態（P-12）---
    selected = sorted(o.name for o in bpy.context.scene.objects if o.select_get())
    meshes_selected = [n for n in selected if bpy.data.objects[n].type == "MESH"]
    check("保存済みの選択状態にメッシュが含まれる（P-12）",
          bool(meshes_selected), selected)
    report["savedSelection"] = selected

    # --- ドットを含む名前が無いこと（V-003 / P-21）---
    object_names = sorted(o.name for o in bpy.data.objects)
    report["objectNames"] = object_names
    dotted = [n for n in object_names if "." in n]
    check("オブジェクト名にドットが含まれない（V-003 / P-21）", not dotted, dotted)
    # コレクション名のドットは「予約コレクションの重複サフィックス」だけ許す。
    # `LOD0.001` は Blender が自動で付けるものでユーザーには直せない（P-24）
    dotted_collections = [c.name for c in bpy.data.collections if "." in c.name]
    unexpected = [n for n in dotted_collections
                  if _strip_duplicate_suffix(n) != "LOD0"]
    check("ドット付きのコレクション名は LOD0 の重複サフィックスだけ（P-24）",
          not unexpected, {"dotted": dotted_collections, "unexpected": unexpected})
    report["dottedCollections"] = dotted_collections

    print(_format(report))
    return report


def _format(report):
    lines = ["", "=== S-09 バッチサンプルの検証 ===",
             "ファイル: {0}".format(report.get("blendFile")),
             "Blender: {0}".format(report.get("blenderVersion")), ""]
    for entry in report["checks"]:
        if entry["ok"]:
            mark = "OK  "
        elif entry.get("fatal", True):
            mark = "NG  "
        else:
            mark = "情報 "
        lines.append("{0}{1} -> {2}".format(
            mark, entry["name"], json.dumps(entry["detail"], ensure_ascii=False)
        ))
    lines.append("")
    lines.append("総合: {0}".format("OK" if report["ok"] else "NG"))
    lines.append("")
    lines.append("VERIFY_JSON_BEGIN")
    lines.append(json.dumps(report, ensure_ascii=False, indent=2))
    lines.append("VERIFY_JSON_END")
    return "\n".join(lines)


# --- エントリポイント ---------------------------------------------------------


def parse_args(argv):
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = []
    verify_path = None
    output = None
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--verify":
            index += 1
            verify_path = argv[index] if index < len(argv) else DEFAULT_OUTPUT
        elif token in ("--output", "-o"):
            index += 1
            output = argv[index] if index < len(argv) else None
        elif not token.startswith("-"):
            output = token
        index += 1
    return output, verify_path


def main():
    output, verify_path = parse_args(sys.argv)

    if verify_path:
        path = verify_path
        if not os.path.isabs(path):
            path = os.path.join(repo_root(), path)
        report = verify(path)
        sys.exit(0 if report["ok"] else 1)

    path = output or DEFAULT_OUTPUT
    if not os.path.isabs(path):
        path = os.path.join(repo_root(), path)
    built = build(path)
    print("")
    print("=== 生成しました ===")
    print("出力: {0}".format(path))
    for entry in built:
        print("  {0}  マテリアル {1}  三角形 {2}  寸法 {3} cm".format(
            entry["asset"], entry["material"], entry["triangles"],
            entry["dimensionsCm"]))
    print("")
    print("BUILD_JSON_BEGIN")
    print(json.dumps({"output": path, "assets": built}, ensure_ascii=False,
                     indent=2))
    print("BUILD_JSON_END")
    sys.exit(0)


if __name__ == "__main__":
    main()
