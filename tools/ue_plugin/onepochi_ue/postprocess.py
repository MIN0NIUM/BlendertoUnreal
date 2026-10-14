"""インポート後のポストプロセスと実測（UE 側 [C4]）。

**設計方針 D-6**: UE 側の「お作法」はインポータ任せにせずポストプロセスに寄せる。
インポータの対応範囲に引きずられず、Interchange の仕様変更にも強くなる。

UE 5.8 の API は未検証の部分が多いため、**すべての操作を個別に try で囲み、
失敗を例外にせずレポートへ記録する**。1つの API が無くても全体は完走させる。
"""

import unreal

#: 同名ソケットを消す試行回数の上限（無限ループ防止）。
#: インポータ製 + 前回のポストプロセス製で 2 個が通常。
#: 上限に達したら「消せない」ことをレポートに残す
MAX_DUPLICATE_SOCKETS = 8


def _record(sink, label, error):
    sink.append("{0}: {1}: {2}".format(label, type(error).__name__, error))


def attempt(sink, label, func, *args, **kwargs):
    """func を呼び、失敗したら sink に記録して None を返す。"""
    try:
        return func(*args, **kwargs)
    except Exception as error:  # noqa: BLE001 - 失敗理由を残して完走させたい
        _record(sink, label, error)
        return None


def set_property(sink, obj, name, value):
    """set_editor_property を安全に呼ぶ。成功したら True。"""
    try:
        obj.set_editor_property(name, value)
        return True
    except Exception as error:  # noqa: BLE001
        _record(sink, "set {0}".format(name), error)
        return False


def get_property(sink, obj, name, default=None):
    try:
        return obj.get_editor_property(name)
    except Exception as error:  # noqa: BLE001
        _record(sink, "get {0}".format(name), error)
        return default


def static_mesh_subsystem(sink):
    """StaticMeshEditorSubsystem を取得する。取得できなければ None。

    **UE 5.8 実測の注意**: `-run=pythonscript` の commandlet モードでは
    エディタサブシステムが初期化されないため **None が返る**。
    そのため実測・設定はできる限り `StaticMesh` 自身のメソッド／プロパティで行い、
    サブシステムは「あれば使う」補助に留める。
    """
    if not hasattr(unreal, "StaticMeshEditorSubsystem"):
        return None
    subsystem = attempt(sink, "get_editor_subsystem(StaticMeshEditorSubsystem)",
                        unreal.get_editor_subsystem, unreal.StaticMeshEditorSubsystem)
    return subsystem


def collision_element_counts(mesh, sink):
    """body_setup.agg_geom から簡易コリジョンの形状数を数える。

    `StaticMeshEditorSubsystem.get_simple_collision_count` は commandlet では
    使えないため、こちらを正とする。
    """
    counts = {}
    body_setup = get_property(sink, mesh, "body_setup")
    if body_setup is None:
        return counts
    agg = get_property(sink, body_setup, "agg_geom")
    if agg is None:
        return counts
    for key, field in (("convex", "convex_elems"), ("box", "box_elems"),
                       ("sphere", "sphere_elems"), ("capsule", "sphyl_elems")):
        try:
            counts[key] = len(agg.get_editor_property(field))
        except Exception as error:  # noqa: BLE001
            _record(sink, "agg_geom.{0}".format(field), error)
    counts["total"] = sum(v for v in counts.values() if isinstance(v, int))
    return counts


# --- 適用 ---------------------------------------------------------------------


def apply_nanite(mesh, settings, sink):
    """nanite.enabled / fallbackRelativeError を適用する。"""
    if not settings:
        return
    current = get_property(sink, mesh, "nanite_settings")
    if current is None:
        return
    changed = False
    if "enabled" in settings:
        if set_property(sink, current, "enabled", bool(settings["enabled"])):
            changed = True
    if "fallbackRelativeError" in settings:
        for name in ("fallback_relative_error", "fallback_percent_triangles"):
            try:
                current.set_editor_property(name, float(settings["fallbackRelativeError"]))
                changed = True
                break
            except Exception:
                continue
    if changed:
        set_property(sink, mesh, "nanite_settings", current)


