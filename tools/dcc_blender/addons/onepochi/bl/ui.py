"""オペレータと UI パネル（docs/02_仕様書.md §11.1）。

UI 文言はすべて日本語（Q-18）。
"""

import os
import subprocess
import sys

import bpy
from bpy.types import Operator, Panel, UIList

from ..core import issues as issues_mod
from ..core import presets as presets_mod
from ..core import ue_bridge
from . import collect, pipeline

SEVERITY_ICON = {
    "error": "CANCEL",
    "warning": "ERROR",
    "info": "INFO",
}

SEVERITY_LABEL = {
    "error": "エラー",
    "warning": "警告",
    "info": "情報",
}


def _store_result(settings, result):
    settings.issues.clear()
    for issue in result.issues.sorted():
        item = settings.issues.add()
        item.rule_id = issue.rule_id
        item.severity = issue.severity
        item.target = issue.target or ""
        item.message = issue.message
        item.auto_fixed = issue.auto_fixed
    settings.last_summary = result.issues.summary()
    settings.last_manifest_path = result.manifest_path or ""
    settings.last_output_dir = (os.path.dirname(result.manifest_path)
                                if result.manifest_path else "")
    settings.last_ok = bool(result.ok)


def _send_to_ue(operator, settings, manifest_path, resolved_settings):
    """UE に取り込みを依頼し、結果を UI に反映する（P2 / docs/02 §2.1）。

    戻り値は成功したかどうか。失敗理由は `BridgeError` が日本語で持っている。
    """
    connection = ue_bridge.settings_from_preset(resolved_settings)
    try:
        outcome = ue_bridge.request_import(
            connection["plugin_dir"], manifest_path,
            base_url=connection["base_url"], timeout=connection["timeout"],
        )
    except ue_bridge.BridgeError as error:
        for line in error.lines():
            print("[ワンポチ] " + line)
        settings.last_ue_result = "failed"
        settings.last_ue_message = error.message
        operator.report({"ERROR"}, "UE への取り込みに失敗: " + error.message)
        return False

    for message in outcome["messages"]:
        print("[ワンポチ] " + message)

    report = outcome["report"] or {}
    errors = report.get("errors") or []
    warnings = report.get("warnings") or []
    for text in errors:
        print("[ワンポチ] UE エラー: " + text)
    for text in warnings:
        print("[ワンポチ] UE 警告: " + text)

    settings.last_ue_report_path = outcome.get("reportPath") or ""
    if outcome["ok"]:
        assets = report.get("assets") or []
        names = ", ".join(str(a.get("assetPath") or "") for a in assets)
        settings.last_ue_result = "success"
        settings.last_ue_message = names or "成功"
        operator.report({"INFO"}, "UE への取り込み完了: " + (names or "成功"))
        return True

    settings.last_ue_result = "failed"
    first = errors[0] if errors else "原因はレポートを確認してください"
    settings.last_ue_message = first
    operator.report({"ERROR"}, "UE への取り込みに失敗: " + first)
    return False


def _build_overrides(settings):
    """UI の明示指定をプリセットへの上書きとして組み立てる。

    合成順序: プリセット -> onepochi_asset.json -> **ここで作る overrides**
    （docs/02 §10.2）
    """
    overrides = {}
    material = {}
    if settings.material_mode != "PRESET":
        material["mode"] = settings.material_mode
    if settings.master_material_path:
        material["masterMaterialPath"] = settings.master_material_path
    if material:
        overrides["material"] = material
    return overrides or None


def _common_kwargs(settings):
    collection_name = (settings.target_collection
                       if settings.target_mode == "COLLECTION" else None)
    return {
        "preset": settings.preset,
        "asset_name": settings.asset_name or None,
        "collection_name": collection_name or None,
        "asset_folder": bpy.path.abspath(settings.asset_folder) if settings.asset_folder else None,
        "overrides": _build_overrides(settings),
        "environment": (settings.environment
                        if settings.environment != "PRESET" else None),
        "asset_type": settings.asset_type,
    }


class ONEPOCHI_OT_validate(Operator):
    bl_idname = "onepochi.validate"
    bl_label = "チェックのみ"
    bl_description = "書き出さずに検証だけ実行します"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.scene.onepochi
        try:
            result = pipeline.validate_only(**_common_kwargs(settings))
        except Exception as error:  # noqa: BLE001
            self.report({"ERROR"}, "検証中にエラー: {0}: {1}".format(
                type(error).__name__, error))
            return {"CANCELLED"}

        _store_result(settings, result)
        print(result.summary())
        if result.issues.has_error():
            self.report({"ERROR"}, "検証エラー: " + result.issues.summary())
        elif result.issues.count(issues_mod.WARNING):
            self.report({"WARNING"}, "警告あり: " + result.issues.summary())
        else:
            self.report({"INFO"}, "問題なし: " + result.issues.summary())
        return {"FINISHED"}


