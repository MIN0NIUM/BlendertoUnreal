"""バリデータ（docs/05_バリデーション規則.md の実装）。

設計方針 D-3: バリデーションは DCC 側（上流）で行う。
UE に持ち込んでから気づく事故を、ここで全部止める。

重大度の扱い:
    error   ... 書き出しを中断する
    warning ... 続行する（プリセットの validation.strict が true なら error 扱い）
    info    ... 記録のみ
"""

import math
import os

import bpy
from mathutils import Vector

from ..core import issues as issues_mod
from ..core import naming
from ..core import texture_discovery
from . import skeletal

#: 面積ゼロ判定のしきい値（m^2）
DEGENERATE_AREA_EPS = 1e-12

#: 凸判定で許容する平面からのはみ出し（m）
CONVEX_EPS = 1e-5

#: スケール/回転が「適用済み」とみなす許容誤差
TRANSFORM_EPS = 1e-5

#: ルートボーンが「原点にある」とみなす許容誤差（cm）
ROOT_BONE_ORIGIN_EPS_CM = 0.01

#: ウェイト合計の「実質ゼロ」判定
ZERO_WEIGHT_EPS = 1e-4

#: 法線マップに期待する Blender の色空間（V-114）
NON_COLOR = "Non-Color"


def _obj(name):
    return bpy.data.objects.get(name)


def _is_identity_scale(scale):
    return all(abs(v - 1.0) <= TRANSFORM_EPS for v in scale)


def _is_zero_rotation(euler):
    return all(abs(v) <= TRANSFORM_EPS for v in euler)


# --- ジオメトリ判定ヘルパ -----------------------------------------------------


def count_degenerate_triangles(mesh):
    mesh.calc_loop_triangles()
    count = 0
    for tri in mesh.loop_triangles:
        v0 = mesh.vertices[tri.vertices[0]].co
        v1 = mesh.vertices[tri.vertices[1]].co
        v2 = mesh.vertices[tri.vertices[2]].co
        area = (v1 - v0).cross(v2 - v0).length * 0.5
        if area * area < DEGENERATE_AREA_EPS:
            count += 1
    return count


def count_loose_geometry(mesh):
    """(孤立頂点数, 孤立辺数) を返す。"""
    used_verts = set()
    used_edge_keys = set()
    for polygon in mesh.polygons:
        used_verts.update(polygon.vertices)
        used_edge_keys.update(polygon.edge_keys)
    loose_verts = len(mesh.vertices) - len(used_verts)
    loose_edges = sum(1 for e in mesh.edges if e.key not in used_edge_keys)
    return loose_verts, loose_edges


def count_ngons(mesh):
    return sum(1 for p in mesh.polygons if len(p.vertices) > 4)


def is_closed_manifold(mesh):
    """すべての辺がちょうど2面に共有されているか。"""
    edge_use = {}
    for polygon in mesh.polygons:
        for key in polygon.edge_keys:
            edge_use[key] = edge_use.get(key, 0) + 1
    if not edge_use:
        return False
    return all(count == 2 for count in edge_use.values())


def non_convex_face_count(mesh):
    """凸でない根拠となる面の数を返す。0 なら凸。"""
    bad = 0
    vertices = [v.co for v in mesh.vertices]
    for polygon in mesh.polygons:
        normal = polygon.normal
        if normal.length_squared < 1e-12:
            continue
        origin = vertices[polygon.vertices[0]]
        for co in vertices:
            if normal.dot(co - origin) > CONVEX_EPS:
                bad += 1
                break
    return bad


def count_unused_material_slots(obj):
    """ポリゴンから参照されていないマテリアルスロットの数。"""
    mesh = obj.data
    slot_count = len(obj.material_slots)
    if slot_count <= 1:
        return 0
    used = set(p.material_index for p in mesh.polygons)
    return sum(1 for i in range(slot_count) if i not in used)


# --- 個別ルール ---------------------------------------------------------------


def check_scene_units(result, scene=None):
    """V-010: シーン単位設定。"""
    scene = scene or bpy.context.scene
    units = scene.unit_settings
    target = "Scene '{0}'".format(scene.name)
    if units.system != "METRIC":
        result.error("V-010",
                     "シーンの単位系が METRIC ではありません（現在: {0}）".format(units.system),
                     target)
    if abs(units.scale_length - 1.0) > 1e-6:
        result.error("V-010",
                     "シーンの Unit Scale が 1.0 ではありません（現在: {0}）。"
                     "1.0 以外だと書き出しスケールが狂います".format(units.scale_length),
                     target)
    if getattr(units, "length_unit", "METERS") not in ("METERS", "ADAPTIVE"):
        result.warn("V-010",
                    "シーンの長さ単位が METERS ではありません（現在: {0}）".format(units.length_unit),
                    target)


