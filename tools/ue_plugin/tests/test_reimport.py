"""再インポート時の設定保持（T-7）の単体テスト（unreal 不要）。

`onepochi_ue.reimport` は**モジュールの最上位で `unreal` を import しない**
（UE 依存部は関数の中で `postprocess` を読む）ため、UE を起動せずに
方針の解決とカテゴリ分けを検証できる。

実行:
    python tools/ue_plugin/tests/test_reimport.py
"""

import inspect
import json
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
PROJECT_ROOT = os.path.dirname(os.path.dirname(PLUGIN_ROOT))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

from onepochi_ue import reimport  # noqa: E402

SCHEMA_PATH = os.path.join(PROJECT_ROOT, "schema", "onepochi_manifest.schema.json")
PRESET_PATH = os.path.join(PROJECT_ROOT, "tools", "presets", "project_default.json")


def _force_utf8_stdout():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_stdout()


def _policy(mode, override=None):
    return {"mode": mode, "override": list(override or [])}


class TestImportsWithoutUnreal(unittest.TestCase):
    """UE 無しで import できること（単体テストの前提そのもの）。"""

    def test_module_has_no_top_level_unreal_dependency(self):
        """`import unreal` はモジュールの最上位に書かない（関数の中で読む）。"""
        self.assertNotIn("unreal", vars(reimport))
        source = inspect.getsource(reimport)
        self.assertNotIn("\nimport unreal", source)

    def test_categories_have_no_duplicates(self):
        self.assertEqual(len(set(reimport.CATEGORIES)),
                         len(reimport.CATEGORIES))


class TestResolvePolicy(unittest.TestCase):
    def test_default_when_nothing_declared(self):
        policy = reimport.resolve_policy({}, {})
        self.assertEqual(policy["mode"], "preserveSettings")
        self.assertEqual(policy["override"], [])
        self.assertEqual(policy["source"], "default")
        self.assertEqual(policy["warnings"], [])

    def test_default_matches_preset(self):
        """既定値は `tools/presets/project_default.json` と一致させる。"""
        with open(PRESET_PATH, "r", encoding="utf-8") as handle:
            preset = json.load(handle)
        declared = preset.get("reimportPolicy") or {}
        self.assertEqual(declared.get("mode"), reimport.DEFAULT_MODE)
        self.assertEqual(declared.get("override", []), [])

    def test_manifest_level_policy_is_used(self):
        policy = reimport.resolve_policy(
            {"reimportPolicy": _policy("overwriteAll")}, {})
        self.assertEqual(policy["mode"], "overwriteAll")
        self.assertEqual(policy["source"], "manifest")

    def test_asset_level_overrides_manifest_level(self):
        manifest = {"reimportPolicy": _policy("overwriteAll", ["lod"])}
        asset = {"reimportPolicy": _policy("preserveSettings", ["nanite"])}
        policy = reimport.resolve_policy(manifest, asset)
        self.assertEqual(policy["mode"], "preserveSettings")
        self.assertEqual(policy["override"], ["nanite"])
        self.assertEqual(policy["source"], "asset")

    def test_asset_level_overrides_even_without_override_list(self):
        manifest = {"reimportPolicy": _policy("preserveSettings", ["nanite"])}
        asset = {"reimportPolicy": {"mode": "overwriteAll"}}
        policy = reimport.resolve_policy(manifest, asset)
        self.assertEqual(policy["mode"], "overwriteAll")
        # アセット側が勝つので、マニフェスト側の override は引き継がない
        self.assertEqual(policy["override"], [])

    def test_unknown_mode_warns_without_raising(self):
        sink = []
        policy = reimport.resolve_policy(
            {}, {"reimportPolicy": {"mode": "ぜんぶ消す"}}, sink)
        self.assertEqual(policy["mode"], reimport.DEFAULT_MODE)
        self.assertTrue(policy["warnings"])
        self.assertEqual(sink, policy["warnings"])
        self.assertIn("ぜんぶ消す", sink[0])

    def test_unknown_override_item_warns_and_is_dropped(self):
        sink = []
        policy = reimport.resolve_policy(
            {}, {"reimportPolicy": _policy("preserveSettings",
                                           ["nanite", "ナナイト", "collision"])},
            sink)
        self.assertEqual(policy["override"], ["nanite", "collision"])
        self.assertEqual(len(policy["warnings"]), 1)
        self.assertIn("ナナイト", policy["warnings"][0])
        self.assertEqual(sink, policy["warnings"])

    def test_missing_mode_warns(self):
        policy = reimport.resolve_policy({}, {"reimportPolicy": {"override": []}})
        self.assertEqual(policy["mode"], reimport.DEFAULT_MODE)
        self.assertTrue(policy["warnings"])

    def test_override_not_a_list_warns(self):
        policy = reimport.resolve_policy(
            {}, {"reimportPolicy": {"mode": "preserveSettings",
                                    "override": "nanite"}})
        self.assertEqual(policy["override"], [])
        self.assertTrue(policy["warnings"])

    def test_policy_not_an_object_falls_back_to_manifest(self):
        manifest = {"reimportPolicy": _policy("overwriteAll")}
        policy = reimport.resolve_policy(manifest, {"reimportPolicy": "保持"})
        self.assertEqual(policy["source"], "manifest")
        self.assertEqual(policy["mode"], "overwriteAll")
        self.assertTrue(policy["warnings"])

    def test_duplicated_override_items_are_deduped(self):
        policy = reimport.resolve_policy(
            {}, {"reimportPolicy": _policy("preserveSettings",
                                           ["nanite", "nanite"])})
        self.assertEqual(policy["override"], ["nanite"])

    def test_none_containers_do_not_raise(self):
        policy = reimport.resolve_policy(None, None)
        self.assertEqual(policy["mode"], reimport.DEFAULT_MODE)