class ONEPOCHI_OT_export(Operator):
    bl_idname = "onepochi.export"
    bl_label = "ワンポチ"
    bl_description = "検証 → 整形 → FBX 出力 → マニフェスト出力をまとめて実行します"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.scene.onepochi

        kwargs = _common_kwargs(settings)
        kwargs["ue_path"] = settings.ue_path_override or None

        try:
            result = pipeline.export_asset(**kwargs)
        except Exception as error:  # noqa: BLE001
            self.report({"ERROR"}, "書き出し中にエラー: {0}: {1}".format(
                type(error).__name__, error))
            return {"CANCELLED"}

        _store_result(settings, result)
        print(result.summary())

        if not result.ok:
            self.report({"ERROR"}, "書き出し失敗: " + result.issues.summary())
            return {"CANCELLED"}

        if settings.auto_import_to_ue:
            _send_to_ue(self, settings, result.manifest_path, result.settings)
        else:
            self.report({"INFO"}, "書き出し完了: " + os.path.basename(
                result.manifest_path or ""))
        return {"FINISHED"}


class ONEPOCHI_OT_send_to_ue(Operator):
    bl_idname = "onepochi.send_to_ue"
    bl_label = "UE へ取り込む"
    bl_description = ("直前に書き出したマニフェストを、起動中の UE に取り込ませます"
                      "（書き出しをやり直しません）")
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return bool(context.scene.onepochi.last_manifest_path)

    def execute(self, context):
        settings = context.scene.onepochi
        manifest_path = settings.last_manifest_path
        if not os.path.isfile(manifest_path):
            self.report({"ERROR"},
                        "マニフェストが見つかりません: {0}".format(manifest_path))
            return {"CANCELLED"}
        try:
            resolved = presets_mod.resolve_settings(
                settings.preset,
                bpy.path.abspath(settings.asset_folder) if settings.asset_folder else None,
                _build_overrides(settings),
                None,
                settings.environment if settings.environment != "PRESET" else None,
            )
        except ValueError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        ok = _send_to_ue(self, settings, manifest_path, resolved)
        return {"FINISHED"} if ok else {"CANCELLED"}


class ONEPOCHI_OT_check_ue(Operator):
    bl_idname = "onepochi.check_ue"
    bl_label = "UE 接続確認"
    bl_description = "UE の Remote Control API に届くかどうかを確認します"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.scene.onepochi
        try:
            resolved = presets_mod.resolve_settings(settings.preset)
        except ValueError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        connection = ue_bridge.settings_from_preset(resolved)
        base_url = connection["base_url"]

        if not ue_bridge.is_reachable(base_url):
            settings.last_ue_result = "unreachable"
            settings.last_ue_message = "{0} に届きません".format(base_url)
            self.report({"ERROR"},
                        "UE に接続できません（{0}）。"
                        "UE が起動しているか、Remote Control API プラグインが"
                        "有効かを確認してください".format(base_url))
            return {"CANCELLED"}

        # 届くだけでは不十分。Python 実行が許可されているかまで確かめる
        try:
            ue_bridge.execute_python(
                "import {0}".format(ue_bridge.RECEIVER_MODULE.split(".")[0]),
                base_url, ue_bridge.PING_TIMEOUT)
        except ue_bridge.BridgeError as error:
            for line in error.lines():
                print("[ワンポチ] " + line)
            settings.last_ue_result = "denied"
            settings.last_ue_message = error.message
            self.report({"ERROR"}, error.message + "（詳細はコンソール）")
            return {"CANCELLED"}

        settings.last_ue_result = "success"
        settings.last_ue_message = "{0} に接続できました".format(base_url)
        self.report({"INFO"}, settings.last_ue_message)
        return {"FINISHED"}


class ONEPOCHI_OT_select_target(Operator):
    bl_idname = "onepochi.select_target"
    bl_label = "対象を選択"
    bl_description = "この指摘の対象オブジェクトを選択します"

    target: bpy.props.StringProperty()

    def execute(self, context):
        obj = bpy.data.objects.get(self.target)
        if obj is None:
            self.report({"WARNING"}, "オブジェクトが見つかりません: {0}".format(self.target))
            return {"CANCELLED"}
        for other in context.view_layer.objects:
            other.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        return {"FINISHED"}


