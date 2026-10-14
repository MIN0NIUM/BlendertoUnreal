"""再インポート時の設定保持（T-7 / `reimportPolicy`）。

**何のためのモジュールか**: 2回目以降の取り込みで、UE 側で手修正した設定
（Nanite / コリジョン / ライトマップ / マテリアル割当 / ソケット / Build Settings）
が消えないようにする。量産では「再インポートしたら手修正が全部飛んだ」が致命的。

**構成**

1. 純粋ロジック（`unreal` 非依存 / 単体テスト対象）
   - :func:`resolve_policy` — マニフェストとアセットから方針を決める
   - :func:`plan_categories` — 「上書きする項目」と「保持する項目」に分ける
2. UE 依存（`postprocess` のヘルパを使い回す）
   - :func:`capture` — インポート**前**のスナップショットを取る
   - :func:`restore` — インポート**後**に保持対象だけ戻す

**`unreal` を import しないのはモジュールの最上位だけ。**UE 依存部は関数の中で
`from . import postprocess`（= `unreal` を import するモジュール）を読む。
こうしておくと `import onepochi_ue.reimport` が UE 無しの環境でも成功し、
純粋ロジックを単体テストできる。

**設計上の注意（既知の落とし穴）**

- *P-15*: プロパティ名はスタブが無い環境では導出値。**候補名を順に試し、
  取れなかった項目はスナップショットに**キーを作らない**。
  「設定したつもりで効いていない」を作らないため、取れなかったことは sink に残す
- *P-18*: `void` を返す UE API は戻り値で成否を判断しない。
  復元の成否は**別の観測**（読み直し / 列挙）で確かめる。観測できないものは
  「適用を試みた」と正直に記録する
- スナップショットは**レポート JSON にそのまま載る**ので、
  `unreal.Vector` などをそのまま入れない（必ず素の数値 / 文字列 / 真偽値にする）

**対応範囲**: 現時点では **StaticMesh のみ**。SkeletalMesh は未対応
（`importer.import_skeletal_mesh_asset` が warning を出す）。
"""

#: 保持 / 上書きの単位。**スキーマの `reimportPolicy.override` の enum と完全一致させる**
#: （`schema/onepochi_manifest.schema.json`）。一致は単体テストで機械的に検証する
CATEGORIES = ("nanite", "lod", "collision", "lightmap", "materials",
              "sockets", "buildSettings")

#: `reimportPolicy.mode` の取りうる値（スキーマと一致）
MODES = ("preserveSettings", "overwriteAll")

#: 既定の方針。`tools/presets/project_default.json` の `reimportPolicy` と一致させる
DEFAULT_MODE = "preserveSettings"

#: Nanite のフォールバック誤差のプロパティ名候補（`postprocess.apply_nanite` と対称）
NANITE_FALLBACK_PROPERTIES = ("fallback_relative_error", "fallback_percent_triangles")

#: Build Settings のキー対応（`postprocess.apply_build_settings` の mapping と対称）
BUILD_SETTING_PROPERTIES = (
    ("recomputeNormals", ("recompute_normals",)),
    ("recomputeTangents", ("recompute_tangents",)),
    ("removeDegenerates", ("remove_degenerates",)),
    ("generateDistanceField", ("generate_distance_field_as_if_two_sided",
                               "generate_distance_field")),
)

#: LOD の画面占有率を読み書きするメソッド名候補（**要検証**）
LOD_SCREEN_SIZE_GETTERS = ("get_lod_screen_sizes",)
LOD_SCREEN_SIZE_SETTERS = ("set_lod_screen_sizes",)


# --- 純粋ロジック（`unreal` 非依存）-------------------------------------------


def default_policy():
    """既定の方針を新しい辞書で返す。"""
    return {"mode": DEFAULT_MODE, "override": [], "source": "default",
            "warnings": []}


