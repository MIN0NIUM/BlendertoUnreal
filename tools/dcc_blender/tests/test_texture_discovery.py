"""テクスチャ発見規則の単体テスト（R-4 / bpy 不要）。

実行:
    python tools/dcc_blender/tests/test_texture_discovery.py
    python -m unittest discover -s tools/dcc_blender/tests

検証の柱:
    1. 3 系統の綴り（スロット名から M_ を外す / スロット名そのまま / アセット名）
    2. 大文字小文字を区別しない
    3. 拡張子の優先順（.png -> .tga -> .jpg -> ...）
    4. 重複は黙って選ばない（V-111 で採用・不採用を明記する）
    5. 未知サフィックス・規約違反の名前は取り込まない（V-092 / V-112）
    6. **再帰しない**（サブフォルダのファイル名は渡ってこない）
    7. マルチスロット（S-03 の M_Slot_A / B / C）で取り違えない
    8. サフィックス表が docs/04 §3 と一致する（P-28: 同じ値を 2 か所に書かない）
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
from onepochi.core import texture_discovery as td  # noqa: E402

#: 仕様の出所。ここの表と実装の `SUFFIXES` を突き合わせる（P-28）
NAMING_DOC = os.path.join(PROJECT_ROOT, "docs", "04_命名規約.md")


def _force_utf8_stdout():
    """Windows の既定コンソール（cp932）でも日本語を出せるようにする（P-9）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_stdout()


def read_doc_suffixes():
    """docs/04 §3 の「テクスチャサフィックス」の表からサフィックスを読む。"""
    with open(NAMING_DOC, encoding="utf-8") as handle:
        text = handle.read()
    start = text.find("## 3. テクスチャサフィックス")
    if start < 0:
        raise AssertionError("docs/04_命名規約.md の §3 が見つかりません")
    end = text.find("\n## ", start + 1)
    section = text[start:end if end > 0 else len(text)]
    return [m.group(1) for m in re.finditer(r"^\|\s*`(_[A-Za-z]+)`\s*\|",
                                            section, re.MULTILINE)]


def rule_ids(issues):
    return [i.rule_id for i in issues]


class TestSuffixTable(unittest.TestCase):
    """P-28: 仕様の表を実装にコピーしたら、テストで突き合わせる。"""

    def test_suffixes_match_the_naming_doc(self):
        doc = read_doc_suffixes()
        # 表のパースが 0 件でも「一致」になってしまうので件数を固定する
        self.assertEqual(len(doc), 9, "docs/04 §3 の表が 9 行ではありません")
        self.assertEqual(list(td.SUFFIXES), doc)

    def test_extensions_are_unique_and_lowercase(self):
        self.assertEqual(len(set(td.EXTENSIONS)), len(td.EXTENSIONS))
        for extension in td.EXTENSIONS:
            self.assertEqual(extension, extension.lower())
            self.assertTrue(extension.startswith("."))

    def test_lossy_extensions_are_part_of_extensions(self):
        for extension in td.LOSSY_EXTENSIONS:
            self.assertIn(extension, td.EXTENSIONS)


class TestParseTextureFile(unittest.TestCase):
    def test_splits_stem_suffix_extension(self):
        record = td.parse_texture_file("T_M_Crate_Body_BC.jpg")
        self.assertEqual(record.stem, "T_M_Crate_Body")
        self.assertEqual(record.suffix, "_BC")
        self.assertEqual(record.extension, ".jpg")

    def test_suffix_is_case_insensitive_but_normalized(self):
        record = td.parse_texture_file("T_Crate_Body_bc.PNG")
        self.assertEqual(record.suffix, "_BC")
        self.assertEqual(record.extension, ".png")

    def test_msk_is_not_read_as_m(self):
        record = td.parse_texture_file("T_Crate_Body_MSK.png")
        self.assertEqual(record.suffix, "_MSK")
        self.assertEqual(record.stem, "T_Crate_Body")

    def test_unknown_extension_is_not_a_texture(self):
        self.assertIsNone(td.parse_texture_file("T_Crate_Body_BC.psd"))
        self.assertIsNone(td.parse_texture_file("readme.txt"))

    def test_unknown_suffix_keeps_the_record_but_has_no_suffix(self):
        record = td.parse_texture_file("T_Crate_Body_XYZ.png")
        self.assertIsNotNone(record)
        self.assertIsNone(record.suffix)

    def test_no_underscore_at_all(self):
        record = td.parse_texture_file("texture.png")
        self.assertIsNotNone(record)
        self.assertIsNone(record.suffix)