class TestPlanCategories(unittest.TestCase):
    def test_first_import_applies_everything_regardless_of_mode(self):
        for mode in reimport.MODES:
            for override in ([], ["nanite"]):
                plan = reimport.plan_categories(_policy(mode, override),
                                                existed=False)
                self.assertEqual(plan["apply"], list(reimport.CATEGORIES),
                                 "mode={0} override={1}".format(mode, override))
                self.assertEqual(plan["preserve"], [])

    def test_overwrite_all_applies_everything(self):
        plan = reimport.plan_categories(_policy("overwriteAll"), existed=True)
        self.assertEqual(plan["apply"], list(reimport.CATEGORIES))
        self.assertEqual(plan["preserve"], [])

    def test_preserve_settings_without_override_preserves_everything(self):
        plan = reimport.plan_categories(_policy("preserveSettings"), existed=True)
        self.assertEqual(plan["apply"], [])
        self.assertEqual(plan["preserve"], list(reimport.CATEGORIES))

    def test_preserve_settings_with_override_applies_only_those(self):
        plan = reimport.plan_categories(
            _policy("preserveSettings", ["nanite"]), existed=True)
        self.assertEqual(plan["apply"], ["nanite"])
        self.assertNotIn("nanite", plan["preserve"])
        self.assertEqual(len(plan["preserve"]), len(reimport.CATEGORIES) - 1)

    def test_apply_and_preserve_are_a_partition(self):
        plan = reimport.plan_categories(
            _policy("preserveSettings", ["lightmap", "sockets"]), existed=True)
        self.assertEqual(sorted(plan["apply"] + plan["preserve"]),
                         sorted(reimport.CATEGORIES))
        self.assertEqual(plan["apply"], ["lightmap", "sockets"])

    def test_order_follows_categories_not_override(self):
        plan = reimport.plan_categories(
            _policy("preserveSettings", ["sockets", "nanite"]), existed=True)
        self.assertEqual(plan["apply"], ["nanite", "sockets"])

    def test_unknown_override_item_is_ignored(self):
        plan = reimport.plan_categories(
            {"mode": "preserveSettings", "override": ["ナナイト"]}, existed=True)
        self.assertEqual(plan["apply"], [])
        self.assertEqual(plan["preserve"], list(reimport.CATEGORIES))

    def test_unknown_mode_falls_back_to_preserve(self):
        """直接呼ばれても**保持する側（安全側）**に倒す。"""
        plan = reimport.plan_categories({"mode": "ぜんぶ消す"}, existed=True)
        self.assertEqual(plan["apply"], [])
        self.assertEqual(plan["preserve"], list(reimport.CATEGORIES))

    def test_empty_policy(self):
        plan = reimport.plan_categories({}, existed=True)
        self.assertEqual(plan["apply"], [])
        self.assertEqual(plan["preserve"], list(reimport.CATEGORIES))

    def test_resolve_then_plan_end_to_end(self):
        manifest = {"reimportPolicy": _policy("overwriteAll")}
        asset = {"reimportPolicy": _policy("preserveSettings", ["nanite"])}
        plan = reimport.plan_categories(
            reimport.resolve_policy(manifest, asset), existed=True)
        self.assertEqual(plan["apply"], ["nanite"])


