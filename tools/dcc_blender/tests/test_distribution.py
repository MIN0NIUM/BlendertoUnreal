"""配布（ZIP）の単体テスト（bpy / unreal 不要 / P6-3 Q-15）。

ZIP の構造を**機械的に**検証する。Blender の「ディスクからインストール」は
ZIP 直下に単一パッケージがあることを要求し、`tools/` は配布先に存在しないため
**プリセットの同梱が必須**になる。この2点を人の目に頼らず確認する。

実行:
    python tools/dcc_blender/tests/test_distribution.py
"""

import os
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DCC_ROOT = os.path.dirname(HERE)
TOOLS_ROOT = os.path.dirname(DCC_ROOT)
PROJECT_ROOT = os.path.dirname(TOOLS_ROOT)
ADDON_ROOT = os.path.join(DCC_ROOT, "addons")

for path in (TOOLS_ROOT, ADDON_ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

import build_addon_zip  # noqa: E402
from onepochi.core import presets  # noqa: E402


def _force_utf8_stdout():
    """Windows の既定コンソール（cp932）でも日本語を出せるようにする（docs/99 P-9）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_stdout()


class TestAddonZip(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.out_dir = self._directory.name
        self.zip_path = build_addon_zip.build(self.out_dir)

    def tearDown(self):
        self._directory.cleanup()

    def test_version_matches_addon(self):
        version = build_addon_zip.read_version()
        self.assertTrue(os.path.basename(self.zip_path).endswith(version + ".zip"))

    def test_structure_verifies(self):
        self.assertEqual(build_addon_zip.verify(self.zip_path), [])

    def test_single_top_level_package(self):
        with zipfile.ZipFile(self.zip_path) as archive:
            tops = {name.split("/")[0] for name in archive.namelist() if name}
        self.assertEqual(sorted(tops), ["onepochi"])

    def test_no_pycache(self):
        with zipfile.ZipFile(self.zip_path) as archive:
            names = archive.namelist()
        offenders = [n for n in names
                     if "__pycache__" in n or n.endswith((".pyc", ".pyo"))]
        self.assertEqual(offenders, [])

    def test_presets_bundled(self):
        """`tools/presets/*.json` が全部入っていること。

        1つでも欠けると、配布先でそのプリセットだけが選べなくなる。
        """
        source = sorted(n for n in os.listdir(os.path.join(TOOLS_ROOT, "presets"))
                        if n.lower().endswith(".json"))
        with zipfile.ZipFile(self.zip_path) as archive:
            bundled = sorted(
                name.split("/")[-1] for name in archive.namelist()
                if name.startswith("onepochi/presets/") and name.endswith(".json"))
        self.assertEqual(bundled, source)
        self.assertGreaterEqual(len(bundled), 2)

    def test_verify_detects_missing_presets(self):
        """検証がちゃんと落ちることを確認する（検証の検証）。"""
        broken = os.path.join(self.out_dir, "broken.zip")
        with zipfile.ZipFile(self.zip_path) as source:
            with zipfile.ZipFile(broken, "w") as target:
                for name in source.namelist():
                    if name.startswith("onepochi/presets/"):
                        continue
                    target.writestr(name, source.read(name))
        problems = build_addon_zip.verify(broken)
        self.assertTrue(any("プリセット" in p for p in problems), problems)

    def test_installed_layout_finds_bundled_presets(self):
        """ZIP を展開した形（`tools/` が無い）でプリセットが見つかること。

        これが落ちると、配布した人の環境で**プリセットが 0 件**になる。
        `presets.preset_dir()` の同梱フォールバックが効いているかの確認。
        """
        install_root = os.path.join(self.out_dir, "install")
        with zipfile.ZipFile(self.zip_path) as archive:
            archive.extractall(install_root)
        core_dir = os.path.join(install_root, "onepochi", "core")
        self.assertTrue(os.path.isdir(core_dir))

        # `presets.preset_dir()` と同じ導出をインストール後のパスで再現する
        bundled = os.path.join(os.path.dirname(core_dir), presets.PRESET_DIR_NAME)
        self.assertTrue(os.path.isdir(bundled), bundled)
        names = presets.list_presets(bundled)
        self.assertIn("project_default", names)
        self.assertGreaterEqual(len(names), 2)

        loaded = presets.resolve_preset("project_default", root=bundled)
        self.assertIsInstance(loaded, dict)

    def test_bundled_preset_dir_wins_over_repo_layout(self):
        """同梱ディレクトリがあるときはそれを返すこと（引数・環境変数が無い場合）。"""
        saved = os.environ.pop(presets.PRESET_DIR_ENV, None)
        try:
            resolved = presets.preset_dir()
            self.assertTrue(os.path.isdir(resolved), resolved)
            names = presets.list_presets(resolved)
            self.assertIn("project_default", names)
        finally:
            if saved is not None:
                os.environ[presets.PRESET_DIR_ENV] = saved

    def test_manifest_toml_has_version(self):
        version = build_addon_zip.read_version()
        with zipfile.ZipFile(self.zip_path) as archive:
            text = archive.read("onepochi/blender_manifest.toml").decode("utf-8")
        self.assertIn('version = "{0}"'.format(version), text)
        self.assertIn('id = "onepochi"', text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