class TestCandidateStems(unittest.TestCase):
    def test_three_spellings_in_priority_order(self):
        self.assertEqual(
            td.candidate_stems("M_Crate_Body", "Crate_01", single_slot=True),
            ["T_Crate_Body", "T_M_Crate_Body", "T_Crate_01"])

    def test_asset_stem_is_only_for_single_slot(self):
        self.assertEqual(
            td.candidate_stems("M_Crate_Body", "Crate_01", single_slot=False),
            ["T_Crate_Body", "T_M_Crate_Body"])

    def test_slot_without_m_prefix_is_not_duplicated(self):
        # V-003c で弾かれる名前だが、発見側が壊れないことを確かめる
        self.assertEqual(
            td.candidate_stems("Crate_Body", None, single_slot=False),
            ["T_Crate_Body"])

    def test_asset_stem_equal_to_slot_stem_is_deduped(self):
        stems = td.candidate_stems("M_Crate_01", "Crate_01", single_slot=True)
        self.assertEqual(stems, ["T_Crate_01", "T_M_Crate_01"])


class TestDiscoverSpellings(unittest.TestCase):
    def test_pattern_1_slot_without_m(self):
        textures, issues = td.discover(
            ["M_Crate_Body"], "Crate_01",
            ["T_Crate_Body_BC.png", "T_Crate_Body_N.png"])
        self.assertEqual(textures["M_Crate_Body"],
                         {"_BC": "T_Crate_Body_BC.png",
                          "_N": "T_Crate_Body_N.png"})
        self.assertEqual(rule_ids(issues), [])

    def test_pattern_2_slot_as_is(self):
        """S-11 の実物の綴り（`T_M_Crate_Body_BC.jpg`）。"""
        textures, _ = td.discover(
            ["M_Crate_Body"], "Crate_01",
            ["T_M_Crate_Body_BC.jpg", "T_M_Crate_Body_N.jpg",
             "T_M_Crate_Body_ORM.jpg"])
        self.assertEqual(textures["M_Crate_Body"],
                         {"_BC": "T_M_Crate_Body_BC.jpg",
                          "_N": "T_M_Crate_Body_N.jpg",
                          "_ORM": "T_M_Crate_Body_ORM.jpg"})

    def test_pattern_3_asset_name_single_slot(self):
        textures, _ = td.discover(
            ["M_Crate_Body"], "Crate_01", ["T_Crate_01_BC.png"])
        self.assertEqual(textures["M_Crate_Body"], {"_BC": "T_Crate_01_BC.png"})

    def test_pattern_3_is_not_used_for_multi_slot(self):
        textures, issues = td.discover(
            ["M_Slot_A", "M_Slot_B"], "Crate_01", ["T_Crate_01_BC.png"])
        self.assertEqual(textures, {})
        self.assertEqual(sorted(rule_ids(issues)), ["V-110", "V-110"])

    def test_pattern_1_wins_over_pattern_2(self):
        textures, issues = td.discover(
            ["M_Crate_Body"], "Crate_01",
            ["T_M_Crate_Body_BC.png", "T_Crate_Body_BC.png"])
        self.assertEqual(textures["M_Crate_Body"]["_BC"], "T_Crate_Body_BC.png")
        self.assertEqual(rule_ids(issues), ["V-111"])

    def test_pattern_2_wins_over_pattern_3(self):
        textures, _ = td.discover(
            ["M_Crate_Body"], "Crate_01",
            ["T_Crate_01_BC.png", "T_M_Crate_Body_BC.png"])
        self.assertEqual(textures["M_Crate_Body"]["_BC"],
                         "T_M_Crate_Body_BC.png")

    def test_suffix_keys_follow_the_table_order(self):
        textures, _ = td.discover(
            ["M_Crate_Body"], "Crate_01",
            ["T_Crate_Body_ORM.png", "T_Crate_Body_N.png",
             "T_Crate_Body_BC.png"])
        self.assertEqual(list(textures["M_Crate_Body"].keys()),
                         ["_BC", "_N", "_ORM"])


class TestDiscoverCaseInsensitive(unittest.TestCase):
    def test_file_name_case_does_not_matter(self):
        textures, _ = td.discover(
            ["M_Crate_Body"], "Crate_01", ["t_crate_body_bc.PNG"])
        self.assertEqual(textures["M_Crate_Body"], {"_BC": "t_crate_body_bc.PNG"})

    def test_original_file_name_is_preserved(self):
        textures, _ = td.discover(
            ["M_Crate_Body"], "Crate_01", ["T_CRATE_BODY_N.Tga"])
        self.assertEqual(textures["M_Crate_Body"]["_N"], "T_CRATE_BODY_N.Tga")


