"""S-07（LOD サンプル）の修正版 `.blend` を headless Blender で生成・検証する。

[docs/14 §9.3](../docs/14_サンプル指摘.md) の修正依頼 G-1 / G-2 / G-3 を解消した
LOD サンプルを**ゼロから組み立てて**保存する。原本
`samples/blender/07_static_lod/07_static_lod.blend` は**一切読まない / 書き換えない**。

生成する構成（[docs/04 §2.2](../docs/04_命名規約.md) / [docs/03 §11.4](../docs/03_マニフェスト仕様.md)）::

    Crate_01/                   ← アセット用コレクション（名前 = アセット名）
    ├── LOD0/  └── Crate_01          1280 三角形
    ├── LOD1/  └── Crate_01_LOD1      320 三角形
    └── LOD2/  └── Crate_01_LOD2       80 三角形

**オブジェクト名について（実測に基づく結論）**
    規約（docs/04 §2.2）は「LOD 間でオブジェクト名が同じでよい」と書いているが、
    **Blender ではこれは実現できない**。`bpy.data.objects` の名前はファイル全体で
    一意で、同名を代入しても黙って `.001` が付く（実測 / Blender 5.2.2 LTS）。
    さらに `.001` は命名規約 V-003（ドットを含む名前）で**エラー**になる。
    そのため LOD0 だけ素のアセット名を使い、LOD1 以降は `<アセット名>_LOD<n>` にする。
    **LOD レベルの判定はコレクション名だけで行われる**（`naming.parse_lod_collection`）
    ので、オブジェクト名のサフィックスはツールの挙動に影響しない。

使い方::

    # 生成（既定の出力先に保存）
    blender --background --factory-startup --python tools/make_sample_lod.py

    # 生成（出力先を指定）
    blender --background --factory-startup --python tools/make_sample_lod.py -- \
        samples/blender/07_static_lod_fixed/07_static_lod_fixed.blend

    # 検証（別プロセスで読み直す）
    blender --background --factory-startup --python tools/make_sample_lod.py -- \
        --verify samples/blender/07_static_lod_fixed/07_static_lod_fixed.blend

検証結果は人間向けのテキストと、`VERIFY_JSON_BEGIN` / `VERIFY_JSON_END` で
囲んだ JSON の両方で標準出力に出す（CI から拾えるように）。
"""

import json
import os
import sys

import bpy

# --- 仕様値 -------------------------------------------------------------------

ASSET_NAME = "Crate_01"
MATERIAL_NAME = "M_Crate_Body"

#: (LOD レベル, Icosphere の分割数, 期待三角形数)
#: Icosphere は 20 * 4**(subdivisions - 1) 三角形になる（2 -> 80 / 3 -> 320 / 4 -> 1280）
LOD_PLAN = (
    (0, 4, 1280),
    (1, 3, 320),
    (2, 2, 80),
)

#: 1 辺 1.0 m（UE で 100uu）。S-01 の `Crate_01` と寸法をそろえる
TARGET_SIZE_M = 1.0

DEFAULT_OUTPUT = os.path.join(
    "samples", "blender", "07_static_lod_fixed", "07_static_lod_fixed.blend"
)


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def lod_object_name(level):
    """LOD レベルごとの本体オブジェクト名。

    LOD0 は素のアセット名。LOD1 以降は Blender が同名を許さないため
    `<アセット名>_LOD<n>` にする（上のモジュール docstring 参照）。
    """
    if level == 0:
        return ASSET_NAME
    return "{0}_LOD{1}".format(ASSET_NAME, level)


# --- 生成 ---------------------------------------------------------------------