def check_names(result, names, allow_leading_digit=False, kind="名前"):
    """V-003 / V-003a / V-003b / V-003d: 命名規約。"""
    for name in names:
        problem = naming.name_problem(name, allow_leading_digit)
        if problem is None:
            continue
        rule = "V-003a" if (name and name[0].isdigit()) else "V-003"
        if not all(c.isascii() for c in name):
            rule = "V-003"
        elif name and not name[0].isdigit() and not naming.NAME_RE.match(name):
            rule = "V-003b"
        result.error(rule, "{0}が規約違反です: {1}".format(kind, problem), name)

    for first, second in naming.find_case_collisions(names):
        result.error("V-003d",
                     "大文字小文字のみ異なる名前が共存しています（'{0}' と '{1}'）。"
                     "Windows で衝突します".format(first, second),
                     second)


def lod_collection_names(spec):
    """命名チェックにかける LOD コレクション名を返す。

    **予約コレクション名に付いた Blender の重複サフィックス（`.001`）は
    剥がしてから検査する**（docs/99 P-24）。コレクション名は `.blend` 全体で
    一意なので、1 ファイルに 2 つのアセットコレクションを置くと 2 つめの
    `LOD0` は **必ず** `LOD0.001` になる。これはユーザーには直せないので、
    V-003（名前にドット）でエラーにしてはいけない。

    予約語でないコレクション名のドットは**そのまま検査する**
    （`Crate_01.001` のような本当の事故は拾いたい）。
    """
    names = []
    for lod in spec.lods:
        name = lod.collection_name
        if not name:
            continue
        if naming.is_reserved_collection(name):
            name = naming.strip_duplicate_suffix(name)
        names.append(name)
    return names


def check_materials(result, material_names):
    """V-003c: マテリアル名は M_<名前> 形式（Q-19 → 違反は書き出し中断）。"""
    for name in material_names:
        if not name:
            result.error("V-032", "マテリアルが割り当てられていない空スロットがあります", None)
            continue
        problem = naming.material_problem(name)
        if problem:
            result.error("V-003c", problem, name)


def check_object_transform(result, obj, auto_fix=True):
    """V-006 / V-007 / V-009: トランスフォームの適用状態。"""
    name = obj.name
    if not _is_identity_scale(obj.scale):
        scale = tuple(round(v, 4) for v in obj.scale)
        result.add("V-006", issues_mod.ERROR if not auto_fix else issues_mod.WARNING,
                   "スケールが適用されていません（{0}）。"
                   "{1}".format(scale,
                                "書き出し用の複製側で適用します" if auto_fix
                                else "Ctrl+A > Scale で適用してください"),
                   name, auto_fixed=auto_fix)
    if not _is_zero_rotation(obj.rotation_euler):
        degrees = tuple(round(math.degrees(v), 3) for v in obj.rotation_euler)
        result.warn("V-007",
                    "回転が適用されていません（{0} 度）。"
                    "{1}".format(degrees,
                                 "書き出し用の複製側で適用します" if auto_fix
                                 else "Ctrl+A > Rotation で適用してください"),
                    name, auto_fixed=auto_fix)
    if obj.matrix_world.determinant() < 0:
        result.error("V-009",
                     "負のスケール（ミラー）が残っています。行列式が負です。"
                     "Ctrl+A > Scale で適用し、必要なら法線を再計算してください",
                     name)


def check_object_misc(result, obj, skeletal_mode=False):
    """V-011: 未適用モディファイア。

    Skeletal Mesh の Armature モディファイアは**適用してはいけない**ので
    警告の対象から外す。「書き出し時に適用されます」は嘘になってしまう
    （prepare.PRESERVED_MODIFIER_TYPES と対応）。
    """
    if not obj.modifiers:
        return
    targets = [m for m in obj.modifiers
               if not (skeletal_mode and m.type == "ARMATURE")]
    if not targets:
        return
    names = ", ".join(m.name for m in targets)
    result.warn("V-011",
                "未適用のモディファイアがあります（{0}）。"
                "書き出し時に適用されます。意図したものか確認してください".format(names),
                obj.name)