class ONEPOCHI_OT_open_output(Operator):
    bl_idname = "onepochi.open_output"
    bl_label = "出力先を開く"
    bl_description = "書き出し先フォルダをエクスプローラで開きます"

    @classmethod
    def poll(cls, context):
        return bool(context.scene.onepochi.last_output_dir)

    def execute(self, context):
        path = context.scene.onepochi.last_output_dir
        if not os.path.isdir(path):
            self.report({"WARNING"}, "フォルダがありません: {0}".format(path))
            return {"CANCELLED"}
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as error:  # noqa: BLE001
            self.report({"WARNING"}, "開けませんでした: {0}".format(error))
            return {"CANCELLED"}
        return {"FINISHED"}


class ONEPOCHI_OT_guess_name(Operator):
    bl_idname = "onepochi.guess_name"
    bl_label = "名前を推測"
    bl_description = "アクティブオブジェクトからアセット名を埋めます"

    def execute(self, context):
        name = collect.guess_asset_name(context)
        if not name:
            self.report({"WARNING"}, "アクティブオブジェクトがありません")
            return {"CANCELLED"}
        context.scene.onepochi.asset_name = name
        return {"FINISHED"}


class ONEPOCHI_UL_issues(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        row = layout.row(align=True)
        row.label(text="", icon=SEVERITY_ICON.get(item.severity, "DOT"))
        row.label(text=item.rule_id)
        if item.target:
            operator = row.operator("onepochi.select_target", text=item.target,
                                    emboss=False)
            operator.target = item.target
        row.label(text=item.message)


class ONEPOCHI_PT_main(Panel):
    bl_label = "ワンポチツール"
    bl_idname = "ONEPOCHI_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "ワンポチ"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.onepochi

        box = layout.box()
        box.prop(settings, "preset")

        env_row = box.row(align=True)
        env_row.prop(settings, "environment")
        if settings.environment == "production":
            warn = box.row()
            warn.alert = True
            warn.label(text="本番 (/Game) に直接書き出します", icon="ERROR")

        box.prop(settings, "asset_type")
        box.prop(settings, "target_mode")
        if settings.target_mode == "COLLECTION":
            box.prop_search(settings, "target_collection", bpy.data, "collections",
                            text="コレクション")

        row = box.row(align=True)
        row.prop(settings, "asset_name")
        row.operator("onepochi.guess_name", text="", icon="EYEDROPPER")

        box.prop(settings, "asset_folder")
        box.prop(settings, "ue_path_override")

        material_box = layout.box()
        material_box.label(text="マテリアル", icon="MATERIAL")
        material_box.prop(settings, "material_mode")
        if settings.material_mode == "createInstances":
            material_box.prop(settings, "master_material_path")
            if not settings.master_material_path:
                material_box.label(
                    text="空の場合はプリセットの値を使います",
                    icon="INFO")

        ue_box = layout.box()
        ue_box.label(text="Unreal Engine", icon="PLUGIN")
        ue_box.prop(settings, "auto_import_to_ue")
        ue_row = ue_box.row(align=True)
        ue_row.operator("onepochi.check_ue", icon="LINKED")
        ue_row.operator("onepochi.send_to_ue", icon="IMPORT")
        if settings.last_ue_message:
            status = ue_box.row()
            status.alert = settings.last_ue_result != "success"
            status.label(
                text=settings.last_ue_message,
                icon="CHECKMARK" if settings.last_ue_result == "success" else "ERROR")

        row = layout.row(align=True)
        row.scale_y = 1.3
        row.operator("onepochi.validate", icon="CHECKMARK")
        row.operator("onepochi.export", icon="EXPORT")

        if settings.last_summary:
            result_box = layout.box()
            header = result_box.row()
            header.label(text="検証結果", icon="INFO")
            header.label(text=settings.last_summary)
            result_box.template_list("ONEPOCHI_UL_issues", "", settings, "issues",
                                     settings, "issue_index", rows=6)
            if settings.last_manifest_path:
                info = result_box.column(align=True)
                info.label(text=os.path.basename(settings.last_manifest_path),
                           icon="FILE_TEXT")
                info.operator("onepochi.open_output", icon="FILE_FOLDER")


CLASSES = (
    ONEPOCHI_OT_validate,
    ONEPOCHI_OT_export,
    ONEPOCHI_OT_send_to_ue,
    ONEPOCHI_OT_check_ue,
    ONEPOCHI_OT_select_target,
    ONEPOCHI_OT_open_output,
    ONEPOCHI_OT_guess_name,
    ONEPOCHI_UL_issues,
    ONEPOCHI_PT_main,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