def resolve_policy(manifest, asset, sink=None):
    """`reimportPolicy` を決める。

    優先順位: **アセット側（`asset["reimportPolicy"]`）がマニフェスト側
    （`manifest["reimportPolicy"]`）を上書きする**。どちらも無ければ
    `{"mode": "preserveSettings", "override": []}`（プリセットの既定値）。

    ※ スキーマ（v0.3.0）では `reimportPolicy` は **assets[] の下にだけ**
    定義されている（トップレベルは `additionalProperties: false`）。
    マニフェスト側を読むのは将来の共通指定・手書きマニフェストへの保険。

    **未知の値で例外を投げない。**不明な `mode` は既定（= 保持する側）に倒し、
    不明な `override` 項目は捨てて、理由を warning 文字列として残す
    （戻り値の `warnings` と、渡されていれば `sink` の両方に積む）。

    戻り値: `{"mode": str, "override": [str, ...], "source": str,
    "warnings": [str, ...]}`
    """
    warnings = []
    raw, source = _pick_raw_policy(manifest, asset, warnings)

    mode = DEFAULT_MODE
    override = []

    if raw is not None:
        declared = raw.get("mode")
        if declared is None:
            warnings.append(
                "reimportPolicy に mode がありません（{0} 側）。"
                "既定の '{1}' として扱います".format(source, DEFAULT_MODE))
        elif declared not in MODES:
            warnings.append(
                "reimportPolicy.mode が不明な値です: {0!r}（{1} 側）。"
                "既定の '{2}' として扱います（設定を保持する側に倒します）".format(
                    declared, source, DEFAULT_MODE))
        else:
            mode = declared

        declared_override = raw.get("override")
        if declared_override is None:
            pass
        elif not isinstance(declared_override, (list, tuple)):
            warnings.append(
                "reimportPolicy.override が配列ではありません: {0!r}。"
                "空として扱います".format(declared_override))
        else:
            for item in declared_override:
                if item in CATEGORIES:
                    if item not in override:
                        override.append(item)
                else:
                    warnings.append(
                        "reimportPolicy.override に不明な項目があります: {0!r}。"
                        "無視します（有効な項目: {1}）".format(
                            item, ", ".join(CATEGORIES)))

    if sink is not None:
        sink.extend(warnings)
    return {"mode": mode, "override": override, "source": source,
            "warnings": warnings}


def _pick_raw_policy(manifest, asset, warnings):
    """アセット側 → マニフェスト側の順に `reimportPolicy` を探す。"""
    for container, label in ((asset, "asset"), (manifest, "manifest")):
        if not isinstance(container, dict):
            continue
        raw = container.get("reimportPolicy")
        if raw is None:
            continue
        if not isinstance(raw, dict):
            warnings.append(
                "reimportPolicy がオブジェクトではありません（{0} 側）: {1!r}。"
                "無視します".format(label, raw))
            continue
        return raw, label
    return None, "default"


def plan_categories(policy, existed):
    """方針を「上書きする項目」と「保持する項目」に分ける。

    - `existed = False`（新規作成）→ **全カテゴリ `apply`**（保持するものが無い）
    - `mode = "overwriteAll"` → 全カテゴリ `apply`
    - `mode = "preserveSettings"` → `override` に挙がったものだけ `apply`、
      残りは `preserve`

    戻り値: `{"apply": [...], "preserve": [...]}`（並びは :data:`CATEGORIES` の順）
    """
    policy = policy or {}
    mode = policy.get("mode") or DEFAULT_MODE
    override = [c for c in (policy.get("override") or []) if c in CATEGORIES]

    if not existed or mode == "overwriteAll":
        return {"apply": list(CATEGORIES), "preserve": []}

    # 不明な mode は resolve_policy が既定に倒しているが、
    # 直接呼ばれた場合も「保持する側」に倒す（安全側）
    return {
        "apply": [c for c in CATEGORIES if c in override],
        "preserve": [c for c in CATEGORIES if c not in override],
    }


def normalize_asset_path(path):
    """オブジェクト参照からパッケージパスを取り出す（`unreal` 非依存）。

    受け付ける形:
        ``/Game/X/M_A``
        ``/Game/X/M_A.M_A``
        ``Material'/Game/X/M_A.M_A'``
    """
    if path is None:
        return None
    text = str(path)
    if "'" in text:
        start = text.find("'")
        end = text.rfind("'")
        if end > start:
            text = text[start + 1:end]
    if "." in text:
        head, _, _tail = text.rpartition(".")
        return head
    return text