def check_mesh_geometry(result, obj, limits, auto_fix=True, skeletal_mode=False):
    """V-020 〜 V-034: メッシュジオメトリ。"""
    name = obj.name
    mesh = obj.data

    triangles = 0
    try:
        mesh.calc_loop_triangles()
        triangles = len(mesh.loop_triangles)
    except Exception:
        triangles = sum(max(len(p.vertices) - 2, 0) for p in mesh.polygons)

    if triangles == 0:
        result.error("V-020", "三角形数が 0 です（空のメッシュ）", name)
        return

    ngons = count_ngons(mesh)
    if ngons:
        result.warn("V-021",
                    "N-gon（5角以上の面）が {0} 個あります。"
                    "書き出し時に三角形化します".format(ngons),
                    name, auto_fixed=auto_fix)

    degenerate = count_degenerate_triangles(mesh)
    if degenerate:
        result.error("V-022",
                     "面積ゼロの三角形が {0} 個あります。UE で法線・ライトマップが壊れます".format(degenerate),
                     name)

    loose_verts, loose_edges = count_loose_geometry(mesh)
    if loose_verts or loose_edges:
        result.warn("V-023",
                    "孤立頂点 {0} 個 / 孤立辺 {1} 本があります".format(loose_verts, loose_edges),
                    name)

    uv_count = len(mesh.uv_layers)
    if uv_count == 0:
        result.error("V-025", "UV が1つもありません（UV0 は必須）", name)
    elif uv_count == 1 and not skeletal_mode:
        # ライトマップ UV は Static Mesh の概念。Skeletal Mesh には不要
        result.warn("V-026",
                    "UV1（ライトマップ用）がありません。UE 側で自動生成します",
                    name)

    slot_count = len(obj.material_slots)
    if slot_count == 0:
        result.warn("V-032", "マテリアルスロットが割り当てられていません", name)
    else:
        max_slots = limits.get("maxMaterialSlots")
        if max_slots and slot_count > max_slots:
            result.warn("V-033",
                        "マテリアルスロットが {0} 個あります（推奨上限 {1}）。"
                        "ドローコールが増えます".format(slot_count, max_slots),
                        name)
        unused = count_unused_material_slots(obj)
        if unused:
            result.warn("V-034",
                        "使用されていないマテリアルスロットが {0} 個あります".format(unused),
                        name)

    result.info("V-031",
                "頂点 {0} / 三角形 {1} / UV {2}ch".format(len(mesh.vertices), triangles, uv_count),
                name)


def check_collisions(result, spec, limits):
    """V-040 〜 V-044: コリジョン。"""
    main_names = set(spec.main_object_names)
    for entry in spec.collisions:
        obj = _obj(entry.object_name)
        if obj is None:
            continue

        if entry.target not in main_names:
            result.error("V-041",
                         "コリジョン '{0}' に対応する本体メッシュ '{1}' が書き出し対象にありません。"
                         "命名が <Prefix>_<本体メッシュ名>_<連番> になっているか確認してください".format(
                             entry.object_name, entry.target),
                         entry.object_name)

        if entry.index is None:
            result.warn("V-040",
                        "コリジョン名に連番がありません。<Prefix>_<本体名>_<2桁連番> を推奨します",
                        entry.object_name)

        mesh = obj.data
        if entry.shape == "convex":
            if not is_closed_manifold(mesh):
                result.error("V-042",
                             "凸包コリジョンが閉じた多面体になっていません"
                             "（辺が2面に共有されていない箇所があります）",
                             entry.object_name)
            bad_faces = non_convex_face_count(mesh)
            if bad_faces:
                result.warn("V-043",
                            "凸ではありません（{0}/{1} 面が凸条件を満たしません）。"
                            "UE は凸包として扱うため、意図しない形状になります".format(
                                bad_faces, len(mesh.polygons)),
                            entry.object_name)

        max_tris = limits.get("maxCollisionTrianglesPerShape")
        if max_tris and entry.triangle_count > max_tris:
            result.warn("V-044",
                        "コリジョンの三角形数が {0} です（推奨上限 {1}）。物理コストが上がります".format(
                            entry.triangle_count, max_tris),
                        entry.object_name)

        if len(obj.material_slots) > 0:
            result.warn("V-034",
                        "コリジョンメッシュにマテリアルスロットが付いています（不要です）",
                        entry.object_name)