#: ライトマップ UV チャンネルのプロパティ名。
#: UE 5.8 実測では `light_map_coordinate_index`（`lightmap_` ではない）。
LIGHTMAP_INDEX_PROPERTIES = ("light_map_coordinate_index", "lightmap_coordinate_index")


def _set_first_available(mesh, property_names, value, sink, label):
    """候補のプロパティ名を順に試す。UE バージョン差を吸収する。"""
    errors = []
    for name in property_names:
        try:
            mesh.set_editor_property(name, value)
            return name
        except Exception as error:  # noqa: BLE001
            errors.append("{0}: {1}".format(name, error))
    sink.append("{0} を設定できませんでした（試した候補: {1}）".format(
        label, ", ".join(property_names)))
    return None


def _get_first_available(mesh, property_names, sink, label):
    for name in property_names:
        try:
            return name, mesh.get_editor_property(name)
        except Exception:
            continue
    sink.append("{0} を取得できませんでした（試した候補: {1}）".format(
        label, ", ".join(property_names)))
    return None, None


def apply_lightmap(mesh, settings, sink):
    if not settings:
        return
    if "coordinateIndex" in settings:
        _set_first_available(mesh, LIGHTMAP_INDEX_PROPERTIES,
                             int(settings["coordinateIndex"]), sink,
                             "ライトマップ UV チャンネル")
    if "resolution" in settings:
        set_property(sink, mesh, "light_map_resolution", int(settings["resolution"]))


#: KAggregateGeom の形状配列プロパティ名
AGG_GEOM_FIELDS = ("convex_elems", "box_elems", "sphere_elems", "sphyl_elems",
                   "tapered_capsule_elems", "level_set_elems", "skinned_level_set_elems")


def clear_simple_collision(mesh, sink, reason=None):
    """簡易コリジョンを全消去する。

    **UE 5.8 実測**: コリジョンメッシュを含まない FBX をインポートすると、
    Interchange が**凸コリジョンを1個自動生成する**。マニフェストで
    `collision.simple = "none"` と宣言した場合は、宣言どおりの状態にする
    （方針 D-6: UE 側のお作法はポストプロセスに寄せる）。

    戻り値: 消去した形状数（取得できなければ None）
    """
    body_setup = get_property(sink, mesh, "body_setup")
    if body_setup is None:
        return None
    agg = get_property(sink, body_setup, "agg_geom")
    if agg is None:
        return None

    removed = 0
    for field in AGG_GEOM_FIELDS:
        try:
            current = agg.get_editor_property(field)
        except Exception:
            continue  # このバージョンに存在しないフィールド
        if not current:
            continue
        removed += len(current)
        try:
            agg.set_editor_property(field, [])
        except Exception as error:  # noqa: BLE001
            _record(sink, "agg_geom.{0} のクリア".format(field), error)

    if removed:
        if not set_property(sink, body_setup, "agg_geom", agg):
            return None
        sink.append(
            "{0}、インポータが自動生成した簡易コリジョン {1} 個を削除しました".format(
                reason or "マニフェストが collision.simple = \"none\" のため", removed)
        )
    return removed


def apply_collision(mesh, settings, sink):
    """コリジョンの宣言を UE 側に反映する。

    ・`simple = "none"` のときは自動生成されたコリジョンを消す
    ・`simple = "fromDCC"` のときは FBX から入ったものをそのまま使う
    ・`complexMode` を body_setup.collision_trace_flag に反映する
    """
    result = {}
    if not settings:
        return result

    if settings.get("simple") == "none":
        result["clearedShapes"] = clear_simple_collision(mesh, sink)

    mode = settings.get("complexMode", "default")
    mapping = {
        "default": "CTF_USE_DEFAULT",
        "simpleAsComplex": "CTF_USE_SIMPLE_AS_COMPLEX",
        "complexAsSimple": "CTF_USE_COMPLEX_AS_SIMPLE",
    }
    flag_name = mapping.get(mode)
    if flag_name is None:
        sink.append("collision.complexMode が不明な値です: {0}".format(mode))
        return result
    enum = getattr(unreal, "CollisionTraceFlag", None)
    if enum is None:
        sink.append("unreal.CollisionTraceFlag がこの UE に存在しません")
        return result
    flag = getattr(enum, flag_name, None)
    if flag is None:
        sink.append("CollisionTraceFlag.{0} が存在しません".format(flag_name))
        return result
    body_setup = get_property(sink, mesh, "body_setup")
    if body_setup is None:
        return result
    if set_property(sink, body_setup, "collision_trace_flag", flag):
        result["complexMode"] = mode
    return result