# --- UE 依存: スナップショット -------------------------------------------------


def capture(mesh, sink):
    """インポート**前**の設定スナップショットを返す。

    **取れなかった項目はキーを作らない**（P-15）。代わりに理由を `sink` に残す。
    「読めなかった」と「読めて 0 だった」を混同させないため。

    戻り値はカテゴリ名をキーにした JSON 安全な辞書。
    読めたカテゴリだけが存在する（空なら `{}`）。
    """
    from . import postprocess as pp

    snapshot = {}
    if mesh is None:
        sink.append("再インポート前のスナップショットを取れません（メッシュが None）")
        return snapshot

    for category, capturer in (
        ("nanite", _capture_nanite),
        ("lightmap", _capture_lightmap),
        ("collision", _capture_collision),
        ("buildSettings", _capture_build_settings),
        ("sockets", _capture_sockets),
        ("materials", _capture_materials),
        ("lod", _capture_lod),
    ):
        try:
            data = capturer(mesh, sink, pp)
        except Exception as error:  # noqa: BLE001 - 1項目の失敗で全体を止めない
            sink.append("スナップショット({0}) で例外: {1}: {2}".format(
                category, type(error).__name__, error))
            continue
        if data:
            snapshot[category] = data
        else:
            sink.append("スナップショット({0}) を取得できませんでした。"
                        "このカテゴリは復元できません".format(category))
    return snapshot


def _number(value):
    try:
        return round(float(value), 6)
    except Exception:  # noqa: BLE001
        return None


def _capture_nanite(mesh, sink, pp):
    settings = pp.get_property(sink, mesh, "nanite_settings")
    if settings is None:
        return None
    data = {}
    enabled = pp.get_property(sink, settings, "enabled")
    if enabled is not None:
        data["enabled"] = bool(enabled)
    name, value = pp._get_first_available(
        settings, NANITE_FALLBACK_PROPERTIES, sink, "nanite のフォールバック誤差")
    if name is not None:
        number = _number(value)
        if number is not None:
            data["fallbackRelativeError"] = number
            # どの名前で読めたかを残す（P-15: 名前は環境で揺れる）
            data["fallbackProperty"] = name
    return data or None


def _capture_lightmap(mesh, sink, pp):
    data = {}
    name, value = pp._get_first_available(
        mesh, pp.LIGHTMAP_INDEX_PROPERTIES, sink, "ライトマップ UV チャンネル")
    if name is not None and value is not None:
        try:
            data["coordinateIndex"] = int(value)
            data["coordinateProperty"] = name
        except Exception as error:  # noqa: BLE001
            sink.append("ライトマップ UV チャンネルを数値にできません: {0}".format(error))
    resolution = pp.get_property(sink, mesh, "light_map_resolution")
    if resolution is not None:
        try:
            data["resolution"] = int(resolution)
        except Exception as error:  # noqa: BLE001
            sink.append("light_map_resolution を数値にできません: {0}".format(error))
    return data or None


def _enum_name(value):
    """UE の enum 値から名前だけ取り出す（`CTF_USE_DEFAULT` など）。"""
    name = getattr(value, "name", None)
    if isinstance(name, str) and name:
        return name
    text = str(value)
    return text.rsplit(".", 1)[-1] if "." in text else text


def _capture_collision(mesh, sink, pp):
    """コリジョンのスナップショット。

    **形状データそのものは復元できない。**`KAggregateGeom` の凸包を Python から
    作り直す手段が確立していないため、ここでは**形状の個数**と
    `collision_trace_flag` だけを記録する。復元時は個数を突き合わせて、
    減っていたら warning を出す（= 嘘をつかない）。
    """
    data = {}
    counts = pp.collision_element_counts(mesh, sink)
    if counts:
        data["elementCounts"] = dict(counts)
    body_setup = pp.get_property(sink, mesh, "body_setup")
    if body_setup is not None:
        flag = pp.get_property(sink, body_setup, "collision_trace_flag")
        if flag is not None:
            data["collisionTraceFlag"] = _enum_name(flag)
    if data:
        # 「個数は記録したが形状は戻せない」ことを明示する
        data["shapesRestorable"] = False
    return data or None