def check_lods(result, spec):
    """V-045 / V-046 / V-047: LOD（コレクション方式）。"""
    levels = sorted(lod.level for lod in spec.lods)
    if not levels:
        result.error("V-045", "LOD0 が見つかりません", spec.asset_name)
        return
    if levels[0] != 0:
        result.error("V-045a",
                     "LOD0 コレクションがありません（最小の LOD が LOD{0}）".format(levels[0]),
                     spec.asset_name)
    expected = list(range(len(levels)))
    if levels != expected:
        result.error("V-045",
                     "LOD コレクションに欠番があります（検出: {0} / 期待: {1}）".format(
                         levels, expected),
                     spec.asset_name)

    # マテリアル構成の一致
    reference = None
    for lod in sorted(spec.lods, key=lambda l: l.level):
        objects = [_obj(n) for n in lod.object_names]
        objects = [o for o in objects if o is not None]
        slots = tuple(sorted(set(
            slot.material.name if slot.material else ""
            for o in objects for slot in o.material_slots
        )))
        if reference is None:
            reference = slots
        elif slots != reference:
            result.error("V-046",
                         "LOD{0} のマテリアル構成が LOD0 と一致しません（{1} vs {2}）".format(
                             lod.level, list(slots), list(reference)),
                         lod.collection_name or spec.asset_name)

    # 三角形数の単調減少
    counts = []
    for lod in sorted(spec.lods, key=lambda l: l.level):
        total = 0
        for name in lod.object_names:
            obj = _obj(name)
            if obj is not None and obj.type == "MESH":
                obj.data.calc_loop_triangles()
                total += len(obj.data.loop_triangles)
        counts.append((lod.level, total))
    for index in range(1, len(counts)):
        previous_level, previous = counts[index - 1]
        level, current = counts[index]
        if current >= previous:
            result.warn("V-047",
                        "LOD{0} の三角形数 {1} が LOD{2} の {3} 以上です。"
                        "LOD の意味がありません".format(level, current, previous_level, previous),
                        spec.asset_name)


def check_sockets(result, spec):
    """V-049: ソケット名の重複 / V-050: ソケット名の命名規約。

    **V-050 が無いと D-3 違反になる。**`SOCKET_1_weapon` のような名前は
    マニフェストのスキーマ（`ueAssetName`）に違反するが、DCC 側で止めないと
    「書き出しは通るのに後段で落ちる」ことになる。
    """
    seen = {}
    for socket in spec.sockets:
        problem = naming.socket_name_problem(socket.object_name)
        if problem:
            result.error("V-050", problem, socket.object_name)
        if socket.name in seen:
            result.error("V-049",
                         "ソケット名が重複しています（'{0}' と '{1}'）".format(
                             seen[socket.name], socket.object_name),
                         socket.object_name)
        else:
            seen[socket.name] = socket.object_name


def image_colorspace_of(file_name):
    """ファイル名に対応する Blender の画像の色空間名を返す。無ければ None。

    マニフェストに入るのは**ファイル名だけ**なので、`bpy.data.images` 側の
    パスの末尾で突き合わせる（Blender の相対パス表記 `//textures/T_xxx.jpg` や
    区切りがバックスラッシュのものでも一致する）。

    **`os.path.basename()` は使えない。**Blender の相対パスは `//` で始まるが、
    Windows の `ntpath` はこれを UNC パス（`//サーバ/共有`）と解釈するため、
    `os.path.basename("//textures/T_x.png")` は **空文字を返す**（実測）。
    区切りを `/` に揃えて自前で切る。
    """
    if not file_name:
        return None
    target = file_name.lower()
    for image in bpy.data.images:
        path = image.filepath_raw or image.filepath or ""
        if not path:
            continue
        tail = path.replace("\\", "/").rsplit("/", 1)[-1]
        if tail.lower() == target:
            try:
                return image.colorspace_settings.name
            except AttributeError:
                return None
    return None


def check_textures(result, spec, texture_dir):
    """V-110 / V-111 / V-112 / V-113 / V-114（+ V-092）: テクスチャの発見（R-4）。

    **すべて警告・情報であり、書き出しは止めない**（Q-06）。
    走査は `texture_dir` の**直下だけ**（再帰しない / docs/04 §3）。

    テクスチャフォルダが無い・対象拡張子のファイルが 1 つも無い場合は
    **何も言わない。**テクスチャを置いていないアセット（S-01〜S-10）に
    「見つかりません」と言っても直しようがない。
    """
    if not texture_dir:
        return
    file_names = texture_discovery.list_texture_files(texture_dir)
    if not texture_discovery.has_texture_files(file_names):
        return

    slot_names = [name for _, name in spec.material_slots]
    if not slot_names:
        return

    textures, found_issues = texture_discovery.discover(
        slot_names, spec.asset_name, file_names)
    result.extend(found_issues)

    for slot_name in slot_names:
        normal = (textures.get(slot_name) or {}).get(
            texture_discovery.NORMAL_SUFFIX)
        if not normal:
            continue
        colorspace = image_colorspace_of(normal)
        if colorspace is not None and colorspace != NON_COLOR:
            result.info(
                "V-114",
                "法線マップ '{0}' の色空間が Blender 上で '{1}' になっています"
                "（本来は '{2}'）。UE 側はサフィックスで決めるため取り込み結果は"
                "変わりませんが、Blender 上の見た目がずれます".format(
                    normal, colorspace, NON_COLOR),
                normal)


