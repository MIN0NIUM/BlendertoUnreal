"""UE 側マニフェスト検証の単体テスト（unreal 不要）。

`onepochi_ue.manifest_io` は意図的に `unreal` を import していないため、
UE を起動せずにゲートキーパーの挙動を検証できる。

実行:
    python tools/ue_plugin/tests/test_manifest_io.py
"""

import copy
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
PROJECT_ROOT = os.path.dirname(os.path.dirname(PLUGIN_ROOT))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

from onepochi_ue import manifest_io  # noqa: E402


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

SAMPLE_MANIFEST = os.path.join(
    PROJECT_ROOT, "samples", "blender", "01_static_minimal", "fbx", "onepochi.json"
)


def _write_temp(manifest, directory, filename="onepochi.json"):
    path = os.path.join(directory, filename)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False)
    return path


class TestRealSampleManifest(unittest.TestCase):
    """実際にツールが生成したマニフェストを UE 側のゲートが通すか。"""

    @classmethod
    def setUpClass(cls):
        if not os.path.isfile(SAMPLE_MANIFEST):
            raise unittest.SkipTest(
                "サンプルのマニフェストがありません。先に Blender 側の書き出しを実行してください: "
                + SAMPLE_MANIFEST
            )

    def test_prepare_accepts_generated_manifest(self):
        prepared = manifest_io.prepare(SAMPLE_MANIFEST)
        self.assertEqual(prepared["uniformScale"], 1.0)
        self.assertEqual(prepared["skipped"], [])
        self.assertIn("assets", prepared["manifest"])

    def test_resolve_source_file_finds_fbx(self):
        manifest, path = manifest_io.load(SAMPLE_MANIFEST)
        asset = manifest["assets"][0]
        fbx = manifest_io.resolve_source_file(path, asset)
        self.assertTrue(os.path.isfile(fbx))
        self.assertTrue(fbx.lower().endswith(".fbx"))

    def test_destination_passes_whitelist(self):
        manifest, _ = manifest_io.load(SAMPLE_MANIFEST)
        asset = manifest["assets"][0]
        destination = manifest_io.check_destination(manifest, asset)
        self.assertTrue(destination.startswith("/Game/_Dev"))