def build_settings_provider(sink):
    """LOD Build Settings を読み書きできるオブジェクトを返す（無ければ None）。

    `postprocess.build_settings_provider` に委譲する（実装の一本化）。
    """
    from . import postprocess as pp
    return pp.build_settings_provider(sink)


def _capture_build_settings(mesh, sink, pp, lod_index=0):
    provider = pp.build_settings_provider(sink)
    if provider is None:
        sink.append("Build Settings のスナップショットを取れません"
                    "（StaticMeshEditorSubsystem / EditorStaticMeshLibrary が"
                    "どちらも使えません）")
        return None
    build = pp.attempt(sink, "get_lod_build_settings",
                       provider.get_lod_build_settings, mesh, lod_index)
    if build is None:
        return None

    data = {}
    for key, candidates in BUILD_SETTING_PROPERTIES:
        name, value = pp._get_first_available(build, candidates, sink,
                                              "buildSettings.{0}".format(key))
        if name is not None and value is not None:
            data[key] = bool(value)
    scale = pp.get_property(sink, build, "build_scale3d")
    vector = pp._vector_list(scale)
    if vector is not None:
        data["buildScale"] = vector
    return data or None


def _capture_sockets(mesh, sink, pp):
    """全ソケットを列挙して記録する。

    `StaticMesh.sockets` は protected で読めないが、**`get_sockets_by_tag("")`
    で全ソケットを列挙できる**（P-17 の実測）。`find_socket()` は先頭 1個しか
    返さないので、ここでは必ず全列挙を使う。
    """
    if not hasattr(mesh, "get_sockets_by_tag"):
        sink.append("StaticMesh.get_sockets_by_tag が無いためソケットを列挙できません")
        return None
    sockets = pp.attempt(sink, 'get_sockets_by_tag("")',
                         mesh.get_sockets_by_tag, "")
    if sockets is None:
        return None
    entries = []
    for socket in sockets:
        name = pp.get_property(sink, socket, "socket_name")
        if name is None:
            continue
        entries.append({
            "name": str(name),
            "location": pp._vector_list(
                pp.get_property(sink, socket, "relative_location")),
            "rotation": pp._rotator_list(
                pp.get_property(sink, socket, "relative_rotation")),
            "scale": pp._vector_list(
                pp.get_property(sink, socket, "relative_scale")),
        })
    # **0個でも「読めた」として記録する**（空リストは偽になるので辞書で包む）
    return {"entries": entries, "count": len(entries)}


def _capture_materials(mesh, sink, pp):
    materials = pp.get_property(sink, mesh, "static_materials", None)
    if materials is None:
        return None
    entries = []
    for index, slot in enumerate(materials):
        entry = {"index": index}
        name = pp.get_property(sink, slot, "material_slot_name")
        if name is not None:
            entry["slotName"] = str(name)
        interface = pp.get_property(sink, slot, "material_interface")
        if interface is not None:
            path = pp.attempt(sink, "material_interface.get_path_name",
                              interface.get_path_name) \
                if hasattr(interface, "get_path_name") else None
            entry["materialPath"] = normalize_asset_path(path)
        else:
            entry["materialPath"] = None
        entries.append(entry)
    return {"slots": entries, "count": len(entries)}


def _capture_lod(mesh, sink, pp):
    """LOD のスナップショット。

    **LOD の段数そのものは復元できない**（ソースジオメトリが必要）。
    記録するのは段数と、取れれば LOD ごとの画面占有率（screen size）。
    画面占有率の API 名は **要検証**（P-15）。
    """
    data = {}
    if hasattr(mesh, "get_num_lods"):
        count = pp.attempt(sink, "get_num_lods", mesh.get_num_lods)
        if count is not None:
            data["lodCount"] = int(count)

    provider = pp.build_settings_provider(sink)
    if provider is not None:
        for getter in LOD_SCREEN_SIZE_GETTERS:
            function = getattr(provider, getter, None)
            if function is None:
                continue
            sizes = pp.attempt(sink, getter, function, mesh)
            if sizes is None:
                continue
            values = []
            for value in sizes:
                number = _number(value)
                if number is not None:
                    values.append(number)
            if values:
                data["screenSizes"] = values
                data["screenSizeGetter"] = getter
            break
        else:
            sink.append("LOD の画面占有率を読めませんでした"
                        "（試した候補: {0} / 要検証）".format(
                            ", ".join(LOD_SCREEN_SIZE_GETTERS)))
    return data or None