def check_environment(result, output_dir, blend_path):
    """V-100 / V-102: 環境チェック。"""
    if output_dir:
        parent = output_dir if os.path.isdir(output_dir) else os.path.dirname(output_dir)
        if parent and not os.path.isdir(parent):
            try:
                os.makedirs(parent)
            except OSError as error:
                result.error("V-100",
                             "出力先フォルダを作成できません: {0}（{1}）".format(parent, error),
                             parent)
                return
        if parent and not os.access(parent, os.W_OK):
            result.error("V-100", "出力先フォルダに書き込めません: {0}".format(parent), parent)

    if not blend_path:
        result.warn("V-102",
                    ".blend ファイルが保存されていません。"
                    "元データの追跡ができないため、保存してから書き出してください")


def check_destination_path(result, ue_path, whitelist=None):
    """V-004: UE 出力先パス。whitelist があればその配下かも見る。"""
    problem = naming.ue_path_problem(ue_path)
    if problem:
        result.error("V-004", problem, ue_path)
        return
    if whitelist:
        allowed = any(ue_path == root or ue_path.startswith(root.rstrip("/") + "/")
                      for root in whitelist)
        if not allowed:
            result.error("V-004",
                         "出力先 '{0}' が許可されたルート {1} の配下ではありません".format(
                             ue_path, whitelist),
                         ue_path)


# --- 統合 ---------------------------------------------------------------------



# --- Skeletal Mesh（V-060..V-079 / docs/05 §6） -------------------------------


def check_armature_object(result, armature_obj, auto_fix=True):
    """Armature オブジェクト自身のトランスフォーム（V-077）。

    Armature にスケールが残っていると、ボーン長とメッシュのスケールが
    FBX 往復で食い違う。メッシュと同じ基準で弾く。
    """
    if armature_obj is None:
        return
    if not _is_identity_scale(armature_obj.scale):
        result.error("V-077",
                     "Armature にスケールが適用されていません（{0}）。"
                     "Ctrl+A > Scale で適用してください".format(
                         [round(v, 6) for v in armature_obj.scale]),
                     armature_obj.name)
    if not _is_zero_rotation(armature_obj.rotation_euler):
        result.error("V-077",
                     "Armature に回転が適用されていません（{0}）。"
                     "Ctrl+A > Rotation で適用してください".format(
                         [round(math.degrees(v), 3)
                          for v in armature_obj.rotation_euler]),
                     armature_obj.name)


def check_bone_names(result, bone_entries, allow_leading_digit=False):
    """ボーン名の規約（V-066 / V-078）。

    UE ではボーン名が ABP・Control Rig・リターゲットの**キー**になる。
    先頭や末尾のスペースは UI でほぼ判別できないまま参照を壊すため、
    メッシュ名と同じ厳しさで弾く（docs/14 §1.3 で実際に踏んだ問題）。
    """
    names = [entry.name for entry in bone_entries]
    for name in names:
        problem = naming.name_problem(name, allow_leading_digit)
        if problem:
            result.error("V-066",
                         "ボーン名が規約違反です: {0}（'{1}'）".format(problem, name),
                         name)
    for first, second in naming.find_case_collisions(names):
        result.warn("V-078",
                    "大文字小文字だけが違うボーン名があります（'{0}' / '{1}'）。"
                    "UE 側で衝突する可能性があります".format(first, second),
                    second)


def check_bone_hierarchy(result, spec, armature_obj, settings, limits):
    """ボーン階層の構造（V-061 / V-062 / V-063 / V-067 / V-073）。"""
    bones = spec.bones
    if not bones:
        result.error("V-060",
                     "Armature にボーンがありません", spec.armature_name or spec.asset_name)
        return

    # V-061: ルートは1本
    roots = [b for b in bones if b.parent is None]
    if len(roots) > 1:
        result.error("V-061",
                     "ルートボーンが {0} 本あります（{1}）。"
                     "UE の Skeleton はルートが1本である必要があります".format(
                         len(roots), ", ".join(b.name for b in roots)),
                     spec.armature_name)
    if not roots:
        result.error("V-061", "ルートボーンが見つかりません", spec.armature_name)
        return

    root = roots[0]
    expected_root = (settings.get("skeleton") or {}).get("rootBoneName")

    # V-063: ルートは原点
    if any(abs(value) > ROOT_BONE_ORIGIN_EPS_CM for value in root.head_cm):
        result.error("V-063",
                     "ルートボーン '{0}' が原点にありません（{1} cm）。"
                     "原点かつ床の高さに置いてください".format(root.name, root.head_cm),
                     root.name)

    # ルート名の慣習（情報のみ。UE 側で強制はしない）
    if expected_root and root.name != expected_root:
        result.warn("V-062",
                    "ルートボーン名が '{0}' ではなく '{1}' です。"
                    "プリセットの skeleton.rootBoneName と揃えると"
                    "リターゲットで扱いやすくなります".format(expected_root, root.name),
                    root.name)

    # V-062: deform_only で書き出すとき、ルートが deform でないと消える
    fbx_settings = (settings.get("export") or {}).get("fbx") or {}
    if fbx_settings.get("use_armature_deform_only") and not root.use_deform:
        result.error("V-062",
                     "ルートボーン '{0}' にウェイトが無く、"
                     "FBX 書き出し設定 use_armature_deform_only が有効なため"
                     "ルートが削除されます。ルートにウェイトを少量割り当てるか、"
                     "プリセットで use_armature_deform_only を false にしてください".format(
                         root.name),
                     root.name)

    # V-067: 実質ゼロ長のボーン
    for name, length in skeletal.degenerate_bones(armature_obj, spec.exported_bones):
        result.error("V-067",
                     "ボーン '{0}' の長さが実質ゼロです（{1} m）。"
                     "FBX 書き出しで向きが不定になります。"
                     "tail を伸ばしてください（0.1 m 程度を推奨）".format(name, length),
                     name)

    # V-073: ボーン数の上限（Q-06 により警告）
    max_bones = limits.get("maxBones")
    if max_bones and len(bones) > int(max_bones):
        result.warn("V-073",
                    "ボーン数が上限を超えています（{0} > {1}）。"
                    "対象プラットフォームによっては問題になります".format(
                        len(bones), max_bones),
                    spec.armature_name)


