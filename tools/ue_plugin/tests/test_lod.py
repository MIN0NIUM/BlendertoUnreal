"""LOD 適用の純粋ロジックの単体テスト（unreal 不要 / T-5）。

`onepochi_ue.lod` は `unreal` を**任意の依存**として扱う（無ければ適用部だけが
動かない）。正規化・計画・比較はここで機械的に検証する。

実行:
    python tools/ue_plugin/tests/test_lod.py
"""

import io
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
PROJECT_ROOT = os.path.dirname(os.path.dirname(PLUGIN_ROOT))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

from onepochi_ue import lod  # noqa: E402


def _force_utf8_stdout():
    """Windows の既定コンソール（cp932）でも日本語を出せるようにする（docs/99 P-9）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_stdout()

SCHEMA_PATH = os.path.join(PROJECT_ROOT, "schema", "onepochi_manifest.schema.json")


def _schema_lod():
    """JSON Schema の `staticMesh.lod` ノードを返す（二重管理の検出に使う）。"""
    with io.open(SCHEMA_PATH, encoding="utf-8") as handle:
        schema = json.load(handle)
    return (schema["$defs"]["staticMeshAsset"]["properties"]["staticMesh"]
            ["properties"]["lod"])


def _asset(lod_block, stats=None):
    asset = {"assetType": "StaticMesh", "staticMesh": {"lod": lod_block}}
    if stats is not None:
        asset["stats"] = stats
    return asset


def _levels(*specs):
    """(level, sourceFile, screenSize) から levels[] を作る。"""
    out = []
    for level, source, screen in specs:
        entry = {"level": level}
        if source is not None:
            entry["sourceFile"] = source
        if screen is not None:
            entry["screenSize"] = screen
        out.append(entry)
    return out


class TestModeNormalization(unittest.TestCase):
    def test_valid_modes_match_the_schema_enum(self):
        """`VALID_MODES` が JSON Schema の enum と一致すること（二重管理の検出）。"""
        node = _schema_lod()
        self.assertEqual(sorted(lod.VALID_MODES),
                         sorted(node["properties"]["mode"]["enum"]))

    def test_engine_limit_matches_the_schema(self):
        """`MAX_STATIC_MESH_LODS` が schema の `autoLodCount.maximum` と一致すること。"""
        node = _schema_lod()
        self.assertEqual(lod.MAX_STATIC_MESH_LODS,
                         node["properties"]["autoLodCount"]["maximum"])

    def test_unknown_mode_warns_instead_of_raising(self):
        mode, notes = lod.normalize_mode({"mode": "ちょっと速いやつ"})
        self.assertEqual(mode, "none")
        self.assertTrue(notes)

    def test_missing_mode_warns(self):
        mode, notes = lod.normalize_mode({"levels": []})
        self.assertEqual(mode, "none")
        self.assertTrue(notes)

    def test_empty_block_is_none_without_noise(self):
        self.assertEqual(lod.normalize_mode(None), ("none", []))
        self.assertEqual(lod.normalize_mode({}), ("none", []))


class TestNormalizeLevels(unittest.TestCase):
    def test_sorts_by_level_and_counts_slots(self):
        block = {"levels": _levels((2, "b.fbx", None), (0, "a.fbx", None),
                                   (1, "c.fbx", None))}
        result = lod.normalize_levels(block)
        self.assertEqual([e["level"] for e in result["levels"]], [0, 1, 2])
        self.assertEqual(result["declaredCount"], 3)
        self.assertTrue(result["hasLod0"])
        self.assertEqual(result["problems"], [])

    def test_missing_lod0_entry_is_a_note_not_an_error(self):
        """Blender 側は LOD0 を本体 FBX に書くため、levels[] に無いことがある。"""
        block = {"levels": _levels((1, "a.fbx", None), (2, "b.fbx", None))}
        result = lod.normalize_levels(block)
        self.assertFalse(result["hasLod0"])
        self.assertEqual(result["declaredCount"], 3)
        self.assertEqual(result["problems"], [])
        self.assertTrue(result["notes"])

    def test_gap_is_a_problem(self):
        block = {"levels": _levels((0, "a.fbx", None), (2, "b.fbx", None))}
        result = lod.normalize_levels(block)
        self.assertEqual(result["missingLevels"], [1])
        self.assertTrue(result["problems"])

    def test_duplicate_level_is_a_problem(self):
        block = {"levels": _levels((1, "a.fbx", None), (1, "b.fbx", None))}
        self.assertTrue(lod.normalize_levels(block)["problems"])

    def test_level_one_without_source_file_is_a_problem(self):
        block = {"levels": _levels((1, None, None))}
        problems = lod.normalize_levels(block)["problems"]
        self.assertTrue(any("sourceFile" in p for p in problems))

    def test_empty_levels_still_counts_one_slot(self):
        """本体 FBX が LOD0 なので、スロットは必ず 1 段以上ある。"""
        self.assertEqual(lod.normalize_levels({"levels": []})["declaredCount"], 1)
        self.assertEqual(lod.normalize_levels({})["declaredCount"], 1)

    def test_over_the_engine_limit_is_a_problem(self):
        specs = [(n, "lod{0}.fbx".format(n), None)
                 for n in range(lod.MAX_STATIC_MESH_LODS + 1)]
        problems = lod.normalize_levels({"levels": _levels(*specs)})["problems"]
        self.assertTrue(any("上限" in p for p in problems))

    def test_garbage_does_not_raise(self):
        for block in ({"levels": "manual"}, {"levels": [None, 3]},
                      {"levels": [{"level": "0"}]}, {"levels": [{"level": -1}]},
                      {"levels": [{"level": True}]}):
            result = lod.normalize_levels(block)
            self.assertIsInstance(result["problems"], list)


class TestScreenSizes(unittest.TestCase):
    def test_all_specified_returns_the_array(self):
        levels = lod.normalize_levels(
            {"levels": _levels((0, "a.fbx", 1.0), (1, "b.fbx", 0.5),
                               (2, "c.fbx", 0.25))})["levels"]
        sizes, notes = lod.plan_screen_sizes(levels, 3)
        self.assertEqual(sizes, [1.0, 0.5, 0.25])
        self.assertEqual(notes, [])

    def test_partially_specified_returns_none(self):
        """一部だけ指定されたときは**設定しない**（UE の自動計算に任せる）。"""
        levels = lod.normalize_levels(
            {"levels": _levels((0, "a.fbx", 1.0), (1, "b.fbx", None),
                               (2, "c.fbx", 0.25))})["levels"]
        sizes, notes = lod.plan_screen_sizes(levels, 3)
        self.assertIsNone(sizes)
        self.assertTrue(notes)

    def test_none_specified_returns_none_with_a_note(self):
        levels = lod.normalize_levels(
            {"levels": _levels((0, "a.fbx", None), (1, "b.fbx", None))})["levels"]
        sizes, notes = lod.plan_screen_sizes(levels, 2)
        self.assertIsNone(sizes)
        self.assertTrue(notes)

    def test_non_numeric_screen_size_returns_none(self):
        levels = [{"level": 0, "screenSize": "おおきい"},
                  {"level": 1, "screenSize": 0.5}]
        sizes, notes = lod.plan_screen_sizes(levels, 2)
        self.assertIsNone(sizes)
        self.assertTrue(notes)

    def test_zero_slots_returns_none(self):
        self.assertEqual(lod.plan_screen_sizes([], 0), (None, []))


class TestAutoReduction(unittest.TestCase):
    def test_halves_each_level(self):
        percentages, notes = lod.auto_reduction_percentages(3)
        self.assertEqual(percentages, [1.0, 0.5, 0.25])
        self.assertEqual(notes, [])

    def test_clamps_to_engine_limit(self):
        percentages, notes = lod.auto_reduction_percentages(99)
        self.assertEqual(len(percentages), lod.MAX_STATIC_MESH_LODS)
        self.assertTrue(notes)

    def test_floor_on_percent_triangles(self):
        percentages, _ = lod.auto_reduction_percentages(lod.MAX_STATIC_MESH_LODS)
        self.assertTrue(all(p >= lod.AUTO_MIN_PERCENT_TRIANGLES for p in percentages))

    def test_invalid_counts_fail_without_raising(self):
        for value in (None, 0, -1, "3", True):
            percentages, notes = lod.auto_reduction_percentages(value)
            self.assertEqual(percentages, [])
            self.assertTrue(notes)


class TestDeclaredCount(unittest.TestCase):
    def test_manual_uses_levels(self):
        asset = _asset({"mode": "manual",
                        "levels": _levels((0, "a.fbx", None), (1, "b.fbx", None),
                                          (2, "c.fbx", None))})
        self.assertEqual(lod.declared_lod_count(asset), 3)

    def test_manual_without_lod0_entry_still_counts_three(self):
        asset = _asset({"mode": "manual",
                        "levels": _levels((1, "b.fbx", None), (2, "c.fbx", None))})
        self.assertEqual(lod.declared_lod_count(asset), 3)

    def test_auto_uses_auto_lod_count(self):
        self.assertEqual(
            lod.declared_lod_count(_asset({"mode": "auto", "autoLodCount": 4})), 4)

    def test_auto_without_count_is_unknown(self):
        """推測しない。分からないものは None。"""
        self.assertIsNone(lod.declared_lod_count(_asset({"mode": "auto"})))

    def test_none_declares_one(self):
        self.assertEqual(lod.declared_lod_count(_asset({"mode": "none"})), 1)
        self.assertEqual(lod.declared_lod_count({"assetType": "StaticMesh"}), 1)


class TestDeclaredTriangleCounts(unittest.TestCase):
    def test_absent_when_not_declared(self):
        """宣言が無ければ None（でっち上げない）。v0.3.0 以前のマニフェストもこれに当たる。"""
        asset = _asset({"mode": "manual",
                        "levels": _levels((0, "a.fbx", None), (1, "b.fbx", None))},
                       stats={"triangleCount": 1280})
        self.assertIsNone(lod.declared_lod_triangle_counts(asset))

    def test_picked_up_from_stats_when_present(self):
        asset = _asset({"mode": "manual"},
                       stats={"lodTriangleCounts": [1280, 320, 80]})
        self.assertEqual(lod.declared_lod_triangle_counts(asset), [1280, 320, 80])

    def test_picked_up_from_levels_when_present(self):
        block = {"mode": "manual", "levels": [
            {"level": 0, "sourceFile": "a.fbx", "triangleCount": 1280},
            {"level": 1, "sourceFile": "b.fbx", "triangleCount": 320},
        ]}
        self.assertEqual(lod.declared_lod_triangle_counts(_asset(block)),
                         [1280, 320])

    def test_lod0_filled_from_stats(self):
        block = {"mode": "manual", "levels": [
            {"level": 1, "sourceFile": "b.fbx", "triangleCount": 320},
        ]}
        asset = _asset(block, stats={"triangleCount": 1280})
        self.assertEqual(lod.declared_lod_triangle_counts(asset), [1280, 320])

    def test_partial_declaration_is_unusable(self):
        block = {"mode": "manual", "levels": [
            {"level": 0, "sourceFile": "a.fbx", "triangleCount": 1280},
            {"level": 1, "sourceFile": "b.fbx"},
        ]}
        self.assertIsNone(lod.declared_lod_triangle_counts(_asset(block)))


class TestCompare(unittest.TestCase):
    def test_lod_count_matches(self):
        asset = _asset({"mode": "manual",
                        "levels": _levels((0, "a.fbx", None), (1, "b.fbx", None),
                                          (2, "c.fbx", None))})
        comparison = lod.compare_lod(asset, {"lodCount": 3,
                                             "lodTriangleCounts": [1280, 320, 80]})
        self.assertTrue(comparison["lodCount"]["matches"])
        self.assertEqual(comparison["lodCount"]["declared"], 3)
        self.assertTrue(comparison["lodTriangleCountsDescending"]["matches"])

    def test_lod_count_mismatch_is_reported(self):
        asset = _asset({"mode": "manual",
                        "levels": _levels((0, "a.fbx", None), (1, "b.fbx", None),
                                          (2, "c.fbx", None))})
        comparison = lod.compare_lod(asset, {"lodCount": 1})
        self.assertFalse(comparison["lodCount"]["matches"])

    def test_swapped_lods_are_caught_by_the_descending_rule(self):
        """LOD1 と LOD2 が入れ替わっていたら落ちること（S-07 原本の実際の不具合）。"""
        asset = _asset({"mode": "manual",
                        "levels": _levels((0, "a.fbx", None), (1, "b.fbx", None),
                                          (2, "c.fbx", None))})
        comparison = lod.compare_lod(asset, {"lodCount": 3,
                                             "lodTriangleCounts": [1280, 80, 320]})
        self.assertFalse(comparison["lodTriangleCountsDescending"]["matches"])

    def test_duplicate_lod_geometry_is_caught(self):
        """同じ FBX が 2 段に入ってしまった場合（減っていない）。"""
        asset = _asset({"mode": "manual",
                        "levels": _levels((0, "a.fbx", None), (1, "b.fbx", None))})
        comparison = lod.compare_lod(asset, {"lodCount": 2,
                                             "lodTriangleCounts": [1280, 1280]})
        self.assertFalse(comparison["lodTriangleCountsDescending"]["matches"])

    def test_single_lod_has_no_descending_item(self):
        comparison = lod.compare_lod(_asset({"mode": "none"}),
                                     {"lodCount": 1, "lodTriangleCounts": [1280]})
        self.assertNotIn("lodTriangleCountsDescending", comparison)
        self.assertTrue(comparison["lodCount"]["matches"])

    def test_missing_measurement_makes_no_item(self):
        self.assertEqual(lod.compare_lod(_asset({"mode": "none"}), {}), {})

    def test_declared_triangle_counts_compared_when_available(self):
        asset = _asset({"mode": "manual"},
                       stats={"lodTriangleCounts": [1280, 320, 80]})
        comparison = lod.compare_lod(asset, {"lodCount": 3,
                                             "lodTriangleCounts": [1280, 320, 80]})
        self.assertTrue(comparison["lodTriangleCounts"]["matches"])

        bad = lod.compare_lod(asset, {"lodCount": 3,
                                      "lodTriangleCounts": [1280, 320, 79]})
        self.assertFalse(bad["lodTriangleCounts"]["matches"])

    def test_levels_triangle_count_is_compared_strictly(self):
        """schema v0.3.1 の `levels[].triangleCount` で厳密比較になる（R-6 / M-5）。"""
        block = {"mode": "manual", "levels": [
            {"level": 0, "sourceFile": "a.fbx", "triangleCount": 1280},
            {"level": 1, "sourceFile": "b.fbx", "triangleCount": 320},
            {"level": 2, "sourceFile": "c.fbx", "triangleCount": 80},
        ]}
        measured = {"lodCount": 3, "lodTriangleCounts": [1280, 320, 80]}
        comparison = lod.compare_lod(_asset(block), measured)
        self.assertTrue(comparison["lodTriangleCounts"]["matches"])
        self.assertEqual(comparison["lodTriangleCounts"]["declared"],
                         [1280, 320, 80])

        # 1 枚でも違えば落ちる（降順チェックだけでは通ってしまう値）
        off = lod.compare_lod(_asset(block),
                              {"lodCount": 3,
                               "lodTriangleCounts": [1280, 318, 80]})
        self.assertFalse(off["lodTriangleCounts"]["matches"])
        self.assertTrue(off["lodTriangleCountsDescending"]["matches"])

    def test_v030_manifest_falls_back_to_descending(self):
        """宣言が無い（v0.3.0 以前）なら厳密比較の項目は作らず降順チェックだけ（後方互換）。"""
        block = {"mode": "manual",
                 "levels": _levels((0, "a.fbx", None), (1, "b.fbx", None))}
        comparison = lod.compare_lod(_asset(block),
                                     {"lodCount": 2,
                                      "lodTriangleCounts": [1280, 320]})
        self.assertNotIn("lodTriangleCounts", comparison)
        self.assertTrue(comparison["lodTriangleCountsDescending"]["matches"])


class TestResolveSource(unittest.TestCase):
    def test_relative_to_the_manifest(self):
        manifest = os.path.join(PROJECT_ROOT, "samples", "blender", "x",
                                "fbx", "onepochi.json")
        resolved = lod.resolve_level_source(manifest, "SM_Crate_01_LOD1.fbx")
        self.assertEqual(os.path.normcase(resolved),
                         os.path.normcase(os.path.join(
                             os.path.dirname(manifest), "SM_Crate_01_LOD1.fbx")))

    def test_subfolder(self):
        manifest = os.path.join(PROJECT_ROOT, "fbx", "onepochi.json")
        resolved = lod.resolve_level_source(manifest, "lod/SM_Crate_01_LOD1.fbx")
        self.assertTrue(resolved.endswith(os.path.join("lod", "SM_Crate_01_LOD1.fbx")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