class TestSchemaAgreement(unittest.TestCase):
    """**スキーマを実際に読んで**定数と突き合わせる（P-14 の教訓）。"""

    def setUp(self):
        with open(SCHEMA_PATH, "r", encoding="utf-8") as handle:
            self.schema = json.load(handle)
        self.definition = self.schema["$defs"]["reimportPolicy"]

    def test_categories_match_schema_override_enum(self):
        enum = self.definition["properties"]["override"]["items"]["enum"]
        self.assertEqual(sorted(enum), sorted(reimport.CATEGORIES),
                         "reimport.CATEGORIES がスキーマの "
                         "reimportPolicy.override の enum と一致しません")
        self.assertEqual(len(enum), len(set(enum)))

    def test_modes_match_schema_mode_enum(self):
        enum = self.definition["properties"]["mode"]["enum"]
        self.assertEqual(sorted(enum), sorted(reimport.MODES))

    def test_default_mode_is_a_valid_mode(self):
        self.assertIn(reimport.DEFAULT_MODE,
                      self.definition["properties"]["mode"]["enum"])

    def test_asset_types_can_declare_the_policy(self):
        for name in ("staticMeshAsset", "skeletalMeshAsset",
                     "animationSequenceAsset"):
            properties = self.schema["$defs"][name]["properties"]
            self.assertIn("reimportPolicy", properties, name)


class TestNormalizeAssetPath(unittest.TestCase):
    def test_package_path_passthrough(self):
        self.assertEqual(reimport.normalize_asset_path("/Game/X/M_A"),
                         "/Game/X/M_A")

    def test_object_path(self):
        self.assertEqual(reimport.normalize_asset_path("/Game/X/M_A.M_A"),
                         "/Game/X/M_A")

    def test_class_prefixed_reference(self):
        self.assertEqual(
            reimport.normalize_asset_path(
                "/Script/Engine.MaterialInstanceConstant'/Game/X/MI_A.MI_A'"),
            "/Game/X/MI_A")

    def test_none(self):
        self.assertIsNone(reimport.normalize_asset_path(None))


class TestPostprocessContract(unittest.TestCase):
    """`postprocess` 側の受け口が `reimport` と噛み合っているか。

    `postprocess` は最上位で `unreal` を import するため、
    **ダミーの `unreal` モジュールを差し込んで**構造だけを確認する
    （API を呼ばないので挙動の検証にはならない。実機確認は N-2）。
    """

    def setUp(self):
        self._injected = "unreal" not in sys.modules
        if self._injected:
            sys.modules["unreal"] = types.ModuleType("unreal")
        try:
            from onepochi_ue import postprocess
        except Exception as error:  # pragma: no cover
            raise unittest.SkipTest(
                "postprocess を読めません: {0}".format(error))
        self.postprocess = postprocess

    def tearDown(self):
        if self._injected:
            sys.modules.pop("unreal", None)
            sys.modules.pop("onepochi_ue.postprocess", None)

    def test_apply_static_mesh_accepts_categories(self):
        signature = inspect.signature(self.postprocess.apply_static_mesh)
        self.assertIn("categories", signature.parameters)
        # 既定は None = 従来どおり全適用（後方互換）
        self.assertIsNone(signature.parameters["categories"].default)

    def test_applied_categories_are_a_subset(self):
        self.assertTrue(
            set(self.postprocess.APPLIED_CATEGORIES)
            <= set(reimport.CATEGORIES),
            "postprocess.APPLIED_CATEGORIES に reimport.CATEGORIES 外の"
            "項目があります")

    def test_lod_is_not_applied_by_postprocess(self):
        """LOD は別モジュールの担当（T-5）。ここで適用しないことを明示する。"""
        self.assertNotIn("lod", self.postprocess.APPLIED_CATEGORIES)

    def test_build_settings_provider_is_shared(self):
        self.assertTrue(hasattr(self.postprocess, "build_settings_provider"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
