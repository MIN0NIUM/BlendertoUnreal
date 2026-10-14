"""テクスチャ取り込みの純粋ロジックの単体テスト（unreal 不要 / P5-3・P5-4）。

`onepochi_ue.textures` は `unreal` を**任意の依存**として扱う（無ければ UE 依存部だけが
動かない）。サフィックス表・命名・パス解決・計画づくりはここで機械的に検証する。

**この中で一番大事なのは「表の二重管理が壊れたら落ちる」テスト**:

* `textures.TEXTURE_SETTINGS`（実装）
* `docs/02_仕様書.md` §7.3 の表（**仕様の出所**）
* `tools/presets/project_default.json` の `material.textureParameterMap`（既定プリセット）

の3つが**同じサフィックス集合**を持ち、sRGB / 圧縮設定が一致することを確かめる。
片方だけ直したら落ちる（docs/99 P-14 と同じ趣旨: 定数は実装 / 仕様から導出する）。

実行:
    python tools/ue_plugin/tests/test_textures.py
"""

import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
PROJECT_ROOT = os.path.dirname(os.path.dirname(PLUGIN_ROOT))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

from onepochi_ue import manifest_io, textures  # noqa: E402


def _force_utf8_stdout():
    """Windows の既定コンソール（cp932）でも日本語を出せるようにする（docs/99 P-9）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_stdout()

PRESET_PATH = os.path.join(PROJECT_ROOT, "tools", "presets", "project_default.json")
SPEC_PATH = os.path.join(PROJECT_ROOT, "docs", "02_仕様書.md")

#: docs/02 §7.3 の「圧縮設定」列のラベル -> `unreal.TextureCompressionSettings` の名前
_COMPRESSION_BY_LABEL = {
    "Default": "TC_DEFAULT",
    "Normalmap": "TC_NORMALMAP",
    "Masks": "TC_MASKS",
}


def _preset_parameter_map():
    """既定プリセットの `material.textureParameterMap` を読む。"""
    with io.open(PRESET_PATH, encoding="utf-8") as handle:
        preset = json.load(handle)
    return preset["material"]["textureParameterMap"]


def _spec_table():
    """docs/02 §7.3 の表を読む（**仕様の出所**）。

    戻り値: `{"_BC": {"parameter": "BaseColor", "srgb": True,
                      "compression": "TC_DEFAULT"}, ...}`
    """
    with io.open(SPEC_PATH, encoding="utf-8") as handle:
        text = handle.read()
    start = text.find("### 7.3")
    end = text.find("### 7.4", start)
    if start < 0 or end < 0:
        raise AssertionError(
            "docs/02_仕様書.md の §7.3 〜 §7.4 が見つかりません。"
            "節番号が変わったらこのテストも直す")
    rows = {}
    for line in text[start:end].splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 5:
            continue
        suffixes = re.findall(r"`(_[A-Za-z]+)`", cells[0])
        if not suffixes:
            continue            # ヘッダ行・区切り行
        parameters = re.findall(r"`([A-Za-z]+)`", cells[2])
        for suffix in suffixes:
            rows[suffix] = {
                "parameter": parameters[0] if parameters else None,
                # ✔ = sRGB ON / ✘ = OFF
                "srgb": "\u2714" in cells[3],
                "compression": _COMPRESSION_BY_LABEL.get(cells[4], cells[4]),
            }
    return rows


# --- フェイク（UE 依存部を unreal 無しで動かす） ------------------------------


class _FakeEnum(object):
    """`unreal.TextureCompressionSettings` の代役。

    **`TC_DEFAULT` は値 0** にしてある。真偽値で判定する実装だと
    「取れなかった」と誤読するため（`compression_value` は `is not None` で見る）。
    """
    TC_DEFAULT = 0
    TC_NORMALMAP = 1
    TC_MASKS = 6


class _FakeUnreal(object):
    TextureCompressionSettings = _FakeEnum


class _FakeTexture(object):
    """`get_editor_property` / `set_editor_property` だけ持つテクスチャの代役。

    `known` に無いプロパティ名は **UE と同じく例外**にする（候補名を順に
    試す実装 / docs/99 P-15 が効いているかを確かめるため）。
    """

    def __init__(self, known=("srgb", "compression_settings"), values=None):
        self.known = tuple(known)
        self.values = dict(values or {})
        self.post_edit_change_calls = 0

    def set_editor_property(self, name, value):
        if name not in self.known:
            raise RuntimeError("プロパティがありません: {0}".format(name))
        self.values[name] = value

    def get_editor_property(self, name):
        if name not in self.known:
            raise RuntimeError("プロパティがありません: {0}".format(name))
        return self.values.get(name)

    def post_edit_change(self):
        self.post_edit_change_calls += 1   # void（戻り値なし / docs/99 P-18）


# --- 表の一致（二重管理の検出） ----------------------------------------------


class TestSuffixTableIsNotDuplicated(unittest.TestCase):
    def test_keys_match_the_default_preset(self):
        """`TEXTURE_SETTINGS` のキー集合が既定プリセットと一致すること。

        片方だけサフィックスを足したら落ちる。
        """
        self.assertEqual(sorted(textures.TEXTURE_SETTINGS),
                         sorted(_preset_parameter_map()))

    def test_keys_match_the_spec_table(self):
        """`TEXTURE_SETTINGS` のキー集合が docs/02 §7.3 の表と一致すること。"""
        self.assertEqual(sorted(textures.TEXTURE_SETTINGS), sorted(_spec_table()))

    def test_spec_table_parsed_all_nine_rows(self):
        """表がパースできていることを先に確かめる（0 件でも一致してしまうため）。"""
        self.assertEqual(len(_spec_table()), 9)

    def test_srgb_and_compression_match_the_spec_table(self):
        """各サフィックスの sRGB / 圧縮設定が docs/02 §7.3 と一致すること。"""
        spec = _spec_table()
        for suffix, expected in sorted(spec.items()):
            with self.subTest(suffix=suffix):
                actual = textures.TEXTURE_SETTINGS[suffix]
                self.assertEqual(actual["srgb"], expected["srgb"])
                self.assertEqual(actual["compression"], expected["compression"])

    def test_preset_parameter_names_match_the_spec_table(self):
        """MI パラメータ名もプリセットと仕様で一致すること。"""
        preset = _preset_parameter_map()
        spec = _spec_table()
        for suffix, expected in sorted(spec.items()):
            with self.subTest(suffix=suffix):
                self.assertEqual(preset[suffix], expected["parameter"])

    def test_every_compression_name_has_candidates(self):
        """圧縮設定名はすべて候補表（P-15）に載っていること。"""
        for suffix, settings in sorted(textures.TEXTURE_SETTINGS.items()):
            with self.subTest(suffix=suffix):
                candidates = textures.compression_candidates(settings["compression"])
                self.assertIn(settings["compression"], candidates)
                self.assertEqual(candidates[0], settings["compression"])


class TestSettingsFor(unittest.TestCase):
    def test_normal_map_is_normalmap_and_srgb_off(self):
        settings = textures.settings_for("_N")
        self.assertEqual(settings["compression"], "TC_NORMALMAP")
        self.assertFalse(settings["srgb"])

    def test_color_suffixes_are_srgb_on(self):
        for suffix in ("_BC", "_D", "_E"):
            with self.subTest(suffix=suffix):
                settings = textures.settings_for(suffix)
                self.assertTrue(settings["srgb"])
                self.assertEqual(settings["compression"], "TC_DEFAULT")

    def test_data_suffixes_are_srgb_off(self):
        for suffix in ("_ORM", "_M", "_R", "_AO", "_MSK"):
            with self.subTest(suffix=suffix):
                settings = textures.settings_for(suffix)
                self.assertFalse(settings["srgb"])
                self.assertEqual(settings["compression"], "TC_MASKS")

    def test_unknown_suffix_returns_none_without_raising(self):
        """未知のサフィックスは None。**例外にしない**（呼び側が警告を積む）。"""
        for suffix in ("_XYZ", "_Height", "_ROUGHNESSMAP"):
            with self.subTest(suffix=suffix):
                self.assertIsNone(textures.settings_for(suffix))

    def test_empty_and_garbage_return_none(self):
        for suffix in (None, "", 0, [], {}):
            with self.subTest(suffix=suffix):
                self.assertIsNone(textures.settings_for(suffix))

    def test_underscore_and_case_are_normalized(self):
        """`bc` / `_bc` / `BC` はどれも `_BC` として引けること。"""
        expected = textures.TEXTURE_SETTINGS["_BC"]
        for suffix in ("bc", "_bc", "BC", " _Bc "):
            with self.subTest(suffix=suffix):
                self.assertEqual(textures.settings_for(suffix), expected)

    def test_settings_are_not_mutated_by_callers(self):
        """返した辞書を書き換えても表が壊れないこと（共有参照の事故防止）。"""
        settings = textures.settings_for("_N")
        self.assertFalse(settings["srgb"])
        self.assertIs(settings, textures.TEXTURE_SETTINGS["_N"])


class TestAssetName(unittest.TestCase):
    def test_drops_the_extension(self):
        self.assertEqual(textures.asset_name_for("T_Crate_01_BC.png"),
                         "T_Crate_01_BC")

    def test_drops_the_folder_and_the_extension(self):
        self.assertEqual(textures.asset_name_for("sub/dir/T_Crate_01_N.tga"),
                         "T_Crate_01_N")

    def test_accepts_backslashes(self):
        self.assertEqual(textures.asset_name_for(r"sub\T_Crate_01_ORM.exr"),
                         "T_Crate_01_ORM")

    def test_keeps_the_last_extension_only(self):
        self.assertEqual(textures.asset_name_for("T_A_BC.1001.png"), "T_A_BC.1001")

    def test_no_extension_is_kept_as_is(self):
        self.assertEqual(textures.asset_name_for("T_Crate_01_BC"), "T_Crate_01_BC")

    def test_empty_is_empty(self):
        self.assertEqual(textures.asset_name_for(None), "")
        self.assertEqual(textures.asset_name_for(""), "")


class TestNamingWarnings(unittest.TestCase):
    def test_compliant_name_has_no_warning(self):
        self.assertEqual(textures.naming_warnings("T_Crate_01_BC"), [])

    def test_missing_prefix_is_detected(self):
        """`T_` が無い名前を検出すること（docs/04 §1）。"""
        problems = textures.naming_warnings("Crate_01_BC")
        self.assertTrue(any("T_" in p for p in problems))

    def test_dot_is_detected(self):
        """ドットを含む名前（Blender の `.001` 等）を検出すること（docs/04 N-3）。"""
        self.assertTrue(textures.naming_warnings("T_Crate_01_BC.001"))

    def test_leading_digit_is_detected(self):
        """先頭が数字の名前を検出すること（docs/04 N-1）。"""
        self.assertTrue(textures.naming_warnings("01_T_Crate"))

    def test_multibyte_is_detected(self):
        """2バイト文字を検出すること（docs/04 N-3）。"""
        self.assertTrue(textures.naming_warnings("T_木箱_BC"))

    def test_space_and_hyphen_are_detected(self):
        for name in ("T_Crate 01_BC", "T_Crate-01_BC"):
            with self.subTest(name=name):
                self.assertTrue(textures.naming_warnings(name))

    def test_empty_name_is_detected(self):
        self.assertTrue(textures.naming_warnings(""))

    def test_violations_are_warnings_not_exceptions(self):
        """**勝手にリネームせず**、例外も投げずに警告の配列を返すこと。"""
        for name in ("", "crate", "T_木箱", "T_a-b"):
            with self.subTest(name=name):
                self.assertIsInstance(textures.naming_warnings(name), list)


# --- パス解決（マニフェストからの相対） --------------------------------------


class TextureFolderCase(unittest.TestCase):
    """`<assetFolder>/fbx/onepochi.json` 構成（docs/03 §5.1）の一時フォルダを作る。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="onepochi_tex_")
        self.asset_dir = os.path.join(self.tmp, "Crate_01")
        self.fbx_dir = os.path.join(self.asset_dir, "fbx")
        self.texture_dir = os.path.join(self.asset_dir, "textures")
        os.makedirs(self.fbx_dir)
        os.makedirs(self.texture_dir)
        self.manifest_path = os.path.join(self.fbx_dir, "onepochi.json")
        with io.open(self.manifest_path, "w", encoding="utf-8") as handle:
            handle.write("{}")
        self.base_color = os.path.join(self.texture_dir, "T_Crate_01_BC.png")
        with io.open(self.base_color, "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestPathResolution(TextureFolderCase):
    def test_relative_to_the_asset_folder_next_to_the_manifest(self):
        """`textureFolder` は **assetFolder からの相対**（docs/03 §5.1）。

        `assetFolder` の宣言が無くても、マニフェストの1つ上から解決できること。
        """
        resolved = textures.resolve_texture_folder("textures", self.manifest_path)
        self.assertEqual(os.path.normcase(resolved),
                         os.path.normcase(self.texture_dir))

    def test_never_resolved_against_the_current_directory(self):
        """**cwd に依存しないこと。**別の cwd から呼んでも同じ結果になる。"""
        original = os.getcwd()
        try:
            os.chdir(self.tmp)
            from_tmp = textures.resolve_texture_folder("textures", self.manifest_path)
            os.chdir(PROJECT_ROOT)
            from_root = textures.resolve_texture_folder("textures", self.manifest_path)
        finally:
            os.chdir(original)
        self.assertEqual(os.path.normcase(from_tmp), os.path.normcase(from_root))
        self.assertEqual(os.path.normcase(from_tmp),
                         os.path.normcase(self.texture_dir))

    def test_falls_back_to_the_manifest_folder(self):
        """アセットフォルダ側に無ければ**マニフェストと同じ階層**を見ること。

        これは `manifest_io.resolve_source_file` と**同じ基準**（マニフェストの
        あるフォルダ）。実際に `resolve_source_file` と突き合わせて確かめる。
        """
        shutil.rmtree(self.texture_dir)
        beside = os.path.join(self.fbx_dir, "textures")
        os.makedirs(beside)
        resolved = textures.resolve_texture_folder("textures", self.manifest_path)
        self.assertEqual(os.path.normcase(resolved), os.path.normcase(beside))

        # manifest_io が `assets[].sourceFile` を解決する基準と一致すること
        fbx = os.path.join(self.fbx_dir, "SM_Crate_01.fbx")
        with io.open(fbx, "wb") as handle:
            handle.write(b"fbx")
        source = manifest_io.resolve_source_file(
            self.manifest_path, {"sourceFile": "textures/../SM_Crate_01.fbx"})
        self.assertEqual(os.path.normcase(os.path.dirname(source)),
                         os.path.normcase(os.path.dirname(resolved)))

    def test_absolute_texture_folder_is_used_as_is(self):
        resolved = textures.resolve_texture_folder(self.texture_dir,
                                                   self.manifest_path)
        self.assertEqual(os.path.normcase(resolved),
                         os.path.normcase(self.texture_dir))

    def test_missing_folder_returns_the_first_candidate_without_raising(self):
        """存在しないフォルダでも**例外にしない**（`plan_imports` が exists で返す）。"""
        resolved = textures.resolve_texture_folder("nope", self.manifest_path)
        self.assertEqual(os.path.normcase(resolved),
                         os.path.normcase(os.path.join(self.asset_dir, "nope")))

    def test_default_folder_name_when_unspecified(self):
        resolved = textures.resolve_texture_folder(None, self.manifest_path)
        self.assertEqual(os.path.normcase(resolved),
                         os.path.normcase(self.texture_dir))
        self.assertEqual(textures.DEFAULT_TEXTURE_FOLDER, "textures")


class TestTextureFolderOf(TextureFolderCase):
    def test_absolute_asset_folder_wins(self):
        manifest = {"source": {"assetFolder": self.asset_dir.replace("\\", "/"),
                               "textureFolder": "textures"}}
        self.assertEqual(
            os.path.normcase(textures.texture_folder_of(manifest, self.manifest_path)),
            os.path.normcase(self.texture_dir))

    def test_explicit_null_means_no_textures(self):
        """`textureFolder: null` は「テクスチャを持たない」宣言（docs/03 §5.1）。"""
        manifest = {"source": {"assetFolder": self.asset_dir, "textureFolder": None}}
        self.assertIsNone(textures.texture_folder_of(manifest, self.manifest_path))

    def test_missing_source_falls_back_to_the_default(self):
        for manifest in (None, {}, {"source": {}}):
            with self.subTest(manifest=manifest):
                self.assertEqual(
                    os.path.normcase(
                        textures.texture_folder_of(manifest, self.manifest_path)),
                    os.path.normcase(self.texture_dir))

    def test_relative_asset_folder_is_ignored(self):
        """`assetFolder` は絶対パス必須（docs/03 §5.1）。相対なら使わない。"""
        manifest = {"source": {"assetFolder": "Crate_01", "textureFolder": "textures"}}
        self.assertEqual(
            os.path.normcase(textures.texture_folder_of(manifest, self.manifest_path)),
            os.path.normcase(self.texture_dir))


# --- インポート計画 -----------------------------------------------------------


ROOT = "/Game/_Dev/Props/Crate_01/Textures"


class TestPlanImports(TextureFolderCase):
    def test_existing_file_is_planned(self):
        plans = textures.plan_imports({"_BC": "T_Crate_01_BC.png"}, "textures",
                                      self.manifest_path, ROOT)
        self.assertEqual(len(plans), 1)
        plan = plans[0]
        self.assertTrue(plan["exists"])
        self.assertEqual(os.path.normcase(plan["sourcePath"]),
                         os.path.normcase(self.base_color))
        self.assertEqual(plan["assetName"], "T_Crate_01_BC")
        self.assertEqual(plan["destination"], ROOT)
        self.assertEqual(plan["assetPath"], ROOT + "/T_Crate_01_BC")
        self.assertEqual(plan["settings"], textures.TEXTURE_SETTINGS["_BC"])
        self.assertEqual(plan["warnings"], [])

    def test_missing_file_is_reported_without_raising(self):
        """存在しないファイルは `exists: False`。**例外を投げない。**"""
        plans = textures.plan_imports({"_N": "T_Crate_01_N.png"}, "textures",
                                      self.manifest_path, ROOT)
        self.assertEqual(len(plans), 1)
        self.assertFalse(plans[0]["exists"])
        self.assertTrue(any("見つかりません" in w for w in plans[0]["warnings"]))
        # 設定は引けている（ファイルの有無とは独立）
        self.assertEqual(plans[0]["settings"]["compression"], "TC_NORMALMAP")

    def test_missing_folder_does_not_raise(self):
        plans = textures.plan_imports({"_BC": "T_A_BC.png"}, "no_such_folder",
                                      self.manifest_path, ROOT)
        self.assertFalse(plans[0]["exists"])

    def test_unknown_suffix_warns_and_keeps_settings_none(self):
        plans = textures.plan_imports({"_HEIGHT": "T_Crate_01_BC.png"}, "textures",
                                      self.manifest_path, ROOT)
        self.assertIsNone(plans[0]["settings"])
        self.assertTrue(any("7.3" in w for w in plans[0]["warnings"]))
        self.assertTrue(plans[0]["exists"])

    def test_naming_violation_is_a_warning_not_a_rename(self):
        bad = os.path.join(self.texture_dir, "crate_bc.png")
        with io.open(bad, "wb") as handle:
            handle.write(b"x")
        plans = textures.plan_imports({"_BC": "crate_bc.png"}, "textures",
                                      self.manifest_path, ROOT)
        self.assertEqual(plans[0]["assetName"], "crate_bc")   # リネームしない
        self.assertTrue(plans[0]["warnings"])

    def test_empty_path_value_does_not_raise(self):
        for value in (None, "", 0, []):
            with self.subTest(value=value):
                plans = textures.plan_imports({"_BC": value}, "textures",
                                              self.manifest_path, ROOT)
                self.assertEqual(len(plans), 1)
                self.assertFalse(plans[0]["exists"])
                self.assertTrue(plans[0]["warnings"])

    def test_sorted_by_suffix_for_stable_reports(self):
        plans = textures.plan_imports(
            {"_N": "n.png", "_BC": "bc.png", "_ORM": "orm.png"},
            "textures", self.manifest_path, ROOT)
        self.assertEqual([p["suffix"] for p in plans], ["_BC", "_N", "_ORM"])

    def test_empty_input_returns_empty(self):
        for value in (None, {}, "", [], "textures", 3):
            with self.subTest(value=value):
                self.assertEqual(
                    textures.plan_imports(value, "textures",
                                          self.manifest_path, ROOT), [])

    def test_no_texture_root_still_produces_a_plan(self):
        plans = textures.plan_imports({"_BC": "T_Crate_01_BC.png"}, "textures",
                                      self.manifest_path, None)
        self.assertEqual(plans[0]["assetPath"], "T_Crate_01_BC")

    def test_subfolder_in_the_relative_path(self):
        nested = os.path.join(self.texture_dir, "sub")
        os.makedirs(nested)
        target = os.path.join(nested, "T_Crate_01_N.png")
        with io.open(target, "wb") as handle:
            handle.write(b"x")
        plans = textures.plan_imports({"_N": "sub/T_Crate_01_N.png"}, "textures",
                                      self.manifest_path, ROOT)
        self.assertTrue(plans[0]["exists"])
        self.assertEqual(plans[0]["assetPath"], ROOT + "/T_Crate_01_N")


# --- UE 依存部（フェイクで検証） ----------------------------------------------


class FakeUnrealCase(unittest.TestCase):
    def setUp(self):
        self._saved = textures.unreal
        textures.unreal = _FakeUnreal()
        self.sink = []

    def tearDown(self):
        textures.unreal = self._saved


class TestCompressionValue(FakeUnrealCase):
    def test_default_is_zero_and_must_not_be_read_as_missing(self):
        """`TC_DEFAULT` は値 0。**真偽値で判定すると取れていないと誤読する。**"""
        value, name = textures.compression_value("TC_DEFAULT", self.sink)
        self.assertEqual(value, 0)
        self.assertIsNotNone(value)
        self.assertEqual(name, "TC_DEFAULT")
        self.assertEqual(self.sink, [])

    def test_normalmap_and_masks(self):
        self.assertEqual(textures.compression_value("TC_NORMALMAP", self.sink)[0], 1)
        self.assertEqual(textures.compression_value("TC_MASKS", self.sink)[0], 6)
        self.assertEqual(self.sink, [])

    def test_unknown_enum_warns_instead_of_raising(self):
        value, name = textures.compression_value("TC_NOPE", self.sink)
        self.assertIsNone(value)
        self.assertIsNone(name)
        self.assertTrue(self.sink)

    def test_without_unreal_it_warns(self):
        textures.unreal = None
        value, name = textures.compression_value("TC_DEFAULT", self.sink)
        self.assertIsNone(value)
        self.assertTrue(self.sink)


class TestApplyAndVerify(FakeUnrealCase):
    def test_normal_map_gets_srgb_off_and_normalmap_compression(self):
        texture = _FakeTexture()
        record = textures.apply_texture_settings(
            texture, textures.TEXTURE_SETTINGS["_N"], self.sink)
        self.assertEqual(texture.values["srgb"], False)
        self.assertEqual(texture.values["compression_settings"],
                         _FakeEnum.TC_NORMALMAP)
        self.assertEqual(record["srgb"], False)
        self.assertEqual(record["compressionEnum"], "TC_NORMALMAP")
        self.assertEqual(self.sink, [])

    def test_base_color_gets_srgb_on_and_default_compression(self):
        texture = _FakeTexture()
        textures.apply_texture_settings(
            texture, textures.TEXTURE_SETTINGS["_BC"], self.sink)
        self.assertIs(texture.values["srgb"], True)
        self.assertEqual(texture.values["compression_settings"],
                         _FakeEnum.TC_DEFAULT)

    def test_post_edit_change_is_called_and_void_is_not_misread(self):
        """`post_edit_change()` は void。戻り値で成否を判定しないこと（P-18）。"""
        texture = _FakeTexture()
        record = textures.apply_texture_settings(
            texture, textures.TEXTURE_SETTINGS["_N"], self.sink)
        self.assertEqual(texture.post_edit_change_calls, 1)
        self.assertTrue(record["postEditChange"])
        self.assertEqual(self.sink, [])

    def test_fallback_property_name_is_recorded(self):
        """第1候補が通らなければ次の候補を試し、通った名前を残すこと（P-15）。"""
        texture = _FakeTexture(known=("SRGB", "CompressionSettings"))
        record = textures.apply_texture_settings(
            texture, textures.TEXTURE_SETTINGS["_N"], self.sink)
        self.assertEqual(record["appliedBy"]["srgb"], "SRGB")
        self.assertEqual(record["appliedBy"]["compression"], "CompressionSettings")

    def test_unknown_property_names_warn_instead_of_raising(self):
        texture = _FakeTexture(known=())
        record = textures.apply_texture_settings(
            texture, textures.TEXTURE_SETTINGS["_N"], self.sink)
        self.assertIsNone(record["srgb"])
        self.assertTrue(self.sink)

    def test_none_texture_warns(self):
        record = textures.apply_texture_settings(
            None, textures.TEXTURE_SETTINGS["_N"], self.sink)
        self.assertIsNone(record["srgb"])
        self.assertTrue(self.sink)

    def test_verify_passes_after_apply(self):
        """書いたあと**読み直して**一致を確かめること（docs/99 P-23）。"""
        texture = _FakeTexture()
        settings = textures.TEXTURE_SETTINGS["_N"]
        textures.apply_texture_settings(texture, settings, self.sink)
        verified = textures.verify_texture_settings(texture, settings, self.sink)
        self.assertTrue(verified["matches"])
        self.assertEqual(sorted(verified["checked"]), ["compression", "srgb"])
        self.assertEqual(self.sink, [])

    def test_verify_catches_a_setting_that_did_not_take(self):
        """「設定したつもりで効いていない」を実測で捕まえること（P-23 の本題）。"""
        texture = _FakeTexture(values={"srgb": True,
                                       "compression_settings": _FakeEnum.TC_DEFAULT})
        verified = textures.verify_texture_settings(
            texture, textures.TEXTURE_SETTINGS["_N"], self.sink)
        self.assertFalse(verified["matches"])
        self.assertEqual(len(verified["problems"]), 2)
        self.assertTrue(self.sink)

    def test_verify_cannot_confirm_is_none_not_true(self):
        """読めなかったら `matches` は **None**（確かめられないことを合格にしない）。"""
        texture = _FakeTexture(known=())
        verified = textures.verify_texture_settings(
            texture, textures.TEXTURE_SETTINGS["_N"], self.sink)
        self.assertIsNone(verified["matches"])
        self.assertEqual(verified["checked"], [])

    def test_verify_without_settings_is_none(self):
        verified = textures.verify_texture_settings(_FakeTexture(), None, self.sink)
        self.assertIsNone(verified["matches"])


class TestImportTextures(FakeUnrealCase):
    def test_no_plans_returns_a_well_formed_record(self):
        record = textures.import_textures([], self.sink)
        for key in ("paths", "imported", "missing", "failed", "reused"):
            self.assertEqual(record[key], [])
        self.assertEqual(record["saved"], 0)
        self.assertEqual(record["verifyFailed"], 0)

    def test_missing_sources_are_reported_and_nothing_raises(self):
        """ソースが1枚も無くても**例外にしない**（MI 生成を続行させる）。"""
        plans = [{"suffix": "_BC", "sourcePath": "C:/nope/T_A_BC.png",
                  "assetName": "T_A_BC", "destination": ROOT,
                  "assetPath": ROOT + "/T_A_BC", "settings": None,
                  "exists": False, "warnings": ["見つかりません"]}]
        record = textures.import_textures(plans, self.sink)
        self.assertEqual(len(record["missing"]), 1)
        self.assertEqual(record["imported"], [])
        self.assertIn("見つかりません", self.sink)

    def test_plan_warnings_are_forwarded_to_the_sink(self):
        plans = [{"suffix": "_BC", "sourcePath": None, "assetName": "",
                  "destination": ROOT, "assetPath": None, "settings": None,
                  "exists": False, "warnings": ["警告A", "警告B"]}]
        textures.import_textures(plans, self.sink)
        self.assertIn("警告A", self.sink)
        self.assertIn("警告B", self.sink)

    def test_without_interchange_manager_everything_fails_softly(self):
        plans = [{"suffix": "_BC", "sourcePath": __file__, "assetName": "T_A_BC",
                  "destination": ROOT, "assetPath": ROOT + "/T_A_BC",
                  "settings": textures.TEXTURE_SETTINGS["_BC"],
                  "exists": True, "warnings": []}]
        record = textures.import_textures(plans, self.sink)
        self.assertEqual(len(record["failed"]), 1)
        self.assertEqual(record["imported"], [])
        self.assertTrue(self.sink)

    def test_already_imported_textures_are_reused(self):
        """同じテクスチャを複数スロットが参照しても二重に取り込まないこと。"""
        plans = [{"suffix": "_BC", "sourcePath": __file__, "assetName": "T_A_BC",
                  "destination": ROOT, "assetPath": ROOT + "/T_A_BC",
                  "settings": textures.TEXTURE_SETTINGS["_BC"],
                  "exists": True, "warnings": []}]
        done = {ROOT + "/T_A_BC": "_BC"}
        record = textures.import_textures(plans, self.sink, done=done)
        self.assertEqual(len(record["reused"]), 1)
        self.assertEqual(record["failed"], [])
        self.assertEqual(record["paths"], [ROOT + "/T_A_BC"])


# --- 往復検証の比較項目（P5 の DoD / K-8） ------------------------------------


def _slot_record(imported=None, observed=None, unassigned=None, assigned=0):
    """`materials.apply_material_slots` が作るスロット記録の形をまねる。

    **形を推測で書かないこと**: `assigned` は件数（int）、`observed` は
    `{パラメータ名: パス or None}`、`imported` は `import_slot_textures` の記録。
    """
    return {
        "index": 0,
        "slotName": "M_Crate_Body",
        "textureRoot": "/Game/_Dev/Samples/S11_Texture/Crate_01/Textures",
        "declared": 3,
        "imported": imported if imported is not None else {
            "planned": 0, "paths": [], "imported": [], "missing": [],
            "failed": [], "reused": [], "saved": 0, "verifyFailed": 0,
            "skipped": None},
        "assigned": assigned,
        "observed": observed if observed is not None else {},
        "unassigned": unassigned if unassigned is not None else [],
    }


def _import_block(imported, missing=(), failed=(), reused=()):
    return {"paths": [x["assetPath"] for x in imported],
            "imported": list(imported), "missing": list(missing),
            "failed": list(failed), "reused": list(reused),
            "saved": len(imported), "verifyFailed": 0, "skipped": None}


def _imported_one(suffix, srgb, compression, checked=("srgb", "compression"),
                  problems=()):
    return {
        "suffix": suffix,
        "assetName": "T_Crate_Body" + suffix,
        "assetPath": "/Game/x/T_Crate_Body" + suffix,
        "settings": textures.TEXTURE_SETTINGS[suffix],
        "applied": True,
        "verified": {"srgb": srgb, "compression": compression,
                     "readBy": {"srgb": "srgb",
                                "compression": "compression_settings"},
                     "checked": list(checked), "problems": list(problems),
                     "matches": not problems},
        "saved": True,
    }


def _single_slot_asset(suffix, relative):
    return {"materialSlots": [{"index": 0, "slotName": "M_Crate_Body",
                               "textures": {suffix: relative}}]}


#: S-11 の実物（`T_Crate_Body_{BC,N,ORM}.jpg` / 2026-10-14 実測）
S11_ASSET = {
    "materialSlots": [{
        "index": 0, "slotName": "M_Crate_Body",
        "textures": {"_BC": "T_Crate_Body_BC.jpg",
                     "_N": "T_Crate_Body_N.jpg",
                     "_ORM": "T_Crate_Body_ORM.jpg"},
    }],
}

#: `sample_texture` プリセットの実効値（`project_default` からの継承込み）
S11_POLICY = {"importTextures": True,
              "textureParameterMap": {"_BC": "BaseColor", "_N": "Normal",
                                      "_ORM": "ORM"}}


class CompareTexturesTest(unittest.TestCase):
    """`compare_textures` は「確かめられなかった」を合格にしない。"""

    def test_asset_without_textures_gets_no_items(self):
        """S-01〜S-10 を落とさないため、宣言が無ければ項目を作らない。"""
        self.assertEqual(textures.compare_textures({}, {}), {})
        self.assertEqual(
            textures.compare_textures(
                {"materialSlots": [{"index": 0, "slotName": "M_A"}]}, {}), {})

    def test_all_green_when_everything_is_measured(self):
        imported = [_imported_one("_BC", True, 0), _imported_one("_N", False, 2),
                    _imported_one("_ORM", False, 5)]
        record = {"textures": [_slot_record(
            imported=_import_block(imported),
            observed={"BaseColor": "/Game/x/T_Crate_Body_BC",
                      "Normal": "/Game/x/T_Crate_Body_N",
                      "ORM": "/Game/x/T_Crate_Body_ORM"},
            assigned=3)]}
        result = textures.compare_textures(S11_ASSET, record, S11_POLICY)
        self.assertEqual(result["textureCount"]["declared"], 3)
        self.assertTrue(result["textureCount"]["matches"])
        self.assertTrue(result["textureCompression"]["matches"])
        self.assertTrue(result["textureSrgb"]["matches"])
        self.assertTrue(result["textureParameters"]["matches"])
        self.assertEqual(result["textureParameters"]["expected"],
                         ["BaseColor", "Normal", "ORM"])

    def test_missing_texture_fails_the_count(self):
        imported = [_imported_one("_BC", True, 0)]
        record = {"textures": [_slot_record(
            imported=_import_block(imported, missing=[{"suffix": "_N"}]),
            observed={"BaseColor": "/Game/x/T_Crate_Body_BC"}, assigned=1)]}
        result = textures.compare_textures(S11_ASSET, record, S11_POLICY)
        self.assertFalse(result["textureCount"]["matches"])
        self.assertEqual(result["textureCount"]["missing"], 1)

    def test_reused_textures_count_toward_the_total(self):
        """複数スロットが同じテクスチャを使う場合、再利用も「取り込めた」に数える。"""
        imported = [_imported_one("_BC", True, 0), _imported_one("_N", False, 2)]
        record = {"textures": [_slot_record(
            imported=_import_block(
                imported, reused=[{"suffix": "_ORM",
                                   "assetPath": "/Game/x/T_Crate_Body_ORM"}]),
            observed={"BaseColor": "/Game/x/a", "Normal": "/Game/x/b",
                      "ORM": "/Game/x/c"},
            assigned=3)]}
        result = textures.compare_textures(S11_ASSET, record, S11_POLICY)
        self.assertTrue(result["textureCount"]["matches"])
        self.assertEqual(result["textureCount"]["reused"], 1)

    def test_unverified_settings_are_not_a_pass(self):
        """読めなかったものを緑にしない（docs/99 K-8）。"""
        imported = [_imported_one("_BC", None, None, checked=())]
        record = {"textures": [_slot_record(
            imported=_import_block(imported),
            observed={"BaseColor": "/Game/x/T_Crate_Body_BC"}, assigned=1)]}
        result = textures.compare_textures(
            _single_slot_asset("_BC", "T_Crate_Body_BC.jpg"), record, S11_POLICY)
        self.assertFalse(result["textureCompression"]["matches"])
        self.assertFalse(result["textureSrgb"]["matches"])
        self.assertIn("_BC", result["textureCompression"]["unverified"])

    def test_settings_that_did_not_stick_are_reported(self):
        imported = [_imported_one("_N", True, 0,
                                  problems=["sRGB が True のままです"])]
        record = {"textures": [_slot_record(
            imported=_import_block(imported),
            observed={"Normal": "/Game/x/T_Crate_Body_N"}, assigned=1)]}
        result = textures.compare_textures(
            _single_slot_asset("_N", "T_Crate_Body_N.jpg"), record, S11_POLICY)
        self.assertFalse(result["textureSrgb"]["matches"])
        self.assertTrue(result["textureSrgb"]["problems"])

    def test_unreadable_parameters_are_not_a_pass(self):
        """`observed` が空＝読めなかった。割当なしと区別して合格にしない（P-23）。"""
        imported = [_imported_one("_BC", True, 0)]
        record = {"textures": [_slot_record(
            imported=_import_block(imported), observed={}, assigned=1)]}
        result = textures.compare_textures(
            _single_slot_asset("_BC", "T_Crate_Body_BC.jpg"), record, S11_POLICY)
        self.assertFalse(result["textureParameters"]["matches"])
        self.assertIn("確かめられない", result["textureParameters"]["note"])

    def test_parameter_read_as_none_is_not_assigned(self):
        """キーはあるが値が None ＝「読めたが何も乗っていない」。"""
        imported = [_imported_one("_BC", True, 0)]
        record = {"textures": [_slot_record(
            imported=_import_block(imported),
            observed={"BaseColor": None}, assigned=1)]}
        result = textures.compare_textures(
            _single_slot_asset("_BC", "T_Crate_Body_BC.jpg"), record, S11_POLICY)
        self.assertEqual(result["textureParameters"]["observed"], [])
        self.assertFalse(result["textureParameters"]["matches"])

    def test_unassigned_items_fail_the_parameters(self):
        imported = [_imported_one("_BC", True, 0)]
        record = {"textures": [_slot_record(
            imported=_import_block(imported),
            observed={"BaseColor": "/Game/x/T_Crate_Body_BC"},
            unassigned=[{"suffix": "_BC", "parameter": "BaseColor"}],
            assigned=1)]}
        result = textures.compare_textures(
            _single_slot_asset("_BC", "T_Crate_Body_BC.jpg"), record, S11_POLICY)
        self.assertFalse(result["textureParameters"]["matches"])

    def test_suffix_without_a_parameter_mapping_fails(self):
        imported = [_imported_one("_BC", True, 0)]
        record = {"textures": [_slot_record(
            imported=_import_block(imported),
            observed={"BaseColor": "/Game/x/T_Crate_Body_BC"}, assigned=1)]}
        result = textures.compare_textures(
            _single_slot_asset("_BC", "T_Crate_Body_BC.jpg"), record,
            {"textureParameterMap": {}})
        self.assertFalse(result["textureParameters"]["matches"])
        self.assertEqual(result["textureParameters"]["unmappedSuffixes"], ["_BC"])

    def test_the_four_items_are_conditional_in_the_verify_policy(self):
        """S-01〜S-10（テクスチャ無し）が落ちないことを方針モジュール側でも確かめる。"""
        addon = os.path.join(PROJECT_ROOT, "tools", "dcc_blender", "addons",
                             "onepochi")
        if addon not in sys.path:
            sys.path.insert(0, addon)
        from core import verify as verify_mod
        # `lodTriangleCounts` は項目を作っていたのに検査リストに載っておらず、
        # **ずれても誰も気づかない**状態だった（2026-10-14 の実機検証で発覚）
        for key in ("textureCount", "textureCompression", "textureSrgb",
                    "textureParameters", "lodTriangleCounts"):
            self.assertIn(key, verify_mod.CONDITIONAL)
            self.assertNotIn(key, verify_mod.required_keys("StaticMesh"))
            self.assertNotIn(key, verify_mod.required_keys("SkeletalMesh"))


class JsonableTest(unittest.TestCase):
    """UE の enum をレポートに入れて `json.dump` が落ちた件（docs/99 P-33）。"""

    class _FakeEnum(object):
        """`unreal.TextureCompressionSettings.TC_NORMALMAP` のかわり。"""
        name = "TC_NORMALMAP"

        def __repr__(self):
            return "TextureCompressionSettings.TC_NORMALMAP"

    class _NoName(object):
        def __repr__(self):
            return "<値>"

    def test_primitives_pass_through(self):
        for value in (None, True, False, 0, 5, 1.5, "TC_MASKS"):
            self.assertEqual(textures.jsonable(value), value)

    def test_enum_becomes_its_name(self):
        self.assertEqual(textures.jsonable(self._FakeEnum()), "TC_NORMALMAP")

    def test_object_without_name_falls_back_to_str(self):
        self.assertEqual(textures.jsonable(self._NoName()), "<値>")

    def test_result_of_jsonable_is_always_serializable(self):
        """これが落ちると、取り込みは成功しているのにレポートが消える。"""
        for value in (self._FakeEnum(), self._NoName(), None, True, 3):
            json.dumps({"compression": textures.jsonable(value)})


if __name__ == "__main__":
    unittest.main(verbosity=2)
