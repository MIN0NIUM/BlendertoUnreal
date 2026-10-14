"""コアロジックの単体テスト（bpy 不要）。

実行:
    python tools/dcc_blender/tests/test_core.py
    python -m unittest discover -s tools/dcc_blender/tests
"""

import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
#: アドオンの親 = tools/dcc_blender/addons（Blender の Script Directories 規約）
ADDON_ROOT = os.path.join(os.path.dirname(HERE), "addons")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
if ADDON_ROOT not in sys.path:
    sys.path.insert(0, ADDON_ROOT)

from onepochi.core import issues as issues_mod  # noqa: E402
from onepochi.core import manifest as manifest_mod  # noqa: E402
from onepochi.core import naming  # noqa: E402
from onepochi.core import presets as presets_mod  # noqa: E402
from onepochi.core import ue_bridge  # noqa: E402


def _force_utf8_stdout():
    """Windows の既定コンソール（cp932）でも日本語を出せるようにする。

    サブプロセスとして起動されると stdout が cp932 になり、`✔` などの文字で
    UnicodeEncodeError を出して**何も表示されずに死ぬ**。呼び出し側に
    `-X utf8` を要求せずに済むよう、ここで自衛する（docs/99 の既知の落とし穴を参照）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_stdout()


class TestNaming(unittest.TestCase):
    def test_valid_names(self):
        for name in ("Crate_01", "SM_Crate_01", "a", "A1_b2"):
            self.assertIsNone(naming.name_problem(name), name)

    def test_leading_digit_rejected(self):
        problem = naming.name_problem("01_static_minimal")
        self.assertIsNotNone(problem)
        self.assertIn("数字で始まって", problem)

    def test_leading_digit_allowed_for_samples(self):
        self.assertIsNone(naming.name_problem("01_static_minimal",
                                              allow_leading_digit=True))

    def test_non_ascii_rejected(self):
        problem = naming.name_problem("木箱")
        self.assertIn("ASCII", problem)

    def test_space_and_dot_rejected(self):
        self.assertIn("空白", naming.name_problem("Crate 01"))
        self.assertIn("ドット", naming.name_problem("Crate.001"))
        self.assertIn("ハイフン", naming.name_problem("Crate-01"))

    def test_empty_rejected(self):
        self.assertIsNotNone(naming.name_problem(""))

    def test_material_names(self):
        self.assertIsNone(naming.material_problem("M_Crate_Body"))
        self.assertIn("既定マテリアル名", naming.material_problem("Material"))
        self.assertIn("既定マテリアル名", naming.material_problem("Material.001"))
        self.assertIsNotNone(naming.material_problem("crate_body"))
        self.assertIsNotNone(naming.material_problem("M_木箱"))

    def test_mi_name(self):
        self.assertEqual(naming.mi_name("M_Crate_Body"), "MI_Crate_Body")
        self.assertIsNone(naming.mi_name("Crate_Body"))

    def test_parse_collision_name(self):
        self.assertEqual(naming.parse_collision_name("UCX_Crate_01_01"),
                         ("convex", "Crate_01", 1))
        self.assertEqual(naming.parse_collision_name("UBX_Crate_01_02"),
                         ("box", "Crate_01", 2))
        self.assertEqual(naming.parse_collision_name("USP_Ball_07"),
                         ("sphere", "Ball", 7))
        self.assertEqual(naming.parse_collision_name("UCP_Pipe_01"),
                         ("capsule", "Pipe", 1))
        self.assertIsNone(naming.parse_collision_name("Crate_01"))

    def test_parse_collision_without_index(self):
        self.assertEqual(naming.parse_collision_name("UCX_Crate"),
                         ("convex", "Crate", None))

    def test_collision_name_is_ambiguous_without_context(self):
        """`UCX_Crate_01` は本体 `Crate_01` とも `Crate` の 1 番とも読める。

        文脈が無ければ従来どおり「末尾の連番を剥がす」読み方になる。
        """
        self.assertEqual(naming.parse_collision_name("UCX_Crate_01"),
                         ("convex", "Crate", 1))

    def test_collision_name_prefers_an_existing_body(self):
        """本体メッシュ名の一覧を渡すと、実在する読み方が選ばれる（S-08 の実例）。"""
        self.assertEqual(
            naming.parse_collision_name("UCX_Crate_01", {"Crate_01"}),
            ("convex", "Crate_01", None))
        # 連番付きの読み方が実在するならそちら
        self.assertEqual(
            naming.parse_collision_name("UCX_Crate_01", {"Crate"}),
            ("convex", "Crate", 1))
        # 連番付きの本体が実在する場合も取り違えない
        self.assertEqual(
            naming.parse_collision_name("UCX_Crate_01_01", {"Crate_01"}),
            ("convex", "Crate_01", 1))

    def test_collision_name_falls_back_when_nothing_matches(self):
        """どちらの読み方も実在しないときは従来の規則（V-041 に判定させる）。"""
        self.assertEqual(
            naming.parse_collision_name("UCX_Box_09", {"Crate_01"}),
            ("convex", "Box", 9))

    def test_parse_socket_name(self):
        self.assertEqual(naming.parse_socket_name("SOCKET_weapon_r"), "weapon_r")
        self.assertIsNone(naming.parse_socket_name("Socket_weapon_r"))
        self.assertIsNone(naming.parse_socket_name("SOCKET_"))

    def test_socket_name_problem_accepts_valid_names(self):
        self.assertIsNone(naming.socket_name_problem("SOCKET_weapon_r"))
        self.assertIsNone(naming.socket_name_problem("SOCKET_Lid"))
        # ソケットではない名前には何も言わない
        self.assertIsNone(naming.socket_name_problem("Crate_01"))

    def test_socket_name_problem_rejects_schema_violations(self):
        """V-050。スキーマの `ueAssetName` に違反する名前を DCC 側で止める（D-3）。"""
        for bad in ("SOCKET_1_weapon", "SOCKET_weapon r", "SOCKET_weapon-r",
                    "SOCKET_武器", "SOCKET_weapon.001", "SOCKET_"):
            problem = naming.socket_name_problem(bad)
            self.assertIsNotNone(problem, "{0} が通ってしまった".format(bad))
            self.assertIsInstance(problem, str)


    def test_parse_lod_collection(self):
        self.assertEqual(naming.parse_lod_collection("LOD0"), 0)
        self.assertEqual(naming.parse_lod_collection("LOD12"), 12)
        self.assertIsNone(naming.parse_lod_collection("LOD"))
        self.assertIsNone(naming.parse_lod_collection("lod0"))

    def test_reserved_collections(self):
        for name in ("LOD0", "LOD3", "Collision", "Sockets"):
            self.assertTrue(naming.is_reserved_collection(name), name)
        self.assertFalse(naming.is_reserved_collection("Crate_01"))

    def test_strip_duplicate_suffix(self):
        self.assertEqual(naming.strip_duplicate_suffix("LOD0.001"), "LOD0")
        self.assertEqual(naming.strip_duplicate_suffix("Collision.012"), "Collision")
        self.assertEqual(naming.strip_duplicate_suffix("LOD0"), "LOD0")
        # 3桁以外は Blender の重複サフィックスではないので剥がさない
        self.assertEqual(naming.strip_duplicate_suffix("LOD0.1"), "LOD0.1")
        self.assertTrue(naming.has_duplicate_suffix("LOD0.001"))
        self.assertFalse(naming.has_duplicate_suffix("LOD0"))

    def test_lod_collection_tolerates_blender_duplicate_suffix(self):
        """`LOD0.001` を LOD0 として認識する（docs/99 P-24）。

        **コレクション名は .blend 全体で一意**なので、1 ファイルに 2 つの
        アセットコレクションを置くと 2 つめの `LOD0` には Blender が
        必ず `.001` を付ける。バッチ書き出し（N-5）の前提条件。
        """
        self.assertEqual(naming.parse_lod_collection("LOD0.001"), 0)
        self.assertEqual(naming.parse_lod_collection("LOD2.014"), 2)
        self.assertTrue(naming.is_reserved_collection("LOD0.001"))
        self.assertTrue(naming.is_reserved_collection("Collision.001"))
        self.assertTrue(naming.is_reserved_collection("Sockets.002"))
        # アセット名側では従来どおりドットをエラーにする（緩めない）
        self.assertIsNotNone(naming.name_problem("Crate_01.001"))

    def test_ue_asset_name(self):
        self.assertEqual(naming.ue_asset_name("StaticMesh", "Crate_01"), "SM_Crate_01")
        self.assertEqual(naming.ue_asset_name("StaticMesh", "SM_Crate_01"), "SM_Crate_01")
        self.assertEqual(naming.ue_asset_name("SkeletalMesh", "Dummy"), "SK_Dummy")
        self.assertEqual(naming.ue_asset_name("Skeleton", "Dummy"), "SKEL_Dummy")

    def test_strip_known_prefix(self):
        self.assertEqual(naming.strip_known_prefix("SM_Crate_01"), "Crate_01")
        self.assertEqual(naming.strip_known_prefix("SKEL_Dummy"), "Dummy")
        self.assertEqual(naming.strip_known_prefix("Crate_01"), "Crate_01")

    def test_ue_path(self):
        self.assertIsNone(naming.ue_path_problem("/Game/_Dev/Props/Crate_01"))
        self.assertIn("'/' で始めて", naming.ue_path_problem("Game/Props"))
        self.assertIn("'/' で終わらせない", naming.ue_path_problem("/Game/Props/"))
        self.assertIsNotNone(naming.ue_path_problem("/Game/1Props"))
        self.assertIsNotNone(naming.ue_path_problem(""))

    def test_case_collisions(self):
        collisions = naming.find_case_collisions(["Crate", "crate", "Box"])
        self.assertEqual(collisions, [("Crate", "crate")])
        self.assertEqual(naming.find_case_collisions(["A", "B"]), [])


class TestPresets(unittest.TestCase):
    def test_deep_merge(self):
        base = {"a": 1, "b": {"c": 2, "d": 3}, "e": [1, 2]}
        override = {"b": {"c": 20}, "e": [9]}
        merged = presets_mod.deep_merge(base, override)
        self.assertEqual(merged, {"a": 1, "b": {"c": 20, "d": 3}, "e": [9]})
        # 元の辞書を壊していないこと
        self.assertEqual(base["b"]["c"], 2)

    def test_deep_merge_none_overrides(self):
        merged = presets_mod.deep_merge({"a": 1}, {"a": None})
        self.assertIsNone(merged["a"])

    def test_real_presets_exist(self):
        names = presets_mod.list_presets()
        for expected in ("project_default", "prop", "environment", "character",
                         "sample_debug"):
            self.assertIn(expected, names)

    def test_resolve_preset_inherits(self):
        resolved = presets_mod.resolve_preset("prop")
        self.assertEqual(resolved["name"], "prop")
        # project_default から継承した値
        self.assertIn("limits", resolved)
        self.assertEqual(resolved["limits"]["maxMaterialSlots"], 8)
        # prop 自身の値で上書きされた destination
        self.assertEqual(resolved["destination"]["pathTemplate"],
                         "{contentRoot}/Props/{assetName}")
        self.assertNotIn("extends", resolved)

    def test_resolve_preset_two_levels(self):
        resolved = presets_mod.resolve_preset("environment_mobile")
        # environment -> project_default の2段継承
        self.assertFalse(resolved["staticMesh"]["nanite"]["enabled"])
        self.assertEqual(resolved["staticMesh"]["lod"]["mode"], "manual")
        self.assertEqual(resolved["limits"]["maxTextureResolution"], 2048)
        # 継承元 environment の値が残っていること
        self.assertEqual(resolved["staticMesh"]["lod"]["autoLodCount"], 3)

    def test_sample_debug_allows_leading_digit(self):
        resolved = presets_mod.resolve_preset("sample_debug")
        self.assertTrue(resolved["validation"]["allowLeadingDigitInNames"])

    def test_sample_texture_inherits_material_mode(self):
        """sample_texture は sample_material から mode=createInstances を継承する（M-3）。"""
        resolved = presets_mod.resolve_preset("sample_texture")
        material = resolved["material"]
        # 継承元 sample_material の値
        self.assertEqual(material["mode"], "createInstances")
        self.assertEqual(material["masterMaterialPath"],
                         "/Game/_Dev/Materials/M_OnePochi_Base")
        # sample_texture 自身の値で上書きされたもの
        self.assertTrue(material["importTextures"])
        # textureParameterMap は project_default から継承する。
        # マスターマテリアル M_OnePochi_Base に実在するパラメータ名であること（実測済み）
        parameter_map = material["textureParameterMap"]
        for suffix, parameter in (("_BC", "BaseColor"), ("_N", "Normal"),
                                  ("_ORM", "ORM"), ("_E", "Emissive")):
            self.assertEqual(parameter_map[suffix], parameter)
        # sample_debug -> prop -> project_default の多段継承も効いていること
        self.assertTrue(resolved["validation"]["allowLeadingDigitInNames"])
        self.assertNotIn("extends", resolved)

    def test_character_mobile_limits(self):
        resolved = presets_mod.resolve_preset("character_mobile")
        self.assertEqual(resolved["skeletalMesh"]["maxBoneInfluences"], 4)
        self.assertEqual(resolved["limits"]["maxBones"], 128)

    def test_environment_dev_is_default(self):
        """環境を指定しなければ Dev。本番を汚さない側に倒す（R-2）。"""
        settings = presets_mod.resolve_settings("prop")
        self.assertEqual(settings["destination"]["environment"], "dev")
        self.assertEqual(settings["destination"]["contentRoot"], "/Game/_Dev")
        self.assertEqual(
            presets_mod.resolve_destination_path(settings, "Crate_01"),
            "/Game/_Dev/Props/Crate_01")

    def test_environment_production_switches_content_root(self):
        """本番環境では /Game 直下に出る（R-2）。"""
        settings = presets_mod.resolve_settings("prop", environment="production")
        self.assertEqual(settings["destination"]["contentRoot"], "/Game")
        self.assertEqual(settings["destination"]["pathWhitelist"], ["/Game"])
        self.assertEqual(
            presets_mod.resolve_destination_path(settings, "Crate_01"),
            "/Game/Props/Crate_01")

    def test_environment_whitelist_follows_environment(self):
        """Dev のホワイトリストは本番パスを拒否する（事故防止）。"""
        dev = presets_mod.resolve_settings("prop")
        self.assertEqual(dev["destination"]["pathWhitelist"], ["/Game/_Dev"])

    def test_unknown_environment_raises(self):
        with self.assertRaises(ValueError) as context:
            presets_mod.resolve_settings("prop", environment="staging")
        self.assertIn("staging", str(context.exception))

    def test_environment_names_are_ordered(self):
        settings = presets_mod.resolve_preset("prop")
        self.assertEqual(presets_mod.environment_names(settings),
                         ["dev", "production"])

    def test_preset_can_pin_environment(self):
        """プリセット側で環境を固定できる（sample_debug は dev 固定）。"""
        settings = presets_mod.resolve_settings("sample_debug")
        self.assertEqual(settings["destination"]["environment"], "dev")

    def test_explicit_path_wins_over_template(self):
        settings = presets_mod.resolve_settings("prop")
        self.assertEqual(
            presets_mod.resolve_destination_path(settings, "Crate_01",
                                                 "/Game/Override/Here"),
            "/Game/Override/Here")

    def test_dcc_layout_defaults(self):
        """DCC データフォルダの構造は設定から取れる（R-1）。"""
        settings = presets_mod.resolve_settings("prop")
        layout = presets_mod.dcc_layout(settings)
        self.assertEqual(layout["fbxSubdir"], "fbx")
        self.assertEqual(layout["texturesSubdir"], "textures")
        self.assertEqual(layout["manifestName"], "onepochi.json")

    def test_dcc_layout_is_overridable(self):
        """スタジオ都合でサブフォルダ名を変えられる（R-1）。"""
        settings = presets_mod.resolve_settings(
            "prop", overrides={"dccLayout": {"fbxSubdir": "export_fbx"}})
        layout = presets_mod.dcc_layout(settings)
        self.assertEqual(layout["fbxSubdir"], "export_fbx")
        # 上書きしなかったキーは既定のまま残る
        self.assertEqual(layout["texturesSubdir"], "textures")

    def test_asset_subfolder_uses_layout(self):
        settings = presets_mod.resolve_settings(
            "prop", overrides={"dccLayout": {"fbxSubdir": "out"}})
        path = presets_mod.asset_subfolder(settings, os.path.join("C:", "A"), "fbxSubdir")
        self.assertTrue(path.endswith(os.path.join("A", "out")))

    def test_path_template_collapses_double_slash(self):
        """contentRoot が '/' 終わりでも '//' にならない。"""
        self.assertEqual(
            presets_mod.expand_path_template("{contentRoot}/Props/{assetName}",
                                             contentRoot="/Game/", assetName="X"),
            "/Game/Props/X")

    def test_expand_path_template(self):
        self.assertEqual(
            presets_mod.expand_path_template("/Game/_Dev/Props/{assetName}",
                                             assetName="Crate_01"),
            "/Game/_Dev/Props/Crate_01")

    def test_missing_preset_raises(self):
        with self.assertRaises(IOError):
            presets_mod.load_preset_file("no_such_preset")


class TestManifest(unittest.TestCase):
    def test_run_id_format(self):
        run_id = manifest_mod.make_run_id()
        self.assertRegex(run_id, r"^\d{8}_\d{6}_[0-9a-f]{6}$")

    def test_asset_id_is_unique_per_index(self):
        """バッチ書き出し（N-5）で `assets[].id` が衝突しないこと。"""
        ids = [manifest_mod.asset_id(i) for i in range(3)]
        self.assertEqual(ids, ["a001", "a002", "a003"])
        self.assertEqual(len(set(ids)), len(ids))
        # 単一アセット経路は従来どおり a001 のまま
        self.assertEqual(manifest_mod.asset_id(0), "a001")
        self.assertEqual(manifest_mod.asset_id(999), "a1000")

    def test_check_manifest_shape_accepts_two_assets(self):
        """`assets[]` が 2 件のマニフェストが構造チェックを通ること（N-5）。"""
        def static_asset(asset_id, name):
            return {
                "id": asset_id, "assetType": "StaticMesh", "name": name,
                "destinationPath": "/Game/_Dev/Samples/" + name,
                "sourceFile": name + ".fbx", "staticMesh": {},
            }

        manifest = {
            "schemaVersion": manifest_mod.SCHEMA_VERSION,
            "runId": "20261011_000000_abcdef",
            "generator": {}, "coordinateSystem": {}, "source": {},
            "destination": {}, "validation": {},
            "assets": [static_asset("a001", "SM_Crate_01"),
                       static_asset("a002", "SM_Barrel_01")],
        }
        self.assertEqual(manifest_mod.check_manifest_shape(manifest), [])

    def test_bone_hierarchy_hash_is_deterministic(self):
        hierarchy = [(None, "root"), ("root", "pelvis"), ("pelvis", "spine_01")]
        first = manifest_mod.bone_hierarchy_hash(hierarchy)
        second = manifest_mod.bone_hierarchy_hash(list(hierarchy))
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("sha1:"))

    def test_bone_hierarchy_hash_detects_change(self):
        a = manifest_mod.bone_hierarchy_hash([(None, "root"), ("root", "pelvis")])
        b = manifest_mod.bone_hierarchy_hash([(None, "root"), ("root", "hips")])
        self.assertNotEqual(a, b)

    def test_coordinate_system_block(self):
        block = manifest_mod.blender_coordinate_system()
        self.assertEqual(block["sourceUpAxis"], "Z")
        self.assertEqual(block["sourceForwardAxis"], "-Y")
        self.assertEqual(block["targetForwardAxis"], "X")
        self.assertEqual(block["appliedScale"], 100.0)

    def test_check_manifest_shape_detects_missing(self):
        problems = manifest_mod.check_manifest_shape({"schemaVersion": "0.2.0"})
        self.assertTrue(any("runId" in p for p in problems))
        self.assertTrue(any("assets" in p for p in problems))

    def test_check_manifest_shape_rejects_major_mismatch(self):
        manifest = {
            "schemaVersion": "9.0.0", "runId": "20261010_000000_abcdef",
            "generator": {}, "coordinateSystem": {}, "source": {},
            "destination": {}, "validation": {},
            "assets": [{"assetType": "StaticMesh", "id": "a", "name": "SM_A",
                        "destinationPath": "/Game/A", "sourceFile": "a.fbx",
                        "staticMesh": {}}],
        }
        problems = manifest_mod.check_manifest_shape(manifest)
        self.assertTrue(any("メジャーバージョン" in p for p in problems))

    def test_check_manifest_shape_accepts_minimal_valid(self):
        manifest = {
            "schemaVersion": manifest_mod.SCHEMA_VERSION,
            "runId": "20261010_000000_abcdef",
            "generator": {}, "coordinateSystem": {}, "source": {},
            "destination": {}, "validation": {},
            "assets": [{"assetType": "StaticMesh", "id": "a", "name": "SM_A",
                        "destinationPath": "/Game/A", "sourceFile": "a.fbx",
                        "staticMesh": {}}],
        }
        self.assertEqual(manifest_mod.check_manifest_shape(manifest), [])

    # --- skeleton ブロックの型（force=True の最後の砦） ---

    def _skeletal_manifest(self, skeleton):
        return {
            "schemaVersion": manifest_mod.SCHEMA_VERSION,
            "runId": "20261010_000000_abcdef",
            "generator": {}, "coordinateSystem": {}, "source": {},
            "destination": {}, "validation": {},
            "assets": [{"assetType": "SkeletalMesh", "id": "a", "name": "SK_A",
                        "destinationPath": "/Game/A", "sourceFile": "a.fbx",
                        "skeleton": skeleton, "skeletalMesh": {}}],
        }

    def test_check_manifest_shape_accepts_a_valid_root_bone(self):
        manifest = self._skeletal_manifest(
            {"mode": "create_new", "rootBoneName": "root", "boneCount": 3})
        self.assertEqual(manifest_mod.check_manifest_shape(manifest), [])

    def test_check_manifest_shape_rejects_null_root_bone(self):
        """スキーマは `"type": "string"` で null 非許容。

        通常は V-061 が止めるが `force=True` はそれを飛び越えるため、
        書き出し直前のここで弾かないとスキーマ違反のマニフェストが出る。
        """
        for bad in (None, "", 0, [], {}):
            manifest = self._skeletal_manifest(
                {"mode": "create_new", "rootBoneName": bad, "boneCount": 3})
            problems = manifest_mod.check_manifest_shape(manifest)
            self.assertTrue(
                any("rootBoneName" in p for p in problems),
                "rootBoneName={0!r} が通ってしまった: {1}".format(bad, problems))

    def test_check_manifest_shape_ignores_root_bone_for_static(self):
        """Static Mesh には skeleton が無いので、この検査を持ち込まない。"""
        manifest = {
            "schemaVersion": manifest_mod.SCHEMA_VERSION,
            "runId": "20261010_000000_abcdef",
            "generator": {}, "coordinateSystem": {}, "source": {},
            "destination": {}, "validation": {},
            "assets": [{"assetType": "StaticMesh", "id": "a", "name": "SM_A",
                        "destinationPath": "/Game/A", "sourceFile": "a.fbx",
                        "staticMesh": {}}],
        }
        self.assertEqual(manifest_mod.check_manifest_shape(manifest), [])

    def test_iso_now_has_offset(self):
        text = manifest_mod.iso_now()
        self.assertRegex(text, r"[+-]\d{2}:\d{2}$")


class TestIssues(unittest.TestCase):
    def test_status_passed(self):
        issues = issues_mod.IssueList()
        issues.info("V-031", "情報")
        self.assertEqual(issues.status(), "passed")

    def test_status_with_warnings(self):
        issues = issues_mod.IssueList()
        issues.warn("V-026", "警告")
        self.assertEqual(issues.status(), "passedWithWarnings")

    def test_status_failed(self):
        issues = issues_mod.IssueList()
        issues.warn("V-026", "警告")
        issues.error("V-025", "エラー")
        self.assertEqual(issues.status(), "failed")
        self.assertTrue(issues.has_error())

    def test_sort_order(self):
        issues = issues_mod.IssueList()
        issues.info("V-031", "i")
        issues.error("V-025", "e")
        issues.warn("V-026", "w")
        order = [i.severity for i in issues.sorted()]
        self.assertEqual(order, ["error", "warning", "info"])

    def test_to_manifest_keys(self):
        issues = issues_mod.IssueList()
        issues.warn("V-021", "N-gon", target="Crate_01", auto_fixed=True)
        entry = issues.to_manifest()[0]
        self.assertEqual(set(entry), {"ruleId", "severity", "target", "message",
                                      "autoFixed"})
        self.assertTrue(entry["autoFixed"])

    def test_summary_counts(self):
        issues = issues_mod.IssueList()
        issues.error("V-001", "a")
        issues.warn("V-002", "b")
        issues.warn("V-003", "c")
        summary = issues.summary()
        self.assertIn("エラー 1 件", summary)
        self.assertIn("警告 2 件", summary)



class TestUeBridge(unittest.TestCase):
    """UE への依頼（P2）。HTTP は張らず、組み立てと切り分けだけを見る。"""

    # --- 送る文の組み立て ---

    def test_statement_is_a_single_line(self):
        """複合文を混ぜると実行モードによって解釈が変わるため1行に収める。"""
        statement = ue_bridge.build_import_statement(
            r"C:\tools\ue_plugin", r"C:\a\onepochi.json", r"C:\a\ue_report.json")
        self.assertNotIn("\n", statement)
        self.assertNotIn("if ", statement)

    def test_statement_normalizes_backslashes(self):
        """Windows のパスをそのまま埋めるとバックスラッシュがエスケープ扱いになる。"""
        statement = ue_bridge.build_import_statement(
            r"C:\tools\ue_plugin", r"C:\Users\x\onepochi.json")
        self.assertNotIn("\\", statement)
        self.assertIn("C:/Users/x/onepochi.json", statement)

    def test_statement_calls_the_receiver(self):
        statement = ue_bridge.build_import_statement(
            "/p", "/a/onepochi.json", "/a/ue_report.json", True, True)
        self.assertIn("onepochi_rc.handle(", statement)
        self.assertIn("importlib.reload(onepochi_rc)", statement)
        self.assertIn("True, True)", statement)

    def test_statement_passes_empty_report_as_empty_string(self):
        """Remote Control では引数を省略できないので空文字で渡す。"""
        statement = ue_bridge.build_import_statement("/p", "/a/onepochi.json")
        self.assertIn('r""', statement)

    # --- エラーの切り分け（UE 5.8.3 の実際の文面） ---

    def test_python_execution_disabled_is_identified(self):
        body = ('{"errorMessage": "Object Default__PythonScriptLibrary cannot be '
                'accessed remotely, check remote control project settings."}')
        error = ue_bridge.classify_failure(400, body)
        self.assertIn("Python の実行が許可されていません", error.message)
        # 恒久対策の置き場所を案内する（docs/99 P-29 / DefaultEngine.ini では読まれない）
        self.assertIn("check_rc_config.py", error.hint)
        self.assertIn("bEnableRemotePythonExecution", error.hint)
        self.assertIn("DefaultRemoteControl.ini", error.hint)

    def test_function_not_allowlisted_is_identified(self):
        body = ('{"errorMessage": "Executing function \'PythonScriptLibrary '
                'ExecutePythonCommandEx\' is not allowed by remote control settings."}')
        error = ue_bridge.classify_failure(400, body)
        self.assertIn("許可リスト", error.message)
        self.assertIn("ExecutePythonCommandEx", error.hint)

    def test_404_points_at_the_plugin(self):
        error = ue_bridge.classify_failure(404, "not found")
        self.assertIn("404", error.message)
        self.assertIn("Remote Control API", error.hint)

    def test_unknown_failure_keeps_the_raw_message(self):
        error = ue_bridge.classify_failure(500, '{"errorMessage": "boom"}')
        self.assertIn("500", error.message)
        self.assertEqual(error.detail, "boom")

    def test_non_json_body_is_tolerated(self):
        """UE が HTML やプレーンテキストを返しても落ちない。"""
        error = ue_bridge.classify_failure(503, "<html>Service Unavailable</html>")
        self.assertIn("503", error.message)

    def test_error_lines_are_japanese_and_flat(self):
        """UI にそのまま出せる行のリストになっている（Q-18）。"""
        error = ue_bridge.classify_failure(
            400, '{"errorMessage": "cannot be accessed remotely"}')
        lines = error.lines()
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(isinstance(line, str) for line in lines))

    # --- ログの取り出し（レスポンスの形に依存しない） ---

    def test_log_extraction_handles_both_casings(self):
        body = '{"LogOutput": [{"Type": "Info", "Output": "hello"}]}'
        self.assertIn("UE: [Info] hello", ue_bridge._extract_log(body))
        body = '{"logOutput": [{"type": "Info", "output": "hello"}]}'
        self.assertIn("UE: [Info] hello", ue_bridge._extract_log(body))

    def test_log_extraction_never_raises(self):
        for body in ("", "not json", "[]", "{}", '{"logOutput": "nope"}',
                     '{"logOutput": [null]}'):
            self.assertIsInstance(ue_bridge._extract_log(body), list)

    # --- 設定の読み出し ---

    def test_connection_settings_come_from_preset(self):
        """D-2: 接続先をコードに埋め込まない。"""
        settings = presets_mod.resolve_settings("prop")
        connection = ue_bridge.settings_from_preset(settings)
        self.assertEqual(connection["base_url"], "http://127.0.0.1:30010")
        self.assertEqual(connection["timeout"], 180.0)

    def test_connection_settings_are_overridable(self):
        connection = ue_bridge.settings_from_preset(
            {"ue": {"remoteControlUrl": "http://10.0.0.5:31000",
                    "timeoutSeconds": 30}})
        self.assertEqual(connection["base_url"], "http://10.0.0.5:31000")
        self.assertEqual(connection["timeout"], 30.0)

    def test_connection_settings_fall_back_when_empty(self):
        connection = ue_bridge.settings_from_preset({})
        self.assertEqual(connection["base_url"], ue_bridge.DEFAULT_BASE_URL)

    def test_plugin_dir_points_at_the_receiver(self):
        """アドオンの配置から tools/ue_plugin を導けている。"""
        directory = ue_bridge.default_plugin_dir()
        self.assertTrue(os.path.isdir(directory), directory)
        self.assertTrue(os.path.isfile(os.path.join(directory, "onepochi_rc.py")))

    # --- レポートの扱い ---

    def test_report_path_defaults_next_to_manifest(self):
        path = ue_bridge.default_report_path(os.path.join("C:", "a", "onepochi.json"))
        self.assertEqual(os.path.basename(path), "ue_report.json")
        self.assertEqual(os.path.dirname(path),
                         os.path.dirname(os.path.abspath(
                             os.path.join("C:", "a", "onepochi.json"))))

    def test_missing_report_reads_as_none(self):
        self.assertIsNone(ue_bridge.read_report(
            os.path.join("C:", "no", "such", "ue_report.json")))
        self.assertIsNone(ue_bridge.read_report(""))


class TestSchemaConsistency(unittest.TestCase):
    """スキーマとコードの整合性（ドキュメントとの乖離を機械的に防ぐ）。"""

    def setUp(self):
        import json
        path = os.path.join(PROJECT_ROOT, "schema", "onepochi_manifest.schema.json")
        with open(path, encoding="utf-8") as handle:
            self.schema = json.load(handle)

    def test_schema_version_matches_code(self):
        pattern = self.schema["properties"]["schemaVersion"]["pattern"]
        self.assertRegex(manifest_mod.SCHEMA_VERSION, pattern)
        self.assertIn(manifest_mod.SCHEMA_VERSION, self.schema["$id"])

    def test_schema_description_is_not_stale(self):
        """`description` のバージョン表記が `$id` と食い違っていないか。

        実際に v0.2.0 のまま取り残されていた（T-8 で修正）。
        """
        self.assertIn(manifest_mod.SCHEMA_VERSION, self.schema["description"])

    def test_ruleset_version_matches_docs(self):
        """`RULESET_VERSION` と docs/05 の見出しを一致させる（P-14 の教訓）。

        バージョンをドキュメントとコードの2か所に書くと必ずずれる。
        ずれたらここで落ちるようにしておく。
        """
        path = os.path.join(PROJECT_ROOT, "docs", "05_バリデーション規則.md")
        with open(path, encoding="utf-8") as handle:
            head = handle.read(600)
        self.assertIn("`{0}`".format(manifest_mod.RULESET_VERSION), head,
                      "docs/05 の『ルールセットバージョン』が "
                      "RULESET_VERSION={0} と一致していません".format(
                          manifest_mod.RULESET_VERSION))

    def test_run_id_pattern_matches(self):
        pattern = self.schema["properties"]["runId"]["pattern"]
        self.assertRegex(manifest_mod.make_run_id(), pattern)

    def test_material_pattern_matches_code(self):
        pattern = self.schema["$defs"]["materialName"]["pattern"]
        self.assertEqual(pattern, naming.MATERIAL_RE.pattern)

    def test_asset_name_pattern_matches_code(self):
        pattern = self.schema["$defs"]["ueAssetName"]["pattern"]
        self.assertEqual(pattern, naming.NAME_RE.pattern)

    def test_dcc_enum_excludes_maya(self):
        values = self.schema["$defs"]["generator"]["properties"]["dcc"]["enum"]
        self.assertNotIn("maya", values)
        self.assertIn("blender", values)

    def test_ue_path_pattern_is_compatible(self):
        pattern = self.schema["$defs"]["uePackagePath"]["pattern"]
        compiled = re.compile(pattern)
        self.assertTrue(compiled.match("/Game/_Dev/Props/Crate_01"))
        self.assertFalse(compiled.match("/Game/1Props"))

    def test_socket_name_check_matches_the_schema(self):
        """V-050 の判定とスキーマ（socket.name = ueAssetName）が食い違わない。

        ここが食い違うと「DCC は通すのに後段で落ちる」（D-3 違反）になる。
        """
        ref = self.schema["$defs"]["socket"]["properties"]["name"]["$ref"]
        self.assertEqual(ref, "#/$defs/ueAssetName")
        pattern = re.compile(self.schema["$defs"]["ueAssetName"]["pattern"])
        for name in ("weapon_r", "Lid", "A", "a1_b2",
                     "1_weapon", "weapon r", "weapon-r", "weapon.001", "武器"):
            allowed = bool(pattern.match(name))
            problem = naming.socket_name_problem("SOCKET_" + name)
            self.assertEqual(allowed, problem is None,
                             "SOCKET_{0}: スキーマ={1} 実装={2}".format(
                                 name, allowed, problem))

    def test_root_bone_name_is_not_nullable(self):
        """スキーマが `rootBoneName` に null を許さないことを確かめる。

        実装側（`check_manifest_shape`）がこれを守っているかは
        `TestManifest.test_check_manifest_shape_rejects_null_root_bone` で検証する。
        """
        skeleton = (self.schema["$defs"]["skeletalMeshAsset"]
                    ["properties"]["skeleton"])
        self.assertEqual(skeleton["properties"]["rootBoneName"]["type"], "string")
        self.assertIn("rootBoneName", skeleton["required"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