def build_settings_provider(sink):
    """LOD Build Settings を読み書きできるオブジェクトを返す（無ければ None）。

    `StaticMeshEditorSubsystem` が優先。commandlet モードでは取得できない
    （P-5）ため、非推奨の `EditorStaticMeshLibrary` にフォールバックする。
    **読み取り（スナップショット）と書き込みで同じ経路を使う**ために
    関数として切り出してある（`reimport.capture` が使う）。
    """
    subsystem = static_mesh_subsystem(sink)
    if subsystem is not None and hasattr(subsystem, "get_lod_build_settings"):
        return subsystem
    legacy = getattr(unreal, "EditorStaticMeshLibrary", None)
    if legacy is not None and hasattr(legacy, "get_lod_build_settings"):
        # この関数は読み取り・書き込みの両方から何度も呼ばれるので、
        # 同じ警告でレポートを埋めない
        message = ("StaticMeshEditorSubsystem が使えないため"
                   "EditorStaticMeshLibrary（非推奨）で Build Settings を扱います")
        if message not in sink:
            sink.append(message)
        return legacy
    return None


def apply_build_settings(mesh, settings, sink, lod_index=0):
    """LOD0 の Build Settings を適用する。"""
    if not settings:
        return
    provider = build_settings_provider(sink)

    if provider is None:
        sink.append(
            "Build Settings を適用できません。StaticMeshEditorSubsystem が取得できず"
            "（commandlet モードでは初期化されない）、EditorStaticMeshLibrary も使えません。"
            "エディタ UI 経由の実行では適用されます"
        )
        return
    subsystem = provider
    build = attempt(sink, "get_lod_build_settings", subsystem.get_lod_build_settings,
                    mesh, lod_index)
    if build is None:
        return

    mapping = {
        "recomputeNormals": "recompute_normals",
        "recomputeTangents": "recompute_tangents",
        "removeDegenerates": "remove_degenerates",
        "generateDistanceField": "generate_distance_field_as_if_two_sided",
        "buildScale": "build_scale3d",
    }
    for key, prop in mapping.items():
        if key not in settings:
            continue
        value = settings[key]
        if key == "buildScale":
            try:
                value = unreal.Vector(float(value[0]), float(value[1]), float(value[2]))
            except Exception as error:  # noqa: BLE001
                _record(sink, "buildScale", error)
                continue
        elif key == "generateDistanceField":
            # 相当プロパティ名が UE バージョンで揺れるため候補を順に試す
            applied = False
            for candidate in ("generate_distance_field_as_if_two_sided",
                              "generate_distance_field"):
                try:
                    build.set_editor_property(candidate, bool(value))
                    applied = True
                    break
                except Exception:
                    continue
            if not applied:
                sink.append("generateDistanceField に相当するプロパティが見つかりません")
            continue
        set_property(sink, build, prop, value)

    attempt(sink, "set_lod_build_settings", subsystem.set_lod_build_settings,
            mesh, lod_index, build)