# --- UE 依存: 復元 -------------------------------------------------------------


def restore(mesh, snapshot, categories, sink):
    """スナップショットから `categories` のカテゴリだけ復元する。

    戻り値: `{"restored": [...], "skipped": [...], "failed": [...],
    "details": {...}}`

    - `restored`: 復元を**観測できた**もの
    - `skipped`: スナップショットが無い / この UE では復元手段が無いもの
    - `failed`: 復元を試みて失敗したもの

    **`void` を返す API の戻り値で成否を判断しない**（P-18）。
    各カテゴリは適用後に読み直して確認する。読み直せない項目は
    「適用を試みた」と `details` に正直に書く。
    """
    from . import postprocess as pp

    result = {"restored": [], "skipped": [], "failed": [], "details": {}}
    requested = [c for c in CATEGORIES if c in (categories or [])]
    unknown = [c for c in (categories or []) if c not in CATEGORIES]
    if unknown:
        sink.append("復元対象に不明なカテゴリがあります: {0}。無視します".format(
            ", ".join(str(u) for u in unknown)))
        result["unknownCategories"] = [str(u) for u in unknown]

    if not requested:
        return result

    if mesh is None:
        sink.append("復元できません（メッシュが None）: {0}".format(
            ", ".join(requested)))
        result["skipped"].extend(requested)
        return result

    snapshot = snapshot or {}
    handlers = {
        "nanite": _restore_nanite,
        "lightmap": _restore_lightmap,
        "collision": _restore_collision,
        "buildSettings": _restore_build_settings,
        "sockets": _restore_sockets,
        "materials": _restore_materials,
        "lod": _restore_lod,
    }

    for category in requested:
        data = snapshot.get(category)
        if not data:
            sink.append("復元をスキップ（{0}）: インポート前のスナップショットが"
                        "ありません".format(category))
            result["skipped"].append(category)
            continue
        handler = handlers.get(category)
        if handler is None:
            sink.append("復元をスキップ（{0}）: 復元処理が未実装です".format(category))
            result["skipped"].append(category)
            continue
        detail = {}
        try:
            status = handler(mesh, data, sink, pp, detail)
        except Exception as error:  # noqa: BLE001 - 1項目の失敗で全体を止めない
            sink.append("復元({0}) で例外: {1}: {2}".format(
                category, type(error).__name__, error))
            result["failed"].append(category)
            result["details"][category] = detail
            continue
        if status not in ("restored", "skipped", "failed"):
            status = "failed"
        result[status].append(category)
        if detail:
            result["details"][category] = detail
    return result


def _restore_nanite(mesh, data, sink, pp, detail):
    settings = {}
    if "enabled" in data:
        settings["enabled"] = bool(data["enabled"])
    if "fallbackRelativeError" in data:
        settings["fallbackRelativeError"] = data["fallbackRelativeError"]
    if not settings:
        return "skipped"
    detail["requested"] = dict(settings)
    pp.apply_nanite(mesh, settings, sink)

    # **読み直して確認する**（apply_nanite は成否を返さない）
    current = pp.get_property(sink, mesh, "nanite_settings")
    if current is None:
        detail["verified"] = False
        return "failed"
    observed = {}
    enabled = pp.get_property(sink, current, "enabled")
    if enabled is not None:
        observed["enabled"] = bool(enabled)
    name = data.get("fallbackProperty")
    candidates = (name,) if name else NANITE_FALLBACK_PROPERTIES
    _found, value = pp._get_first_available(current, candidates, sink,
                                            "nanite のフォールバック誤差")
    number = _number(value)
    if number is not None:
        observed["fallbackRelativeError"] = number
    detail["observed"] = observed
    detail["verified"] = True

    if "enabled" in settings and observed.get("enabled") != settings["enabled"]:
        sink.append("nanite.enabled を復元できませんでした"
                    "（期待 {0} / 実測 {1}）".format(settings["enabled"],
                                                      observed.get("enabled")))
        return "failed"
    return "restored"