class TestGateKeeping(unittest.TestCase):
    """壊れた / 危険なマニフェストを確実に弾くか。"""

    def setUp(self):
        self.base = {
            "schemaVersion": manifest_io.SUPPORTED_SCHEMA_VERSION,
            "runId": "20261010_000000_abcdef",
            "generator": {"dcc": "blender", "dccVersion": "5.2.0 LTS",
                          "toolVersion": "0.1.0", "exportedAt": "2026-10-10T00:00:00+09:00"},
            "coordinateSystem": {
                "sourceUpAxis": "Z", "sourceForwardAxis": "-Y", "sourceUnitInMeters": 1.0,
                "targetUpAxis": "Z", "targetForwardAxis": "X",
                "appliedScale": 100.0, "conversionDoneBy": "fbx_exporter",
            },
            "source": {"assetName": "Crate_01", "assetFolder": "C:/a", "dccFile": "a.blend"},
            "destination": {"contentRoot": "/Game", "defaultPath": "/Game/_Dev/X",
                            "allowOverwrite": True, "pathWhitelist": ["/Game/_Dev"]},
            "validation": {"status": "passed", "ranAt": "2026-10-10T00:00:00+09:00",
                           "ruleSetVersion": "0.1.0", "issues": []},
            "assets": [{
                "id": "a001", "assetType": "StaticMesh", "name": "SM_Crate_01",
                "destinationPath": "/Game/_Dev/X", "sourceFile": "SM_Crate_01.fbx",
                "staticMesh": {},
            }],
        }

    def test_missing_file(self):
        with self.assertRaises(manifest_io.ManifestError):
            manifest_io.load(os.path.join(tempfile.gettempdir(), "no_such_onepochi.json"))

    def test_broken_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "onepochi.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{ this is not json")
            with self.assertRaises(manifest_io.ManifestError) as context:
                manifest_io.load(path)
            self.assertIn("JSON", str(context.exception))

    def test_major_version_mismatch_rejected(self):
        manifest = copy.deepcopy(self.base)
        manifest["schemaVersion"] = "9.0.0"
        with self.assertRaises(manifest_io.ManifestError) as context:
            manifest_io.check_schema_version(manifest)
        self.assertIn("メジャーバージョン", str(context.exception))

    def test_minor_version_difference_warns(self):
        """同じメジャーで値が違う場合は警告だけ出して続行する。

        バージョンを上げるたびにテストが壊れないよう、
        **実装のバージョンから隣のマイナーを機械的に作る。**
        """
        major, minor, patch = manifest_io.SUPPORTED_SCHEMA_VERSION.split(".")
        other = "{0}.{1}.{2}".format(major, int(minor) + 1, patch)
        manifest = copy.deepcopy(self.base)
        manifest["schemaVersion"] = other
        warnings = manifest_io.check_schema_version(manifest)
        self.assertEqual(len(warnings), 1)
        self.assertIn(other, warnings[0])

    def test_failed_validation_rejected(self):
        manifest = copy.deepcopy(self.base)
        manifest["validation"]["status"] = "failed"
        manifest["validation"]["issues"] = [
            {"ruleId": "V-025", "severity": "error", "message": "UV がありません"}
        ]
        with self.assertRaises(manifest_io.ManifestError) as context:
            manifest_io.check_validation_status(manifest)
        self.assertIn("V-025", str(context.exception))

    def test_failed_validation_allowed_with_flag(self):
        manifest = copy.deepcopy(self.base)
        manifest["validation"]["status"] = "failed"
        self.assertEqual(manifest_io.check_validation_status(manifest, allow_failed=True), [])

    def test_shape_detects_missing_keys(self):
        manifest = copy.deepcopy(self.base)
        del manifest["source"]
        del manifest["assets"][0]["staticMesh"]
        problems = manifest_io.check_shape(manifest)
        self.assertTrue(any("source" in p for p in problems))
        self.assertTrue(any("staticMesh" in p for p in problems))

    def test_shape_detects_unknown_asset_type(self):
        manifest = copy.deepcopy(self.base)
        manifest["assets"][0]["assetType"] = "Nonsense"
        problems = manifest_io.check_shape(manifest)
        self.assertTrue(any("assetType" in p for p in problems))

    def test_applied_scale_zero_rejected(self):
        manifest = copy.deepcopy(self.base)
        manifest["coordinateSystem"]["appliedScale"] = 0
        with self.assertRaises(manifest_io.ManifestError):
            manifest_io.check_coordinate_system(manifest)

    def test_uniform_scale_computed_from_applied_scale(self):
        manifest = copy.deepcopy(self.base)
        manifest["coordinateSystem"]["appliedScale"] = 1.0
        scale, warnings = manifest_io.check_coordinate_system(manifest)
        self.assertEqual(scale, 100.0)
        self.assertTrue(any("Import Uniform Scale" in w for w in warnings))

    def test_uniform_scale_is_one_for_centimeter_export(self):
        scale, warnings = manifest_io.check_coordinate_system(self.base)
        self.assertEqual(scale, 1.0)
        self.assertEqual([w for w in warnings if "Import Uniform Scale" in w], [])

    def test_non_metre_source_unit_warns(self):
        manifest = copy.deepcopy(self.base)
        manifest["coordinateSystem"]["sourceUnitInMeters"] = 0.01
        _, warnings = manifest_io.check_coordinate_system(manifest)
        self.assertTrue(any("1単位" in w for w in warnings))

    def test_destination_outside_whitelist_rejected(self):
        manifest = copy.deepcopy(self.base)
        manifest["assets"][0]["destinationPath"] = "/Game/Production/Props/Crate_01"
        with self.assertRaises(manifest_io.ManifestError) as context:
            manifest_io.check_destination(manifest, manifest["assets"][0])
        self.assertIn("許可されたルート", str(context.exception))

    def test_destination_without_whitelist_allowed(self):
        manifest = copy.deepcopy(self.base)
        manifest["destination"].pop("pathWhitelist")
        manifest["assets"][0]["destinationPath"] = "/Game/Anywhere"
        self.assertEqual(
            manifest_io.check_destination(manifest, manifest["assets"][0]),
            "/Game/Anywhere")

    def test_relative_destination_rejected(self):
        manifest = copy.deepcopy(self.base)
        manifest["assets"][0]["destinationPath"] = "Game/_Dev/X"
        with self.assertRaises(manifest_io.ManifestError):
            manifest_io.check_destination(manifest, manifest["assets"][0])

    def test_animation_sequence_is_skipped_not_rejected(self):
        manifest = copy.deepcopy(self.base)
        manifest["assets"].append({
            "id": "a002", "assetType": "AnimationSequence", "name": "A_Dummy_Idle",
            "destinationPath": "/Game/_Dev/X", "sourceFile": "A_Dummy_Idle.fbx",
            "animationSequence": {"skeletonAssetPath": "/Game/_Dev/X/SKEL_Dummy"},
        })
        with tempfile.TemporaryDirectory() as directory:
            path = _write_temp(manifest, directory)
            prepared = manifest_io.prepare(path)
        self.assertEqual(prepared["skipped"], ["a002"])
        self.assertTrue(any("未実装" in w for w in prepared["warnings"]))

    def test_missing_source_file_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _write_temp(self.base, directory)
            with self.assertRaises(manifest_io.ManifestError) as context:
                manifest_io.resolve_source_file(path, self.base["assets"][0])
            self.assertIn("中間ファイルが見つかりません", str(context.exception))

    def test_source_file_resolved_relative_to_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _write_temp(self.base, directory)
            fbx = os.path.join(directory, "SM_Crate_01.fbx")
            with open(fbx, "wb") as handle:
                handle.write(b"dummy")
            resolved = manifest_io.resolve_source_file(path, self.base["assets"][0])
            self.assertEqual(os.path.normcase(resolved), os.path.normcase(fbx))


if __name__ == "__main__":
    unittest.main(verbosity=2)