def remove_existing_sockets(mesh, names, sink):
    """同名のソケットを**先に消す**。戻り値は消した個数。

    **Interchange は FBX の `SOCKET_` 命名からソケットを自前で作る**
    （UCX_ のコリジョンと同じ / 実測で確認）。そこにポストプロセスで
    追加すると**同名のソケットが2個**になり、`find_socket()` は
    先に見つかった側（インポータ製）を返す。

    実測したインポータ製ソケットの値（S-08 / UE 5.8.3）:
        location (0, -0, 100)      ... 正しい
        rotation (roll -76.2103, pitch -15.6831, yaw 20.228)
                                   ... Blender の X 回転に -90° が残っている
        scale    (100, 100, 100)   ... **単位換算の 100 倍が混入している**

    scale が 100 倍なのは明確に誤り（ソケットに付けたものが 100 倍になる）。
    そこで方針 D-6 のとおり**インポータ製を捨てて宣言した値で作り直す**。
    消すのは**マニフェストが宣言した名前だけ**なので、UE 側で手で足した
    別名のソケットは残る（同名のものは消える。T-7 の再インポート設定保持で扱う）。
    """
    if not names or not hasattr(mesh, "find_socket") \
            or not hasattr(mesh, "remove_socket"):
        return 0
    removed = 0
    for name in names:
        if not name:
            continue
        # 同名が複数あることがあるので、無くなるまで繰り返す。
        # **`remove_socket()` は void なので戻り値で成否を判断しない**
        # （`attempt()` の None を失敗と読むと1個で止まる）。
        # 進捗は `find_socket()` が None を返すことで判定する
        for _ in range(MAX_DUPLICATE_SOCKETS):
            existing = attempt(sink, "find_socket({0})".format(name),
                               mesh.find_socket, name)
            if existing is None:
                break
            try:
                mesh.remove_socket(existing)
            except Exception as error:  # noqa: BLE001
                _record(sink, "remove_socket({0})".format(name), error)
                break
            removed += 1
        else:
            sink.append("ソケット '{0}' を {1} 回消しても残っています。"
                        "重複している可能性があります".format(
                            name, MAX_DUPLICATE_SOCKETS))
    if removed:
        sink.append("既存のソケット {0} 個を削除し、マニフェストの値で"
                    "作り直します（インポータが自動生成した分を含む）".format(removed))
    return removed


def apply_sockets(mesh, sockets, sink):
    """マニフェストのソケットを生成する（インポータ任せにしない / D-6）。"""
    if not sockets:
        return 0
    if not hasattr(unreal, "StaticMeshSocket"):
        sink.append("unreal.StaticMeshSocket がこの UE に存在しません")
        return 0

    remove_existing_sockets(mesh, [e.get("name") for e in sockets], sink)

    created = 0
    for entry in sockets:
        socket = attempt(sink, "StaticMeshSocket()", unreal.StaticMeshSocket)
        if socket is None:
            continue
        name = entry.get("name")
        location = entry.get("location") or [0.0, 0.0, 0.0]
        rotation = entry.get("rotation") or [0.0, 0.0, 0.0]
        scale = entry.get("scale") or [1.0, 1.0, 1.0]
        set_property(sink, socket, "socket_name", name)
        set_property(sink, socket, "relative_location",
                     unreal.Vector(*[float(v) for v in location]))
        set_property(sink, socket, "relative_rotation",
                     unreal.Rotator(float(rotation[0]), float(rotation[1]), float(rotation[2])))
        set_property(sink, socket, "relative_scale",
                     unreal.Vector(*[float(v) for v in scale]))
        # **`add_socket()` は void を返す**（P-18）。戻り値で成否を判断すると
        # 常に失敗に見える（実測: S-08 で socketsCreated が 0 のまま）。
        # 成否は `find_socket()` の**観測**で確かめる
        attempt(sink, "add_socket({0})".format(name), mesh.add_socket, socket)
        if name and hasattr(mesh, "find_socket"):
            if attempt(sink, "find_socket({0})".format(name),
                       mesh.find_socket, name) is not None:
                created += 1
            else:
                sink.append("ソケット '{0}' を追加できませんでした".format(name))
        else:
            created += 1
    return created


