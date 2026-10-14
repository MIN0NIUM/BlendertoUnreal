"""アドオンのプロパティ定義（UI の状態）。"""

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty,
                       IntProperty, StringProperty)
from bpy.types import PropertyGroup

from ..core import presets as presets_mod

_PRESET_CACHE = {"items": None}


def preset_items(self, context):
    """プリセット一覧を列挙する（ファイル名から動的に作る）。"""
    names = presets_mod.list_presets()
    if not names:
        return [("prop", "prop", "既定プリセット")]
    items = []
    for name in names:
        label = name
        description = ""
        try:
            data = presets_mod.load_preset_file(name)
            description = data.get("description", "") or ""
        except Exception:
            pass
        items.append((name, label, description))
    _PRESET_CACHE["items"] = items
    return items


#: 環境のラベル（日本語）。未知の環境名はそのまま表示する
_ENVIRONMENT_LABELS = {
    "dev": ("Dev（検証用）", "検証用の /Game/_Dev 配下に書き出す。既定"),
    "production": ("本番", "本番想定の /Game 配下に直接書き出す。取り扱い注意"),
}


def environment_items(self, context):
    """環境一覧を列挙する（R-2）。

    選択中のプリセットが `destination.environments` を増やしていれば、それも拾う。
    プリセットが読めない場合は既定の dev / production だけを返す。
    """
    items = [("PRESET", "プリセットに従う",
              "プリセットの destination.environment を使う")]
    names = list(presets_mod.DEFAULT_ENVIRONMENTS)
    try:
        preset_name = getattr(self, "preset", None)
        if preset_name:
            names = presets_mod.environment_names(
                presets_mod.resolve_preset(preset_name))
    except Exception:
        pass
    for name in names:
        label, description = _ENVIRONMENT_LABELS.get(name, (name, ""))
        items.append((name, label, description))
    return items


class OnePochiIssue(PropertyGroup):
    """UI に表示する検証結果の1行。"""

    rule_id: StringProperty(name="ルール")
    severity: StringProperty(name="重大度")
    target: StringProperty(name="対象")
    message: StringProperty(name="内容")
    auto_fixed: BoolProperty(name="自動修正済み", default=False)


class OnePochiSettings(PropertyGroup):
    """シーンに保存される設定。"""

    preset: EnumProperty(
        name="プリセット",
        description="適用するプリセット（tools/presets/*.json）",
        items=preset_items,
    )

    environment: EnumProperty(
        name="出力環境",
        description="UE のどこに書き出すか。Dev は /Game/_Dev、本番は /Game 直下",
        items=environment_items,
    )

    asset_type: EnumProperty(
        name="アセット種別",
        items=[
            ("StaticMesh", "Static Mesh", "静的メッシュとして取り込む"),
            ("SkeletalMesh", "Skeletal Mesh", "スケルタルメッシュとして取り込む"),
        ],
        default="StaticMesh",
    )

    target_mode: EnumProperty(
        name="対象の指定",
        items=[
            ("SELECTION", "選択オブジェクト", "選択中のオブジェクトを書き出す"),
            ("COLLECTION", "コレクション", "アセット用コレクションを起点にする（LOD/Collision/Sockets）"),
        ],
        default="SELECTION",
    )

    target_collection: StringProperty(
        name="コレクション",
        description="アセット用コレクション名（LOD0 / Collision / Sockets を子に持つもの）",
    )

    asset_name: StringProperty(
        name="アセット名",
        description="UE 側のアセット名のベース（プレフィックスは自動付与）。先頭は英字",
    )

    asset_folder: StringProperty(
        name="アセットフォルダ",
        description="空なら .blend の置き場を使う。fbx/ サブフォルダに書き出す",
        subtype="DIR_PATH",
    )

    ue_path_override: StringProperty(
        name="UE 出力先",
        description="空ならプリセットの pathTemplate から決める（例: /Game/_Dev/Props/Crate_01）",
    )

    material_mode: EnumProperty(
        name="マテリアル",
        description="UE 側でマテリアルをどう扱うか",
        items=[
            ("PRESET", "プリセットに従う", "プリセットの material.mode を使う"),
            ("none", "何もしない", "マテリアルを生成も割当もしない"),
            ("assignExisting", "既存を割当", "UE 側の同名マテリアルを探して割り当てる"),
            ("createInstances", "MI を生成",
             "マスターマテリアルから Material Instance を生成して割り当てる"),
        ],
        default="PRESET",
    )

    master_material_path: StringProperty(
        name="マスターマテリアル",
        description="MI 生成の元になる UE のマテリアルパス。"
                    "空ならプリセットの値を使う（例: /Game/_Dev/Materials/M_OnePochi_Base）",
    )

    auto_import_to_ue: BoolProperty(
        name="UE へ自動インポート",
        description="書き出し後に UE へ取り込みを依頼する"
                    "（UE の Remote Control API プラグインが有効な場合のみ）",
        default=False,
    )

    # --- 結果表示用 ---
    issues: CollectionProperty(type=OnePochiIssue)
    issue_index: IntProperty(default=0)
    last_summary: StringProperty(default="")
    last_manifest_path: StringProperty(default="")
    last_output_dir: StringProperty(default="")
    last_ok: BoolProperty(default=False)

    # --- UE 側の結果（P2） ---
    last_ue_result: StringProperty(default="")
    last_ue_message: StringProperty(default="")
    last_ue_report_path: StringProperty(default="")


CLASSES = (OnePochiIssue, OnePochiSettings)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.onepochi = bpy.props.PointerProperty(type=OnePochiSettings)


def unregister():
    if hasattr(bpy.types.Scene, "onepochi"):
        del bpy.types.Scene.onepochi
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