def _restore_lightmap(mesh, data, sink, pp, detail):
    settings = {}
    if "coordinateIndex" in data:
        settings["coordinateIndex"] = data["coordinateIndex"]
    if "resolution" in data:
        settings["resolution"] = data["resolution"]
    if not settings:
        return "skipped"
    detail["requested"] = dict(settings)
    pp.apply_lightmap(mesh, settings, sink)

    observed = {}
    _name, value = pp._get_first_available(mesh, pp.LIGHTMAP_INDEX_PROPERTIES,
                                           sink, "ライトマップ UV チャンネル")
    if value is not None:
        observed["coordinateIndex"] = int(value)
    resolution = pp.get_property(sink, mesh, "light_map_resolution")
    if resolution is not None:
        observed["resolution"] = int(resolution)
    detail["observed"] = observed

    for key in settings:
        if key in observed and observed[key] != settings[key]:
            sink.append("lightmap.{0} を復元できませんでした"
                        "（期待 {1} / 実測 {2}）".format(key, settings[key],
                                                          observed[key]))
            return "failed"
    return "restored"


def _restore_collision(mesh, data, sink, pp, detail):
    """コリジョンの復元。**形状データは復元できない。**

    できるのは `collision_trace_flag` を戻すことと、
    **形状の個数が減っていないかを突き合わせて正直に報告すること**だけ。
    """
    import unreal

    before = data.get("elementCounts") or {}
    after = pp.collision_element_counts(mesh, sink)
    detail["elementCountsBefore"] = dict(before)
    detail["elementCountsAfter"] = dict(after) if after else None
    detail["shapesRestored"] = False

    if before.get("total") is not None and after and after.get("total") is not None:
        if int(after["total"]) < int(before["total"]):
            sink.append(
                "再インポートで簡易コリジョンが {0} 個から {1} 個に減りました。"
                "**形状データの復元は実装できていません**"
                "（KAggregateGeom を Python から作り直す手段が未確立）。"
                "UE 側で付け直してください".format(before["total"],
                                                   after["total"]))
        elif int(after["total"]) > int(before["total"]):
            if int(before["total"]) == 0:
                # **スナップショットが「0 個」なら復元できる。**
                # インポータは `UCX_` が無い FBX にも凸包を1個作ってくる
                # （docs/12 C-7 / I-8）。初回は `collision.simple = "none"` の
                # ポストプロセスが消しているので、再インポートでも消すのが
                # 「元の状態に戻す」ことになる
                cleared = pp.clear_simple_collision(
                    mesh, sink,
                    reason="再インポート前のスナップショットが 0 個だったため")
                recount = pp.collision_element_counts(mesh, sink)
                detail["clearedByRestore"] = cleared
                detail["elementCountsAfter"] = dict(recount) if recount else None
                detail["shapesRestored"] = True
                sink.append(
                    "再インポートでインポータが簡易コリジョンを {0} 個"
                    "自動生成したため、スナップショット（0 個）に合わせて"
                    "削除しました".format(after["total"]))
            else:
                sink.append(
                    "再インポートで簡易コリジョンが {0} 個から {1} 個に増えました。"
                    "インポータが自動生成した可能性があります。"
                    "**増えた分だけを選んで消す手段が無い**ため、そのままにします"
                    "（UE 側で確認してください）".format(
                        before["total"], after["total"]))

    flag_name = data.get("collisionTraceFlag")
    if not flag_name:
        return "skipped"
    enum = getattr(unreal, "CollisionTraceFlag", None)
    flag = getattr(enum, flag_name, None) if enum is not None else None
    if flag is None:
        sink.append("CollisionTraceFlag.{0} を解決できないため"
                    "複雑コリジョンの設定を復元できません".format(flag_name))
        return "skipped"
    body_setup = pp.get_property(sink, mesh, "body_setup")
    if body_setup is None:
        return "failed"
    if not pp.set_property(sink, body_setup, "collision_trace_flag", flag):
        return "failed"
    observed = pp.get_property(sink, body_setup, "collision_trace_flag")
    detail["collisionTraceFlag"] = flag_name
    detail["collisionTraceFlagObserved"] = \
        _enum_name(observed) if observed is not None else None
    if detail["collisionTraceFlagObserved"] not in (None, flag_name):
        sink.append("collision_trace_flag を復元できませんでした"
                    "（期待 {0} / 実測 {1}）".format(
                        flag_name, detail["collisionTraceFlagObserved"]))
        return "failed"
    return "restored"