def update_struct_in_array(sink, array, index, properties, label="配列"):
    """構造体の**配列の要素**を書き換える。

    **UE Python では構造体配列の要素を取り出すとコピーになる**（実測 / docs/99 P-23）。

        array[index].set_editor_property(name, value)   # ← 成功を返すが何も起きない

    値型なので、書き換えたあと **`array[index] = struct` で代入し直す**必要がある。
    これを忘れると「設定したつもりで効いていない」（P-15 / P-18 と同根）。

    `properties` は `((名前, 値), ...)`。戻り値は書き換えた struct（失敗なら None）。
    """
    try:
        struct = array[index]
    except Exception as error:  # noqa: BLE001
        _record(sink, "{0}[{1}] の取得".format(label, index), error)
        return None

    ok = True
    for name, value in properties:
        ok = set_property(sink, struct, name, value) and ok
    if not ok:
        return None

    try:
        array[index] = struct       # ★ ここが本質。代入し直さないと効かない
    except Exception as error:  # noqa: BLE001
        _record(sink, "{0}[{1}] への代入".format(label, index), error)
        return None
    return struct


def apply_material_slot_names(mesh, material_slots, sink):
    """マテリアルスロット名を DCC 側の名前に合わせる（割当順の固定）。

    **実測して確かめる**（docs/99 P-23）: 以前の実装は配列要素のコピーを
    書き換えていたため**一度も効いていなかった**。FBX 由来のスロット名が
    たまたま同じだったので比較項目が緑のままだった（K-8 の典型）。
    """
    if not material_slots:
        return 0
    materials = get_property(sink, mesh, "static_materials")
    if not materials:
        return 0
    renamed = 0
    wanted = {}
    for entry in material_slots:
        index = entry.get("index")
        slot_name = entry.get("slotName")
        if index is None or slot_name is None:
            continue
        if index >= len(materials):
            sink.append("マテリアルスロット index {0} が UE 側に存在しません"
                        "（UE 側は {1} 個）".format(index, len(materials)))
            continue
        if update_struct_in_array(sink, materials, index,
                                  (("material_slot_name", slot_name),),
                                  label="static_materials") is not None:
            wanted[index] = slot_name
            renamed += 1
    if renamed:
        set_property(sink, mesh, "static_materials", materials)
        # 書き戻したあと読み直して**本当に変わったか**を確かめる
        observed = get_property(sink, mesh, "static_materials") or []
        for index, slot_name in wanted.items():
            if index >= len(observed):
                continue
            actual = get_property(sink, observed[index], "material_slot_name")
            if actual is not None and str(actual) != str(slot_name):
                sink.append(
                    "マテリアルスロット {0} の名前が '{1}' のままです"
                    "（'{2}' にしようとしました）".format(index, actual, slot_name))
    return renamed


#: `apply_static_mesh` が適用できるカテゴリ（`reimport.CATEGORIES` の部分集合）。
#: `lod` はここでは扱わない（別モジュールの担当 / T-5）
APPLIED_CATEGORIES = ("nanite", "lightmap", "collision", "buildSettings",
                      "sockets", "materials")


def apply_static_mesh(mesh, asset, sink, categories=None):
    """StaticMesh のポストプロセスを適用する。

    `categories` に `reimport.CATEGORIES` の一部を渡すと**そのカテゴリだけ**
    適用し、適用しなかったものを戻り値の `skipped` に記録する（T-7）。
    `None`（既定）のときは**従来どおり全適用**する（後方互換）。
    """
    block = asset.get("staticMesh") or {}
    applied = {}
    allowed = None if categories is None else set(categories)

    def enabled(name):
        return allowed is None or name in allowed

    if enabled("nanite"):
        apply_nanite(mesh, block.get("nanite"), sink)
        applied["nanite"] = True
    if enabled("lightmap"):
        apply_lightmap(mesh, block.get("lightmap"), sink)
        applied["lightmap"] = True
    if enabled("collision"):
        applied["collision"] = apply_collision(mesh, block.get("collision"), sink)
    if enabled("buildSettings"):
        apply_build_settings(mesh, block.get("buildSettings"), sink)
        applied["buildSettings"] = True
    if enabled("sockets"):
        applied["socketsCreated"] = apply_sockets(mesh, block.get("sockets"), sink)
    if enabled("materials"):
        applied["materialSlotsRenamed"] = apply_material_slot_names(
            mesh, asset.get("materialSlots"), sink)

    skipped = [c for c in APPLIED_CATEGORIES if not enabled(c)]
    if skipped:
        applied["skipped"] = skipped
        sink.append("reimportPolicy により {0} の適用をスキップしました"
                    "（UE 側の設定を保持します）".format(", ".join(skipped)))
    return applied