def check_pose(result, armature_obj):
    """ポーズに値が残っていないか（V-064 / V-065）。"""
    if armature_obj is None:
        return

    scaled = skeletal.non_uniform_bone_scales(armature_obj)
    for name, scale in scaled:
        result.error("V-064",
                     "ボーン '{0}' のポーズにスケールが入っています（{1}）。"
                     "Pose > Clear Transform > Scale で戻してください".format(name, scale),
                     name)

    posed = [name for name in skeletal.posed_bones(armature_obj)
             if name not in {n for n, _ in scaled}]
    if posed:
        shown = ", ".join(posed[:5])
        more = "" if len(posed) <= 5 else " ほか{0}本".format(len(posed) - 5)
        result.warn("V-065",
                    "ポーズに移動・回転が入っているボーンがあります（{0}{1}）。"
                    "書き出しは rest 姿勢で行われるため、"
                    "意図したポーズは反映されません".format(shown, more),
                    posed[0])


def check_skin_weights(result, spec, settings, limits):
    """スキンウェイト（V-068 / V-069 / V-070 / V-071 / V-072 / V-074）。

    **正規化のズレはエラーにしない。**UE は取り込み時に正規化するうえ、
    手付けウェイトでは 1% 程度のズレが普通に出る。これで書き出しを止めると
    実運用に耐えない（docs/14 §1.5 の実測を踏まえた判断）。
    一方「ウェイトが無い頂点」は UE で原点に吸い寄せられる実害があるのでエラー。
    """
    skin = spec.skin
    if skin is None:
        return

    # V-068: 未割当の頂点（実害あり）
    if skin.unassigned_count:
        samples = ", ".join(str(index) for index in skin.unassigned_samples)
        result.error("V-068",
                     "スキンウェイトが無い頂点が {0} 個あります"
                     "（頂点 index: {1} ...）。"
                     "UE では原点に吸い寄せられます".format(
                         skin.unassigned_count, samples),
                     spec.asset_name)

    # V-070: 合計が実質ゼロ（未割当と同じ実害）
    if skin.zero_total_count:
        result.error("V-070",
                     "ウェイトの合計が実質ゼロの頂点が {0} 個あります。"
                     "未割当と同じ結果になります".format(skin.zero_total_count),
                     spec.asset_name)

    # V-069: 正規化のズレ（警告 + 書き出し時に正規化）
    if skin.deviating_count:
        tolerance = skeletal.weight_tolerance(settings)
        result.warn("V-069",
                    "ウェイトの合計が 1.0 でない頂点が {0}/{1} 個あります"
                    "（最大ズレ {2:.4f} / 最悪の合計 {3}）。"
                    "許容 {4} を超えているため、"
                    "**書き出し時に複製側で正規化します**（元のシーンは変更しません）".format(
                        skin.deviating_count, skin.vertex_count,
                        skin.max_deviation, skin.worst_total, tolerance),
                    spec.asset_name, auto_fixed=True)

    # V-071: 影響ボーン数（Q-06 により警告）
    limit = skeletal.expected_influence_limit(settings)
    if skin.over_influence_count:
        result.warn("V-071",
                    "1頂点あたりの影響ボーン数が上限を超えている頂点が "
                    "{0} 個あります（最大 {1} > 上限 {2}）。"
                    "UE 側で切り捨てられます".format(
                        skin.over_influence_count, skin.max_influences, limit),
                    spec.asset_name)

    # V-072: ウェイトを持たない deform ボーン
    if skin.bones_without_weight:
        shown = ", ".join(skin.bones_without_weight[:5])
        more = ("" if len(skin.bones_without_weight) <= 5
                else " ほか{0}本".format(len(skin.bones_without_weight) - 5))
        result.warn("V-072",
                    "ウェイトが1つも割り当たっていないボーンがあります"
                    "（{0}{1}）。そのボーンを動かしてもメッシュは変形しません".format(
                        shown, more),
                    skin.bones_without_weight[0])

    # V-074: ボーンに対応しない頂点グループ
    if skin.unknown_groups:
        shown = ", ".join(skin.unknown_groups[:5])
        result.warn("V-074",
                    "ボーンに対応しない頂点グループがあります（{0}）。"
                    "UE には渡りません".format(shown),
                    spec.asset_name)