def _restore_build_settings(mesh, data, sink, pp, detail, lod_index=0):
    settings = {key: data[key] for key, _candidates in BUILD_SETTING_PROPERTIES
                if key in data}
    if "buildScale" in data and data["buildScale"]:
        settings["buildScale"] = data["buildScale"]
    if not settings:
        return "skipped"
    detail["requested"] = dict(settings)

    provider = pp.build_settings_provider(sink)
    if provider is None:
        sink.append("Build Settings を復元できません"
                    "（StaticMeshEditorSubsystem / EditorStaticMeshLibrary が"
                    "どちらも使えません。commandlet モードでは不可 / P-5）")
        return "skipped"

    pp.apply_build_settings(mesh, settings, sink, lod_index)

    observed = _capture_build_settings(mesh, sink, pp, lod_index) or {}
    detail["observed"] = observed
    for key in settings:
        if key == "buildScale":
            continue
        if key in observed and bool(observed[key]) != bool(settings[key]):
            sink.append("buildSettings.{0} を復元できませんでした"
                        "（期待 {1} / 実測 {2}）".format(key, settings[key],
                                                          observed[key]))
            return "failed"
    return "restored"


def _restore_sockets(mesh, data, sink, pp, detail):
    """ソケットを作り直す。

    インポータは `SOCKET_` 命名からソケットを**自前で作る**（P-17）。しかも
    scale に 100 倍が混入する。そのため**同名を消してスナップショットの値で
    作り直す**。UE 側で手で足した別名のソケットもスナップショットに入っている
    （`get_sockets_by_tag("")` で全列挙しているため）ので一緒に戻る。

    **`add_socket()` は void**（P-18）。成否は `get_sockets_by_tag("")` の
    再列挙で確認する。
    """
    entries = [e for e in (data.get("entries") or []) if e.get("name")]
    detail["requestedNames"] = [e["name"] for e in entries]
    if not entries:
        # 0個だったことは記録するが、消す処理はしない（インポータ製を
        # 消したいかどうかは apply 側の責務）
        detail["note"] = "インポート前にソケットは 0個でした（復元する対象なし）"
        return "skipped"

    pp.apply_sockets(mesh, entries, sink)

    observed = _capture_sockets(mesh, sink, pp) or {}
    names = [e.get("name") for e in (observed.get("entries") or [])]
    detail["observedNames"] = names
    missing = [e["name"] for e in entries if e["name"] not in names]
    if missing:
        sink.append("ソケットを復元できませんでした: {0}".format(", ".join(missing)))
        detail["missing"] = missing
        return "failed"
    duplicated = [n for n in set(names) if names.count(n) > 1]
    if duplicated:
        # P-17 の重複事故。復元自体は成立しているが必ず残す
        sink.append("復元後に同名のソケットが重複しています: {0}".format(
            ", ".join(sorted(duplicated))))
        detail["duplicated"] = sorted(duplicated)
    return "restored"


