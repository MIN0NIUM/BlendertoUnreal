"""FBX 書き出し（docs/02_仕様書.md [A4]）。

Blender のバージョン差でオペレータの引数が増減するため、**プリセットに書かれた
引数を実際のオペレータのプロパティと照合してフィルタする**。未知の引数は
黙って捨てずに警告として返す（推測で動かさない）。
"""

import os

import bpy

FBX_OPERATOR_PATH = "export_scene.fbx"


#: 実オペレータのプロパティではないメタ情報。フィルタ対象から除く
_META_PROPERTIES = (
    "rna_type", "bl_idname", "bl_label", "bl_description", "bl_options",
    "bl_translation_context", "bl_undo_group", "bl_cursor_pending",
)


def _operator_rna():
    """export_scene.fbx の RNA 型を返す。見つからなければ None。

    注意: `bpy.types.EXPORT_SCENE_OT_fbx.bl_rna.properties` では
    bl_* のメタ情報しか取れない（Blender 5.2 実測で 14 個）。
    実際の引数を得るには **オペレータ側の `get_rna_type()`** を使う（同 42 個）。
    """
    try:
        return bpy.ops.export_scene.fbx.get_rna_type()
    except Exception:
        return None


def operator_available():
    rna = _operator_rna()
    if rna is None:
        return False
    return len(supported_properties()) > 0


def supported_properties():
    """オペレータが受け付けるプロパティ名 -> RNA プロパティ の辞書。"""
    rna = _operator_rna()
    if rna is None:
        return {}
    result = {}
    for prop in rna.properties:
        if prop.identifier in _META_PROPERTIES:
            continue
        result[prop.identifier] = prop
    return result


def enum_values(prop):
    try:
        return [item.identifier for item in prop.enum_items]
    except Exception:
        return []


def filter_kwargs(raw):
    """プリセットの fbx 設定を、実際に渡せる形に整える。

    戻り値: (kwargs, warnings)
    """
    properties = supported_properties()
    kwargs = {}
    warnings = []

    if not properties:
        return {}, ["FBX エクスポータ（export_scene.fbx）が見つかりません。"
                    "Blender の FBX アドオンが有効か確認してください"]

    for key, value in raw.items():
        prop = properties.get(key)
        if prop is None:
            warnings.append(
                "FBX 設定 '{0}' はこの Blender（{1}）のエクスポータに存在しないため無視しました".format(
                    key, bpy.app.version_string)
            )
            continue

        if prop.type == "ENUM":
            allowed = enum_values(prop)
            if getattr(prop, "is_enum_flag", False):
                values = value if isinstance(value, (list, tuple, set)) else [value]
                bad = [v for v in values if v not in allowed]
                if bad:
                    warnings.append(
                        "FBX 設定 '{0}' に不正な値 {1} が含まれます（有効値: {2}）。無視しました".format(
                            key, bad, allowed)
                    )
                    continue
                kwargs[key] = set(values)
            else:
                if value not in allowed:
                    warnings.append(
                        "FBX 設定 '{0}' の値 '{1}' は無効です（有効値: {2}）。無視しました".format(
                            key, value, allowed)
                    )
                    continue
                kwargs[key] = value
        else:
            kwargs[key] = value

    return kwargs, warnings


def select_only(objects):
    """指定オブジェクトだけを選択状態にする（background でも動く）。"""
    view_layer = bpy.context.view_layer
    for obj in view_layer.objects:
        try:
            obj.select_set(False)
        except RuntimeError:
            pass
    active = None
    for obj in objects:
        try:
            obj.select_set(True)
            if active is None:
                active = obj
        except RuntimeError:
            pass
    view_layer.objects.active = active
    return active is not None


def export_fbx(filepath, objects, fbx_settings, object_types=None):
    """FBX を書き出す。

    戻り値: (成功したか, 実際に使った kwargs, 警告リスト)
    """
    directory = os.path.dirname(filepath)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)

    raw = dict(fbx_settings or {})
    if object_types is not None:
        raw["object_types"] = list(object_types)

    kwargs, warnings = filter_kwargs(raw)
    if not kwargs:
        return False, {}, warnings

    if not select_only(objects):
        return False, kwargs, warnings + [
            "書き出し対象のオブジェクトを選択できませんでした"
        ]

    kwargs["filepath"] = filepath
    properties = supported_properties()
    if "use_selection" in properties:
        kwargs["use_selection"] = True
    if "use_visible" in properties:
        kwargs["use_visible"] = False
    if "use_active_collection" in properties:
        kwargs["use_active_collection"] = False

    try:
        result = bpy.ops.export_scene.fbx(**kwargs)
    except Exception as error:  # noqa: BLE001 - 失敗理由をそのまま返したい
        return False, kwargs, warnings + [
            "FBX の書き出しに失敗しました: {0}: {1}".format(type(error).__name__, error)
        ]

    if "FINISHED" not in result:
        return False, kwargs, warnings + [
            "FBX の書き出しが完了しませんでした（オペレータの戻り値: {0}）".format(result)
        ]

    if not os.path.isfile(filepath):
        return False, kwargs, warnings + [
            "FBX ファイルが生成されませんでした: {0}".format(filepath)
        ]

    return True, kwargs, warnings


def recorded_settings(kwargs):
    """マニフェストの coordinateSystem.fbxExportSettings に入れる形に整える。

    set は JSON にできないのでソート済みリストにする。filepath は除く。
    """
    record = {}
    for key, value in kwargs.items():
        if key == "filepath":
            continue
        if isinstance(value, set):
            record[key] = sorted(value)
        else:
            record[key] = value
    return record