def wipe_everything():
    """ファイル内のデータブロックを空にする。

    `--factory-startup` の既定シーンには Cube / Camera / Light と
    既定マテリアル `Material` が入っている。`Material` を残すと
    V-003c（docs/14 G-3）で止まるので**必ず消す**。
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


def create_material():
    material = bpy.data.materials.new(MATERIAL_NAME)
    material.use_nodes = True
    principled = material.node_tree.nodes.get("Principled BSDF")
    if principled is not None:
        # 木箱っぽい色。見た目の確認用でしかない
        principled.inputs["Base Color"].default_value = (0.42, 0.26, 0.13, 1.0)
    return material


def normalize_bounds(mesh, size=TARGET_SIZE_M):
    """頂点座標を書き換えて、バウンディングボックスを原点中心の立方体にする。

    Icosphere は分割数が低いと球の近似誤差で軸ごとの寸法がずれる
    （docs/14 §9.3 の実測で LOD1 だけ X が 190.2 cm だった件）。
    3 つの LOD の `dimensions` を**完全に同じ**にしておけば、
    UE 側のバウンズ比較がサンプル間でぶれない。
    """
    coords = [tuple(v.co) for v in mesh.vertices]
    lows = [min(c[i] for c in coords) for i in range(3)]
    highs = [max(c[i] for c in coords) for i in range(3)]
    centers = [(lows[i] + highs[i]) * 0.5 for i in range(3)]
    extents = [highs[i] - lows[i] for i in range(3)]
    factors = [
        (size / extents[i]) if extents[i] > 1e-9 else 1.0
        for i in range(3)
    ]
    for vertex in mesh.vertices:
        co = vertex.co
        for i in range(3):
            co[i] = (co[i] - centers[i]) * factors[i]


def build_lod_object(level, subdivisions, material, target_collection):
    """1 段ぶんの本体メッシュを作って `target_collection` に入れる。"""
    name = lod_object_name(level)

    bpy.ops.mesh.primitive_ico_sphere_add(
        subdivisions=subdivisions,
        radius=TARGET_SIZE_M * 0.5,
        calc_uvs=True,
        enter_editmode=False,
        align="WORLD",
        location=(0.0, 0.0, 0.0),
        rotation=(0.0, 0.0, 0.0),
    )
    obj = bpy.context.active_object

    # 既定のコレクションから外して LOD コレクションに入れ替える
    for collection in list(obj.users_collection):
        collection.objects.unlink(obj)
    target_collection.objects.link(obj)

    obj.name = name
    obj.data.name = name
    normalize_bounds(obj.data)

    # トランスフォームは単位（V-006 / V-007 / V-009 を出さない）
    obj.location = (0.0, 0.0, 0.0)
    obj.rotation_euler = (0.0, 0.0, 0.0)
    obj.scale = (1.0, 1.0, 1.0)

    # マテリアルは全 LOD 共通で 1 枚だけ（V-046 / V-003c）
    obj.data.materials.clear()
    obj.data.materials.append(material)

    obj.data.calc_loop_triangles()
    return obj


def build(output_path):
    scene = bpy.context.scene
    wipe_everything()
    setup_scene_units(scene)

    material = create_material()

    asset_collection = bpy.data.collections.new(ASSET_NAME)
    scene.collection.children.link(asset_collection)

    objects = []
    for level, subdivisions, expected in LOD_PLAN:
        lod_collection = bpy.data.collections.new("LOD{0}".format(level))
        asset_collection.children.link(lod_collection)
        obj = build_lod_object(level, subdivisions, material, lod_collection)
        triangles = len(obj.data.loop_triangles)
        if triangles != expected:
            raise RuntimeError(
                "LOD{0} の三角形数が期待と違います（実測 {1} / 期待 {2}）".format(
                    level, triangles, expected
                )
            )
        objects.append(obj)

    # P-12: メッシュが選択された状態で保存する。
    # Armature だけ / 空の選択状態を保存すると書き出し対象が空になる
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
            "level": level,
            "object": lod_object_name(level),
            "triangles": len(bpy.data.objects[lod_object_name(level)].data.loop_triangles),
        }
        for level, _, _ in LOD_PLAN
    ]


# --- 検証 ---------------------------------------------------------------------


def _triangle_count(obj):
    obj.data.calc_loop_triangles()
    return len(obj.data.loop_triangles)


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

    # --- コレクション構成 ---
    asset = bpy.data.collections.get(ASSET_NAME)
    if not check("アセットコレクション {0} が存在する".format(ASSET_NAME),
                 asset is not None,
                 sorted(c.name for c in bpy.data.collections)):
        print(_format(report))
        return report

    child_names = sorted(c.name for c in asset.children)
    check("子コレクションが LOD0/LOD1/LOD2",
          child_names == ["LOD0", "LOD1", "LOD2"], child_names)

    # --- LOD ごとの三角形数とオブジェクト名 ---
    measured = {}
    for level, _, expected in LOD_PLAN:
        collection = bpy.data.collections.get("LOD{0}".format(level))
        meshes = ([o for o in collection.objects if o.type == "MESH"]
                  if collection else [])
        entry = {
            "collection": "LOD{0}".format(level),
            "objects": [o.name for o in meshes],
            "triangles": [_triangle_count(o) for o in meshes],
            "vertices": [len(o.data.vertices) for o in meshes],
            "dimensionsCm": [[round(v * 100.0, 4) for v in o.dimensions]
                             for o in meshes],
            "materials": [[s.material.name if s.material else None
                           for s in o.material_slots] for o in meshes],
            "expectedTriangles": expected,
        }
        measured["LOD{0}".format(level)] = entry
        check("LOD{0} のメッシュが 1 個".format(level), len(meshes) == 1, entry["objects"])
        total = sum(entry["triangles"])
        check("LOD{0} の三角形数 = {1}".format(level, expected), total == expected, total)
    report["lods"] = measured

    # --- マテリアル ---
    material_names = sorted(m.name for m in bpy.data.materials)
    check("マテリアルは {0} のみ".format(MATERIAL_NAME),
          material_names == [MATERIAL_NAME], material_names)
    orphans = [m.name for m in bpy.data.materials if m.users == 0]
    check("未使用マテリアルが無い", not orphans, orphans)
    slot_sets = set()
    for entry in measured.values():
        for slots in entry["materials"]:
            slot_sets.add(tuple(slots))
    check("全 LOD のマテリアル構成が一致（V-046）",
          slot_sets == {(MATERIAL_NAME,)}, [list(s) for s in sorted(slot_sets)])

    # --- 三角形数の単調減少（V-047）---
    counts = [sum(measured["LOD{0}".format(level)]["triangles"])
              for level, _, _ in LOD_PLAN]
    check("三角形数が LOD レベルに対して単調減少（V-047）",
          all(counts[i] > counts[i + 1] for i in range(len(counts) - 1)), counts)

    # --- 寸法 ---
    dims = [tuple(round(v, 3) for v in d)
            for entry in measured.values() for d in entry["dimensionsCm"]]
    check("全 LOD の寸法が 100x100x100 cm",
          set(dims) == {(100.0, 100.0, 100.0)}, sorted(set(dims)))

    # --- シーン単位（V-010）---
    units = bpy.context.scene.unit_settings
    check("シーン単位が METRIC / scale_length 1.0",
          units.system == "METRIC" and abs(units.scale_length - 1.0) < 1e-6,
          {"system": units.system, "scaleLength": units.scale_length})

    # --- 保存済みの選択状態（P-12）---
    selected = sorted(o.name for o in bpy.context.scene.objects if o.select_get())
    meshes_selected = [n for n in selected
                       if bpy.data.objects[n].type == "MESH"]
    check("保存済みの選択状態にメッシュが含まれる（P-12）",
          bool(meshes_selected), selected)
    report["savedSelection"] = selected

    # --- オブジェクト名（規約との矛盾の実測）---
    object_names = sorted(o.name for o in bpy.data.objects)
    report["objectNames"] = object_names
    with_lod_suffix = [n for n in object_names if "_LOD" in n]
    report["objectNamesContainingLOD"] = with_lod_suffix
    report["blenderAllowsDuplicateObjectNames"] = False
    check("オブジェクト名に `_LOD` が含まれない（docs/04 §2.2 の記述どおり）",
          not with_lod_suffix,
          {
              "withLodSuffix": with_lod_suffix,
              "理由": "Blender は bpy.data.objects の名前をファイル全体で一意に保つため、"
                      "3 つとも Crate_01 にはできない（同名を代入しても黙って .001 が付く）。"
                      "`.001` は V-003（ドットを含む名前）でエラーになるので使えない。"
                      "LOD レベルの判定はコレクション名だけで行われるため実害は無い",
          },
          fatal=False)

    # --- ドットを含む名前が無いこと（V-003）---
    dotted = [n for n in object_names if "." in n]
    check("オブジェクト名にドットが含まれない（V-003）", not dotted, dotted)

    print(_format(report))
    return report


def _format(report):
    lines = ["", "=== S-07 修正版 LOD サンプルの検証 ===",
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
        print("  LOD{0}  オブジェクト {1}  三角形 {2}".format(
            entry["level"], entry["object"], entry["triangles"]))
    print("")
    print("BUILD_JSON_BEGIN")
    print(json.dumps({"output": path, "lods": built}, ensure_ascii=False, indent=2))
    print("BUILD_JSON_END")
    sys.exit(0)


if __name__ == "__main__":
    main()