def _restore_materials(mesh, data, sink, pp, detail):
    """マテリアルスロット名と割当済みマテリアルを戻す。

    スロット数が再インポートで変わっている（DCC 側でスロットを増減した）
    場合は、**index が存在する分だけ**戻して、残りを warning にする。
    """
    import unreal

    slots = data.get("slots") or []
    if not slots:
        return "skipped"
    materials = pp.get_property(sink, mesh, "static_materials", None)
    if not materials:
        sink.append("マテリアルスロットを復元できません"
                    "（static_materials を読めませんでした）")
        return "failed"

    detail["slotCountBefore"] = data.get("count")
    detail["slotCountAfter"] = len(materials)
    if data.get("count") is not None and int(data["count"]) != len(materials):
        sink.append(
            "マテリアルスロット数が再インポートで {0} から {1} に変わりました。"
            "index が一致する分だけ復元します".format(data["count"], len(materials)))

    restored = []
    failed = []
    changed = False
    for entry in slots:
        index = entry.get("index")
        if index is None or index >= len(materials):
            failed.append(entry.get("slotName"))
            continue
        properties = []
        ok = True
        if entry.get("slotName"):
            properties.append(("material_slot_name", entry["slotName"]))
        path = entry.get("materialPath")
        if path:
            asset = pp.attempt(sink, "load_asset({0})".format(path),
                               unreal.EditorAssetLibrary.load_asset, path)
            if asset is None:
                sink.append("マテリアル '{0}' をロードできないため"
                            "スロット {1} の割当を復元できません".format(path, index))
                ok = False
            else:
                properties.append(("material_interface", asset))
        # **構造体配列の要素はコピー**なので代入し直す（docs/99 P-23）
        if ok and properties:
            ok = pp.update_struct_in_array(
                sink, materials, index, properties,
                label="static_materials") is not None
        if ok:
            restored.append(entry.get("slotName"))
            changed = True
        else:
            failed.append(entry.get("slotName"))

    if changed:
        # 構造体の配列は要素を書き換えたあと**配列ごと書き戻す**
        # （postprocess.apply_material_slot_names と同じ作法）
        pp.set_property(sink, mesh, "static_materials", materials)

    # 読み直して確認する
    observed = _capture_materials(mesh, sink, pp) or {}
    detail["observed"] = observed.get("slots")
    detail["restoredSlots"] = restored
    if failed:
        detail["failedSlots"] = failed
        return "failed" if not restored else "restored"
    return "restored" if restored else "skipped"


def _restore_lod(mesh, data, sink, pp, detail):
    """LOD の復元。

    **段数は復元できない**（ソースジオメトリが必要）。段数が減っていたら
    warning を出す。画面占有率は setter が使える場合だけ戻す（**要検証**）。
    """
    before = data.get("lodCount")
    after = None
    if hasattr(mesh, "get_num_lods"):
        after = pp.attempt(sink, "get_num_lods", mesh.get_num_lods)
    detail["lodCountBefore"] = before
    detail["lodCountAfter"] = int(after) if after is not None else None
    if before is not None and after is not None and int(after) < int(before):
        sink.append(
            "再インポートで LOD の段数が {0} から {1} に減りました。"
            "**LOD の復元は実装できていません**"
            "（ソースジオメトリが必要）。マニフェストの staticMesh.lod で"
            "宣言してください".format(before, after))

    sizes = data.get("screenSizes")
    if not sizes:
        return "skipped"
    if after is not None and int(after) < len(sizes):
        # 段数が減っているのに長い配列を渡すと失敗しうるので切り詰める
        sink.append("LOD の段数が減ったため、画面占有率の復元を {0} 段分に"
                    "切り詰めます".format(int(after)))
        sizes = list(sizes)[:int(after)]
        if not sizes:
            return "skipped"
    provider = pp.build_settings_provider(sink)
    if provider is None:
        return "skipped"
    for setter in LOD_SCREEN_SIZE_SETTERS:
        function = getattr(provider, setter, None)
        if function is None:
            continue
        if pp.attempt(sink, setter, function, mesh, sizes) is None:
            # void を返す可能性があるため、戻り値では判断せず読み直す
            pass
        observed = (_capture_lod(mesh, sink, pp) or {}).get("screenSizes")
        detail["screenSizesRequested"] = sizes
        detail["screenSizesObserved"] = observed
        if observed is None:
            sink.append("LOD の画面占有率を復元したか確認できませんでした"
                        "（読み直せません / 要検証）")
            return "restored"
        if [round(v, 4) for v in observed[:len(sizes)]] != \
                [round(v, 4) for v in sizes]:
            sink.append("LOD の画面占有率を復元できませんでした"
                        "（期待 {0} / 実測 {1}）".format(sizes, observed))
            return "failed"
        return "restored"

    sink.append("LOD の画面占有率を復元できません"
                "（試した候補: {0} / 要検証）".format(
                    ", ".join(LOD_SCREEN_SIZE_SETTERS)))
    return "skipped"