def check_shape_keys(result, spec, settings):
    """Shape Key -> Morph Target（V-075 / V-076）。"""
    info = spec.shape_keys
    if info is None or not info.basis_name:
        return

    skeletal_settings = settings.get("skeletalMesh") or {}
    import_morphs = bool((skeletal_settings.get("morphTargets")
                          or {}).get("import", True))

    # V-075: 先頭ブロックは基準形状として扱われ、UE には渡らない
    if info.basis_name != "Basis":
        result.warn("V-075",
                    "Shape Key の先頭が 'Basis' ではなく '{0}' です。"
                    "先頭は基準形状として扱われるため '{0}' は "
                    "Morph Target になりません。"
                    "UE に渡るのは {1} です".format(
                        info.basis_name,
                        ", ".join(info.morph_names) if info.morph_names else "（なし）"),
                    info.mesh_name or spec.asset_name)

    # V-076: Shape Key があるのに取り込まない設定
    if info.morph_names and not import_morphs:
        result.warn("V-076",
                    "Shape Key が {0} 個ありますが、プリセットの "
                    "skeletalMesh.morphTargets.import が false のため"
                    "取り込まれません".format(len(info.morph_names)),
                    spec.asset_name)

    for name in info.morph_names:
        problem = naming.name_problem(name, allow_leading_digit=False)
        if problem:
            result.warn("V-075",
                        "Morph Target 名が規約違反です: {0}（'{1}'）".format(problem, name),
                        name)


def check_skeleton_settings(result, settings):
    """Skeleton の流用設定（V-079）。"""
    skeleton = settings.get("skeleton") or {}
    mode = skeleton.get("mode", "create_new")
    if mode not in ("create_new", "reuse"):
        result.error("V-079",
                     "skeleton.mode は 'create_new' か 'reuse' です"
                     "（指定値: '{0}'）".format(mode))
    if mode == "reuse" and not skeleton.get("assetPath"):
        result.error("V-079",
                     "skeleton.mode が 'reuse' ですが "
                     "skeleton.assetPath（既存 Skeleton のパス）が"
                     "指定されていません")
    path = skeleton.get("assetPath")
    if path:
        problem = naming.ue_path_problem(path)
        if problem:
            result.error("V-079",
                         "skeleton.assetPath が不正です: {0}".format(problem), path)
    compatibility = skeleton.get("compatibilityMode", "exact")
    if compatibility not in ("exact", "subset"):
        result.error("V-079",
                     "skeleton.compatibilityMode は 'exact' か 'subset' です"
                     "（指定値: '{0}'）".format(compatibility))


def check_skeletal(result, spec, settings, allow_leading_digit=False):
    """Skeletal Mesh のまとめ（V-060..V-079）。"""
    armature_obj = _obj(spec.armature_name) if spec.armature_name else None

    if armature_obj is None or armature_obj.type != "ARMATURE":
        result.error("V-060",
                     "Skeletal Mesh として書き出すには Armature が必要です。"
                     "メッシュに Armature モジファイアを付け、"
                     "Armature を同じコレクション（または選択）に含めてください",
                     spec.asset_name)
        return

    limits = settings.get("limits") or {}
    check_armature_object(result, armature_obj)
    check_bone_names(result, spec.bones, allow_leading_digit)
    check_bone_hierarchy(result, spec, armature_obj, settings, limits)
    check_pose(result, armature_obj)
    check_skin_weights(result, spec, settings, limits)
    check_shape_keys(result, spec, settings)
    check_skeleton_settings(result, settings)