class TestDiscoverExtensionPriority(unittest.TestCase):
    def test_png_beats_jpg(self):
        textures, issues = td.discover(
            ["M_Crate_Body"], "Crate_01",
            ["T_Crate_Body_BC.jpg", "T_Crate_Body_BC.png"])
        self.assertEqual(textures["M_Crate_Body"]["_BC"], "T_Crate_Body_BC.png")
        self.assertEqual(rule_ids(issues), ["V-111"])

    def test_full_priority_order(self):
        for better, worse in zip(td.EXTENSIONS, td.EXTENSIONS[1:]):
            textures, _ = td.discover(
                ["M_X"], "X",
                ["T_X_BC" + worse, "T_X_BC" + better])
            self.assertEqual(textures["M_X"]["_BC"], "T_X_BC" + better,
                             "{0} が {1} に負けました".format(better, worse))

    def test_stem_priority_beats_extension_priority(self):
        """語幹の優先順が先。`T_Crate_Body_BC.jpg` が `T_M_..._BC.png` に勝つ。"""
        textures, _ = td.discover(
            ["M_Crate_Body"], "Crate_01",
            ["T_M_Crate_Body_BC.png", "T_Crate_Body_BC.jpg"])
        self.assertEqual(textures["M_Crate_Body"]["_BC"], "T_Crate_Body_BC.jpg")


class TestDiscoverWarnings(unittest.TestCase):
    def test_v110_when_nothing_is_found(self):
        textures, issues = td.discover(
            ["M_Crate_Body"], "Crate_01", ["T_Other_BC.png"])
        self.assertEqual(textures, {})
        found = [i for i in issues if i.rule_id == "V-110"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].severity, issues_mod.WARNING)
        self.assertEqual(found[0].target, "M_Crate_Body")

    def test_v111_names_both_the_chosen_and_the_rejected(self):
        _, issues = td.discover(
            ["M_Crate_Body"], "Crate_01",
            ["T_Crate_Body_BC.png", "T_Crate_Body_BC.tga",
             "T_Crate_Body_BC.jpg"])
        found = [i for i in issues if i.rule_id == "V-111"]
        self.assertEqual(len(found), 1)
        message = found[0].message
        self.assertIn("T_Crate_Body_BC.png", message)   # 採用
        self.assertIn("T_Crate_Body_BC.tga", message)   # 不採用
        self.assertIn("T_Crate_Body_BC.jpg", message)   # 不採用

    def test_v112_for_missing_prefix(self):
        _, issues = td.discover(["M_Crate_Body"], "Crate_01",
                                ["Crate_Body_BC.png"])
        self.assertIn("V-112", rule_ids(issues))

    def test_v112_for_illegal_characters(self):
        _, issues = td.discover(["M_Crate_Body"], "Crate_01",
                                ["T_木箱_BC.png", "T Crate Body_BC.png"])
        self.assertEqual(rule_ids(issues).count("V-112"), 2)

    def test_v092_for_unknown_suffix(self):
        _, issues = td.discover(["M_Crate_Body"], "Crate_01",
                                ["T_Crate_Body_XYZ.png"])
        self.assertIn("V-092", rule_ids(issues))

    def test_non_texture_files_are_silent(self):
        """`.psd` などの作業データには何も言わない（取り込み対象ではない）。"""
        _, issues = td.discover(
            ["M_Crate_Body"], "Crate_01",
            ["T_Crate_Body_BC.png", "source.psd", "notes.txt"])
        self.assertEqual(rule_ids(issues), [])

    def test_v113_for_lossy_normal_map(self):
        _, issues = td.discover(["M_Crate_Body"], "Crate_01",
                                ["T_Crate_Body_N.jpg"])
        found = [i for i in issues if i.rule_id == "V-113"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].severity, issues_mod.WARNING)

    def test_no_v113_for_lossless_normal_map(self):
        _, issues = td.discover(["M_Crate_Body"], "Crate_01",
                                ["T_Crate_Body_N.png"])
        self.assertNotIn("V-113", rule_ids(issues))

    def test_no_v113_for_lossy_base_color(self):
        """非可逆が問題になるのは法線だけ。BaseColor の JPG は警告しない。"""
        _, issues = td.discover(["M_Crate_Body"], "Crate_01",
                                ["T_Crate_Body_BC.jpg"])
        self.assertEqual(rule_ids(issues), [])

    def test_nothing_is_an_error(self):
        """V-110〜V-113 は**書き出しを止めない**（Q-06）。"""
        _, issues = td.discover(
            ["M_A", "M_B"], "Crate_01",
            ["bad name.png", "T_X_ZZZ.png", "T_A_N.jpg"])
        self.assertFalse(issues.has_error())


