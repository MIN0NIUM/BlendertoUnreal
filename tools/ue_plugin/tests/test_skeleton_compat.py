"""既存 Skeleton との互換判定の単体テスト（unreal 不要）。

実行:
    python tools/ue_plugin/tests/test_skeleton_compat.py
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
PROJECT_ROOT = os.path.dirname(os.path.dirname(PLUGIN_ROOT))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

from onepochi_ue import skeleton_compat  # noqa: E402


def _force_utf8_stdout():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_stdout()

#: S-04 のボーン階層（実測）
S04 = [(None, "root"), ("root", "bone_01"), ("bone_01", "bone_02")]


class TestNormalize(unittest.TestCase):
    def test_list_of_lists_from_json(self):
        pairs = skeleton_compat.normalize_pairs(
            [[None, "root"], ["root", "bone_01"]])
        self.assertEqual(pairs, [(None, "root"), ("root", "bone_01")])

    def test_empty_parent_string_becomes_none(self):
        self.assertEqual(skeleton_compat.normalize_pairs([["", "root"]]),
                         [(None, "root")])

    def test_dict_entries_from_bones_array(self):
        pairs = skeleton_compat.normalize_pairs([
            {"name": "root", "parent": None},
            {"name": "bone_01", "parent": "root"},
        ])
        self.assertEqual(pairs, [(None, "root"), ("root", "bone_01")])

    def test_entries_without_name_are_dropped(self):
        self.assertEqual(skeleton_compat.normalize_pairs([[None, ""]]), [])

    def test_none_input(self):
        self.assertEqual(skeleton_compat.normalize_pairs(None), [])


class TestHierarchyFromManifest(unittest.TestCase):
    def test_prefers_bone_hierarchy(self):
        block = {"boneHierarchy": [[None, "root"]],
                 "bones": [{"name": "other", "parent": None}]}
        self.assertEqual(skeleton_compat.hierarchy_from_manifest(block),
                         [(None, "root")])

    def test_falls_back_to_bones(self):
        block = {"bones": [{"name": "root", "parent": None}]}
        self.assertEqual(skeleton_compat.hierarchy_from_manifest(block),
                         [(None, "root")])

    def test_empty_block(self):
        self.assertEqual(skeleton_compat.hierarchy_from_manifest({}), [])
        self.assertEqual(skeleton_compat.hierarchy_from_manifest(None), [])


class TestExactMode(unittest.TestCase):
    def test_identical_is_compatible(self):
        result = skeleton_compat.compare(S04, S04, "exact")
        self.assertTrue(result["compatible"])
        self.assertTrue(result["hashMatches"])
        self.assertEqual(result["missingInExisting"], [])
        self.assertEqual(result["extraInExisting"], [])

    def test_s05_matches_s04(self):
        """S-05 は S-04 と同一階層になるよう直してもらった（docs/14 B-1）。"""
        s05 = list(S04)
        result = skeleton_compat.compare(s05, S04, "exact")
        self.assertTrue(result["compatible"], result["reasons"])

    def test_extra_bone_in_existing_rejected(self):
        existing = S04 + [("bone_02", "bone_03")]
        result = skeleton_compat.compare(S04, existing, "exact")
        self.assertFalse(result["compatible"])
        self.assertEqual(result["extraInExisting"], ["bone_03"])
        self.assertTrue(any("完全一致" in r for r in result["reasons"]))

    def test_missing_bone_rejected(self):
        existing = [(None, "root"), ("root", "bone_01")]
        result = skeleton_compat.compare(S04, existing, "exact")
        self.assertFalse(result["compatible"])
        self.assertEqual(result["missingInExisting"], ["bone_02"])

    def test_different_root_rejected(self):
        """S-05 の修正前の状態（root が無い）を弾けること。"""
        incoming = [(None, "bone_01"), ("bone_01", "bone_02")]
        result = skeleton_compat.compare(incoming, S04, "exact")
        self.assertFalse(result["compatible"])
        self.assertTrue(any("ルートボーン" in r for r in result["reasons"]))

    def test_parent_mismatch_rejected(self):
        existing = [(None, "root"), ("root", "bone_01"), ("root", "bone_02")]
        result = skeleton_compat.compare(S04, existing, "exact")
        self.assertFalse(result["compatible"])
        self.assertEqual(len(result["parentMismatches"]), 1)
        self.assertEqual(result["parentMismatches"][0]["bone"], "bone_02")
        self.assertTrue(any("親が違います" in r for r in result["reasons"]))

    def test_order_difference_rejected(self):
        reordered = [(None, "root"), ("bone_01", "bone_02"), ("root", "bone_01")]
        result = skeleton_compat.compare(S04, reordered, "exact")
        self.assertFalse(result["compatible"])
        self.assertTrue(result["orderDiffers"])
        self.assertTrue(any("並び順" in r for r in result["reasons"]))


class TestSubsetMode(unittest.TestCase):
    def test_subset_allows_extra_bones(self):
        existing = S04 + [("bone_02", "bone_03"), ("bone_03", "bone_04")]
        result = skeleton_compat.compare(S04, existing, "subset")
        self.assertTrue(result["compatible"], result["reasons"])
        self.assertEqual(result["extraInExisting"], ["bone_03", "bone_04"])
        self.assertTrue(any("許容" in r for r in result["reasons"]))

    def test_subset_still_requires_same_parents(self):
        """名前が揃っていても親が違えばバインドが壊れるので許さない。"""
        existing = [(None, "root"), ("root", "bone_01"), ("root", "bone_02"),
                    ("bone_02", "bone_03")]
        result = skeleton_compat.compare(S04, existing, "subset")
        self.assertFalse(result["compatible"])
        self.assertTrue(any("親が違います" in r for r in result["reasons"]))

    def test_subset_rejects_missing_bones(self):
        existing = [(None, "root"), ("root", "bone_01")]
        result = skeleton_compat.compare(S04, existing, "subset")
        self.assertFalse(result["compatible"])

    def test_subset_allows_order_difference(self):
        """並び順は subset では問題にしない。"""
        existing = [(None, "root"), ("root", "bone_01"), ("bone_01", "bone_02"),
                    ("root", "helper")]
        result = skeleton_compat.compare(S04, existing, "subset")
        self.assertTrue(result["compatible"], result["reasons"])


class TestNamesOnlyMode(unittest.TestCase):
    """既存 Skeleton の親子関係が読めない環境（SkeletonModifier 不在）。"""

    def test_identical_names_are_compatible_but_flagged(self):
        names = ["root", "bone_01", "bone_02"]
        result = skeleton_compat.compare_names_only(S04, names, "exact")
        self.assertTrue(result["compatible"], result["reasons"])
        self.assertEqual(result["method"], "namesOnly")
        # 限界を黙らない
        self.assertTrue(any("親子関係は検証できていません" in r
                            for r in result["reasons"]))

    def test_order_difference_rejected_in_exact(self):
        names = ["root", "bone_02", "bone_01"]
        result = skeleton_compat.compare_names_only(S04, names, "exact")
        self.assertFalse(result["compatible"])
        self.assertTrue(any("並び順" in r for r in result["reasons"]))

    def test_missing_bone_rejected(self):
        result = skeleton_compat.compare_names_only(S04, ["root", "bone_01"],
                                                    "exact")
        self.assertFalse(result["compatible"])
        self.assertEqual(result["missingInExisting"], ["bone_02"])

    def test_extra_bone_rejected_in_exact(self):
        names = ["root", "bone_01", "bone_02", "bone_03"]
        result = skeleton_compat.compare_names_only(S04, names, "exact")
        self.assertFalse(result["compatible"])
        self.assertEqual(result["extraInExisting"], ["bone_03"])

    def test_extra_bone_allowed_in_subset(self):
        names = ["root", "bone_01", "bone_02", "bone_03"]
        result = skeleton_compat.compare_names_only(S04, names, "subset")
        self.assertTrue(result["compatible"], result["reasons"])

    def test_empty_existing_rejected(self):
        result = skeleton_compat.compare_names_only(S04, [], "exact")
        self.assertFalse(result["compatible"])
        self.assertTrue(any("読み取れませんでした" in r for r in result["reasons"]))

    def test_unknown_mode_rejected(self):
        result = skeleton_compat.compare_names_only(S04, ["root"], "nope")
        self.assertFalse(result["compatible"])

    def test_unreal_name_objects_are_stringified(self):
        """UE から来る FName は str() が必要。"""
        class FakeName(object):
            def __init__(self, value):
                self.value = value

            def __str__(self):
                return self.value

        names = [FakeName("root"), FakeName("bone_01"), FakeName("bone_02")]
        result = skeleton_compat.compare_names_only(S04, names, "exact")
        self.assertTrue(result["compatible"], result["reasons"])


class TestEdgeCases(unittest.TestCase):
    def test_empty_incoming_rejected(self):
        result = skeleton_compat.compare([], S04, "exact")
        self.assertFalse(result["compatible"])
        self.assertTrue(any("空です" in r for r in result["reasons"]))

    def test_empty_existing_rejected(self):
        result = skeleton_compat.compare(S04, [], "exact")
        self.assertFalse(result["compatible"])
        self.assertTrue(any("読み取れませんでした" in r for r in result["reasons"]))

    def test_unknown_mode_rejected(self):
        result = skeleton_compat.compare(S04, S04, "whatever")
        self.assertFalse(result["compatible"])
        self.assertTrue(any("compatibilityMode" in r for r in result["reasons"]))

    def test_reasons_are_always_present(self):
        for mode in ("exact", "subset"):
            result = skeleton_compat.compare(S04, S04, mode)
            self.assertTrue(result["reasons"])

    def test_hash_is_order_sensitive(self):
        reordered = [(None, "root"), ("bone_01", "bone_02"), ("root", "bone_01")]
        self.assertNotEqual(skeleton_compat.bone_hierarchy_hash(S04),
                            skeleton_compat.bone_hierarchy_hash(reordered))

    def test_hash_is_stable(self):
        self.assertEqual(skeleton_compat.bone_hierarchy_hash(S04),
                         skeleton_compat.bone_hierarchy_hash(list(S04)))


class TestCrossPackageHash(unittest.TestCase):
    """Blender 側と UE 側のハッシュ実装が一致しているか。

    食い違うと「同じ階層なのに不一致」と誤判定する。
    別パッケージに同じアルゴリズムを2つ持っているので、
    **機械的に突き合わせておく**（目視レビューでは腐る）。
    """

    def setUp(self):
        addons = os.path.join(PROJECT_ROOT, "tools", "dcc_blender", "addons")
        if addons not in sys.path:
            sys.path.insert(0, addons)
        try:
            from onepochi.core import manifest as blender_manifest
        except ImportError as error:  # pragma: no cover
            raise unittest.SkipTest(
                "Blender 側のモジュールを読めません: {0}".format(error))
        self.blender_manifest = blender_manifest

    def test_same_hash_for_same_hierarchy(self):
        for hierarchy in (S04,
                          [(None, "root")],
                          [(None, "root"), ("root", "pelvis"),
                           ("pelvis", "spine_01")],
                          []):
            self.assertEqual(
                self.blender_manifest.bone_hierarchy_hash(hierarchy),
                skeleton_compat.bone_hierarchy_hash(hierarchy),
                "階層 {0!r} でハッシュが食い違いました".format(hierarchy))

    def test_none_and_empty_parent_agree(self):
        self.assertEqual(
            self.blender_manifest.bone_hierarchy_hash([(None, "root")]),
            skeleton_compat.bone_hierarchy_hash([("", "root")]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