def validate_asset(spec, settings, ue_path=None, output_dir=None, blend_path=None,
                   texture_dir=None):
    """AssetSpec 1件を検証して IssueList を返す。

    `texture_dir` を渡すとテクスチャの発見規則（R-4 / V-110〜V-114）も見る。
    省略すると**テクスチャの検査はしない**（従来の呼び出しは挙動が変わらない）。
    """
    result = issues_mod.IssueList()
    validation = settings.get("validation", {})
    allow_leading_digit = bool(validation.get("allowLeadingDigitInNames"))
    auto_fix = bool(validation.get("autoFix", True))
    limits = settings.get("limits", {})

    mesh_names = spec.all_mesh_object_names()
    if not mesh_names:
        result.error("V-001", "書き出し対象のメッシュが1つもありません", spec.asset_name)
        return result

    # 命名
    problem = naming.name_problem(spec.asset_name, allow_leading_digit)
    if problem:
        result.error("V-002", "アセット名が規約違反です: {0}".format(problem), spec.asset_name)

    all_names = list(mesh_names)
    all_names.extend(e.object_name for e in spec.collisions)
    all_names.extend(s.object_name for s in spec.sockets)
    collection_names = lod_collection_names(spec)
    check_names(result, all_names, allow_leading_digit, "オブジェクト名")
    check_names(result, collection_names, allow_leading_digit, "コレクション名")

    material_names = [name for _, name in spec.material_slots]
    check_materials(result, material_names)

    if spec.extra_collections:
        result.warn("V-045b",
                    "予約語でないサブコレクションがあります（{0}）。"
                    "LOD<n> / Collision / Sockets 以外は無視されます".format(
                        ", ".join(spec.extra_collections)),
                    spec.asset_name)

    # シーン・環境
    check_scene_units(result)
    check_environment(result, output_dir, blend_path)
    if ue_path:
        whitelist = (settings.get("destination") or {}).get("pathWhitelist")
        check_destination_path(result, ue_path, whitelist)

    # オブジェクト単位
    for name in mesh_names:
        obj = _obj(name)
        if obj is None:
            result.error("V-001", "オブジェクトが見つかりません: {0}".format(name), name)
            continue
        check_object_transform(result, obj, auto_fix)
        check_object_misc(result, obj, spec.is_skeletal)
        check_mesh_geometry(result, obj, limits, auto_fix, spec.is_skeletal)

    for entry in spec.collisions:
        obj = _obj(entry.object_name)
        if obj is not None:
            check_object_transform(result, obj, auto_fix)

    check_collisions(result, spec, limits)
    check_lods(result, spec)
    check_sockets(result, spec)
    check_textures(result, spec, texture_dir)

    if spec.is_skeletal:
        check_skeletal(result, spec, settings, allow_leading_digit)

    # strict モード: 警告もエラー扱いにする
    if validation.get("strict"):
        promoted = issues_mod.IssueList()
        for issue in result:
            if issue.severity == issues_mod.WARNING and not issue.auto_fixed:
                promoted.add(issue.rule_id, issues_mod.ERROR,
                             issue.message + "（strict モードのためエラー扱い）",
                             issue.target, issue.auto_fixed)
            else:
                promoted.add(issue.rule_id, issue.severity, issue.message,
                             issue.target, issue.auto_fixed)
        return promoted

    return result


def validate_scene_objects(settings, objects=None):
    """シーン全体（または指定オブジェクト）をざっと検証する。

    アセット構成を問わず「明らかな事故」を洗い出す Dry Run 用。
    """
    result = issues_mod.IssueList()
    validation = settings.get("validation", {})
    allow_leading_digit = bool(validation.get("allowLeadingDigitInNames"))
    limits = settings.get("limits", {})

    if objects is None:
        objects = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    if not objects:
        result.error("V-001", "メッシュオブジェクトが1つもありません")
        return result

    check_scene_units(result)
    check_names(result, [o.name for o in objects], allow_leading_digit, "オブジェクト名")
    check_names(result, [c.name for c in bpy.data.collections], allow_leading_digit,
                "コレクション名")
    check_materials(result, [m.name for m in bpy.data.materials])

    for obj in objects:
        check_object_transform(result, obj, auto_fix=False)
        check_object_misc(result, obj)
        if obj.type == "MESH":
            check_mesh_geometry(result, obj, limits, auto_fix=False)
            parsed = naming.parse_collision_name(obj.name)
            if parsed and parsed[0] == "convex":
                if not is_closed_manifold(obj.data):
                    result.error("V-042",
                                 "凸包コリジョンが閉じた多面体になっていません",
                                 obj.name)
                bad = non_convex_face_count(obj.data)
                if bad:
                    result.warn("V-043",
                                "凸ではありません（{0}/{1} 面が凸条件を満たしません）".format(
                                    bad, len(obj.data.polygons)),
                                obj.name)
    return result