class TestDiscoverMultiSlot(unittest.TestCase):
    """S-03 の `M_Slot_A` / `M_Slot_B` / `M_Slot_C` で取り違えないこと。"""

    SLOTS = ["M_Slot_A", "M_Slot_B", "M_Slot_C"]
    FILES = [
        "T_Slot_A_BC.png", "T_Slot_A_N.png",
        "T_M_Slot_B_BC.png",
        "T_Slot_C_ORM.png",
    ]

    def test_each_slot_gets_only_its_own(self):
        textures, _ = td.discover(self.SLOTS, "Crate_01", self.FILES)
        self.assertEqual(textures["M_Slot_A"],
                         {"_BC": "T_Slot_A_BC.png", "_N": "T_Slot_A_N.png"})
        self.assertEqual(textures["M_Slot_B"], {"_BC": "T_M_Slot_B_BC.png"})
        self.assertEqual(textures["M_Slot_C"], {"_ORM": "T_Slot_C_ORM.png"})

    def test_no_warning_when_every_slot_has_something(self):
        _, issues = td.discover(self.SLOTS, "Crate_01", self.FILES)
        self.assertEqual(rule_ids(issues), [])

    def test_v110_only_for_the_empty_slot(self):
        _, issues = td.discover(self.SLOTS, "Crate_01",
                                ["T_Slot_A_BC.png", "T_Slot_B_BC.png"])
        found = [i for i in issues if i.rule_id == "V-110"]
        self.assertEqual([i.target for i in found], ["M_Slot_C"])


class TestDiscoverEdgeCases(unittest.TestCase):
    def test_empty_folder(self):
        textures, issues = td.discover(["M_Crate_Body"], "Crate_01", [])
        self.assertEqual(textures, {})
        self.assertEqual(rule_ids(issues), ["V-110"])

    def test_no_slots(self):
        textures, issues = td.discover([], "Crate_01", ["T_Crate_01_BC.png"])
        self.assertEqual(textures, {})
        self.assertEqual(rule_ids(issues), [])

    def test_none_arguments_do_not_crash(self):
        textures, issues = td.discover(None, None, None)
        self.assertEqual(textures, {})
        self.assertEqual(len(issues), 0)


class TestFileSystemHelpers(unittest.TestCase):
    """`list_texture_files` は**直下だけ**を見る（再帰しない）。"""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.mkdtemp(prefix="onepochi_td_")
        os.makedirs(os.path.join(self._tmp, "megascan"))
        for name in ("T_Crate_Body_BC.png", "T_Crate_Body_N.png", "notes.txt"):
            with open(os.path.join(self._tmp, name), "w") as handle:
                handle.write("x")
        # サブフォルダ側（これが拾われてはいけない）
        for name in ("T_Rock_BC.png", "T_Rock_N.png"):
            with open(os.path.join(self._tmp, "megascan", name), "w") as handle:
                handle.write("x")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_lists_only_files_directly_inside(self):
        names = td.list_texture_files(self._tmp)
        self.assertEqual(names,
                         ["T_Crate_Body_BC.png", "T_Crate_Body_N.png",
                          "notes.txt"])
        self.assertNotIn("megascan", names)

    def test_does_not_recurse(self):
        textures, _ = td.discover(["M_Crate_Body"], "Crate_01",
                                  td.list_texture_files(self._tmp))
        self.assertEqual(textures["M_Crate_Body"],
                         {"_BC": "T_Crate_Body_BC.png",
                          "_N": "T_Crate_Body_N.png"})

    def test_missing_folder_is_not_an_error(self):
        self.assertEqual(
            td.list_texture_files(os.path.join(self._tmp, "nope")), [])
        self.assertEqual(td.list_texture_files(None), [])

    def test_texture_folder_path(self):
        self.assertEqual(
            td.texture_folder_path("C:/assets/Crate_01", "textures"),
            os.path.join("C:/assets/Crate_01", "textures"))
        self.assertIsNone(td.texture_folder_path(None, "textures"))
        self.assertIsNone(td.texture_folder_path("C:/assets/Crate_01", None))

    def test_has_texture_files(self):
        self.assertTrue(td.has_texture_files(["T_X_BC.png"]))
        self.assertTrue(td.has_texture_files(["whatever.png"]))
        self.assertFalse(td.has_texture_files(["notes.txt", "source.psd"]))
        self.assertFalse(td.has_texture_files([]))
        self.assertFalse(td.has_texture_files(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