# --- 実測 ---------------------------------------------------------------------


def measure_static_mesh(mesh, sink, expected_socket_names=None):
    """インポート結果を実測する。座標系・単位の検証の根拠になる。

    **UE 5.8 実測**: `StaticMeshEditorSubsystem` は commandlet モードで取得できない。
    そのため **`StaticMesh` 自身のメソッド**（`get_num_triangles` 等）を正とする。
    """
    measured = {}

    box = attempt(sink, "get_bounding_box", mesh.get_bounding_box)
    if box is not None:
        try:
            minimum = box.min
            maximum = box.max
            measured["boundsMinUu"] = [round(minimum.x, 4), round(minimum.y, 4),
                                       round(minimum.z, 4)]
            measured["boundsMaxUu"] = [round(maximum.x, 4), round(maximum.y, 4),
                                       round(maximum.z, 4)]
            measured["dimensionsUu"] = [
                round(abs(maximum.x - minimum.x), 4),
                round(abs(maximum.y - minimum.y), 4),
                round(abs(maximum.z - minimum.z), 4),
            ]
        except Exception as error:  # noqa: BLE001
            _record(sink, "bounding box 展開", error)

    # StaticMesh 自身のメソッド（commandlet でも動く）
    for key, method, args in (
        ("lodCount", "get_num_lods", ()),
        ("triangleCountLod0", "get_num_triangles", (0,)),
        ("renderVertexCountLod0", "get_num_vertices", (0,)),
        ("sectionCountLod0", "get_num_sections", (0,)),
        ("uvChannelCountLod0", "get_num_tex_coords", (0,)),
        ("naniteTriangleCount", "get_num_nanite_triangles", ()),
    ):
        function = getattr(mesh, method, None)
        if function is None:
            continue
        value = attempt(sink, method, function, *args)
        if value is not None:
            measured[key] = value

    collision = collision_element_counts(mesh, sink)
    if collision:
        measured["collisionElementCounts"] = collision
        measured["simpleCollisionCount"] = collision.get("total")
        measured["convexCollisionCount"] = collision.get("convex")

    materials = get_property(sink, mesh, "static_materials", [])
    try:
        measured["materialSlotNames"] = [
            str(m.get_editor_property("material_slot_name")) for m in materials
        ]
    except Exception as error:  # noqa: BLE001
        _record(sink, "material_slot_name 読み取り", error)

    nanite = get_property(sink, mesh, "nanite_settings")
    if nanite is not None:
        enabled = get_property(sink, nanite, "enabled")
        if enabled is not None:
            measured["naniteEnabled"] = bool(enabled)

    name, value = _get_first_available(mesh, LIGHTMAP_INDEX_PROPERTIES, sink,
                                       "ライトマップ UV チャンネル")
    if name is not None:
        measured["lightmapCoordinateIndex"] = value
    value = get_property(sink, mesh, "light_map_resolution")
    if value is not None:
        measured["lightMapResolution"] = value

    # `sockets` プロパティは protected で読めないが、`get_sockets_by_tag("")` で
    # **全ソケットを列挙できる**（実測）。`find_socket()` は先頭1個しか返さないので、
    # **重複を検出するにはこちらが必要**（インポータ製と重なる事故の検知）
    all_sockets = attempt(sink, 'get_sockets_by_tag("")',
                          mesh.get_sockets_by_tag, "") \
        if hasattr(mesh, "get_sockets_by_tag") else None
    if all_sockets is not None:
        names = []
        for socket in all_sockets:
            value = get_property(sink, socket, "socket_name")
            names.append(str(value) if value is not None else "")
        measured["socketNamesAll"] = names
        measured["socketCountTotal"] = len(names)

    # 期待名を引いて確認する
    measured["socketNamesFound"] = []
    if expected_socket_names:
        found = []
        missing = []
        transforms = {}
        for socket_name in expected_socket_names:
            socket = attempt(sink, "find_socket({0})".format(socket_name),
                             mesh.find_socket, socket_name) \
                if hasattr(mesh, "find_socket") else None
            if socket is None:
                missing.append(socket_name)
                continue
            found.append(socket_name)
            # **位置と回転も実測する。**「ソケットが存在する」だけでは
            # 座標系の変換が正しいことの証拠にならない（T-9 / docs/14 §4）
            transforms[socket_name] = {
                "location": _vector_list(
                    get_property(sink, socket, "relative_location")),
                "rotation": _rotator_list(
                    get_property(sink, socket, "relative_rotation")),
                "scale": _vector_list(
                    get_property(sink, socket, "relative_scale")),
            }
        measured["socketNamesFound"] = found
        measured["socketTransforms"] = transforms
        if missing:
            measured["socketNamesMissing"] = missing

    return measured


def _vector_list(value, digits=4):
    """`unreal.Vector` を [x, y, z] にする。読めなければ None。"""
    if value is None:
        return None
    try:
        return [round(float(value.x), digits), round(float(value.y), digits),
                round(float(value.z), digits)]
    except Exception:  # noqa: BLE001
        return None


def _rotator_list(value, digits=4):
    """`unreal.Rotator` を [roll, pitch, yaw] にする（マニフェストと同じ並び）。

    マニフェストの `rotation` は UE 基底での XYZ オイラー角（度）で、
    `unreal.Rotator(roll, pitch, yaw)` にそのまま渡している
    （`apply_sockets()`）。実測も同じ並びで返して突き合わせられるようにする。
    """
    if value is None:
        return None
    try:
        return [round(float(value.roll), digits),
                round(float(value.pitch), digits),
                round(float(value.yaw), digits)]
    except Exception:  # noqa: BLE001
        return None


def compare_with_manifest(asset, measured):
    """マニフェストの stats（DCC 側の予測）と UE 側の実測を突き合わせる。

    これが座標系・単位の検証そのもの。戻り値は比較結果の辞書。
    """
    stats = asset.get("stats") or {}
    comparison = {}

    expected = stats.get("dimensionsCm")
    actual = measured.get("dimensionsUu")
    if expected and actual:
        comparison["dimensions"] = {
            "expectedCm": expected,
            "actualUu": actual,
            "deltaPerAxis": [round(actual[i] - expected[i], 4) for i in range(3)],
            "matches": all(abs(actual[i] - expected[i]) < 0.05 for i in range(3)),
        }

    expected_triangles = stats.get("triangleCount")
    actual_triangles = measured.get("triangleCountLod0")
    if expected_triangles is not None and actual_triangles is not None:
        comparison["triangleCount"] = {
            "expected": expected_triangles,
            "actual": actual_triangles,
            "matches": int(expected_triangles) == int(actual_triangles),
        }

    # 頂点数は「DCC のソース頂点数」と「UE のレンダー頂点数」で意味が違う
    # （UV シーム・分割法線で UE 側が増える）ため、一致判定はしない
    expected_vertices = stats.get("vertexCount")
    actual_vertices = measured.get("renderVertexCountLod0")
    if expected_vertices is not None and actual_vertices is not None:
        comparison["vertexCount"] = {
            "dccSourceVertices": expected_vertices,
            "ueRenderVertices": actual_vertices,
            "note": "UV シームと分割法線により UE 側が増えるのは正常。一致判定はしない",
        }

    expected_uv = stats.get("uvChannelCount")
    actual_uv = measured.get("uvChannelCountLod0")
    if expected_uv is not None and actual_uv is not None:
        comparison["uvChannelCount"] = {
            "expected": expected_uv,
            "actual": actual_uv,
            # UE はライトマップ UV を自動生成して 1ch 増やすことがある
            "matches": int(actual_uv) >= int(expected_uv),
            "note": "UE 側はライトマップ UV を自動生成して増える場合がある（増加は正常）",
        }

    expected_slots = [s.get("slotName") for s in (asset.get("materialSlots") or [])]
    actual_slots = measured.get("materialSlotNames")
    if expected_slots and actual_slots is not None:
        comparison["materialSlots"] = {
            "expected": expected_slots,
            "actual": actual_slots,
            "matches": expected_slots == actual_slots,
        }

    collision_block = ((asset.get("staticMesh") or {}).get("collision") or {})
    expected_shapes = collision_block.get("shapes") or []
    actual_simple = measured.get("simpleCollisionCount")
    if actual_simple is not None:
        expected_by_type = {}
        for shape in expected_shapes:
            shape_type = shape.get("type")
            expected_by_type[shape_type] = expected_by_type.get(shape_type, 0) + 1
        comparison["collisionShapes"] = {
            "expectedCount": len(expected_shapes),
            "expectedByType": expected_by_type,
            "actualCount": actual_simple,
            "actualByType": {k: v for k, v in
                             (measured.get("collisionElementCounts") or {}).items()
                             if k != "total"},
            "matches": len(expected_shapes) == actual_simple,
            "note": "DCC 側にコリジョンが無くても UE が自動生成する場合がある",
        }

    # **ソケットが無くても項目を出す。**「比較できなかった」と
    # 「比較して一致した（どちらも 0 個）」を往復テストが区別できるようにする
    socket_entries = (asset.get("staticMesh") or {}).get("sockets") or []
    expected_sockets = [s.get("name") for s in socket_entries]
    found = measured.get("socketNamesFound") or []
    comparison["sockets"] = {
        "expected": expected_sockets,
        "found": found,
        "missing": measured.get("socketNamesMissing") or [],
        "matches": sorted(found) == sorted(expected_sockets),
    }
    if not expected_sockets:
        comparison["sockets"]["note"] = "DCC 側にソケットはありません"

    # **重複の検出。**`find_socket()` は先頭1個しか返さないので、
    # 全列挙と突き合わせないと「インポータ製と2個になった」事故を見逃す
    total = measured.get("socketCountTotal")
    if total is not None:
        comparison["sockets"]["actualTotal"] = total
        comparison["sockets"]["actualAll"] = measured.get("socketNamesAll") or []
        if total != len(expected_sockets):
            comparison["sockets"]["matches"] = False
            comparison["sockets"]["note"] = (
                "UE 側のソケット総数 {0} が宣言数 {1} と違います"
                "（同名の重複を疑ってください）".format(total,
                                                        len(expected_sockets)))

    # ソケットの**位置と回転**（座標系変換の検証 / T-9）
    transforms = measured.get("socketTransforms") or {}
    if socket_entries and transforms:
        details = {}
        all_match = True
        for entry in socket_entries:
            name = entry.get("name")
            actual = transforms.get(name)
            if actual is None:
                all_match = False
                details[name] = {"note": "UE 側で見つかりませんでした"}
                continue
            detail = {}
            for key, tolerance in (("location", 0.01), ("rotation", 0.01),
                                   ("scale", 0.0001)):
                expected_value = [round(float(v), 4)
                                  for v in (entry.get(key) or [])]
                actual_value = actual.get(key)
                if not expected_value or actual_value is None:
                    continue
                delta = [round(a - e, 4)
                         for a, e in zip(actual_value, expected_value)]
                matched = all(abs(d) <= tolerance for d in delta)
                all_match = all_match and matched
                detail[key] = {"expected": expected_value,
                               "actual": actual_value,
                               "delta": delta,
                               "matches": matched}
            details[name] = detail
        comparison["socketTransforms"] = {
            "perSocket": details,
            "matches": all_match,
            "note": "マニフェストの rotation は UE 基底の XYZ オイラー角（度）。"
                    "実測は Rotator の [roll, pitch, yaw]",
        }

    return comparison
