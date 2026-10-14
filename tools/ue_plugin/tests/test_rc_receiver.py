"""Remote Control の受け口の単体テスト（unreal 不要 / P2）。

`onepochi_rc.handle()` は **例外を外に投げてはいけない。**
失敗しても必ずレポート JSON を書くのが契約（docs/99 P-6 / P-7）。
HTTP のレスポンスだけが頼りだと Blender 側で原因が分からなくなるため。

`unreal` が無い環境では `onepochi_ue.importer` の import が失敗するので、
**その失敗経路がここで検証できる。**UE を起動する必要はない。

実行:
    python tools/ue_plugin/tests/test_rc_receiver.py
"""

import io
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

import onepochi_rc  # noqa: E402


def _force_utf8_stdout():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_stdout()

SAMPLE_MANIFEST = os.path.join(
    PROJECT_ROOT, "samples", "blender", "01_static_minimal", "fbx", "onepochi.json")

#: `unreal` が import できる環境（= UE 内）ではスキップする判定
try:
    import unreal  # noqa: F401
    INSIDE_UE = True
except ImportError:
    INSIDE_UE = False


def _read(path):
    with io.open(path, encoding="utf-8") as handle:
        return json.load(handle)


class TestContract(unittest.TestCase):
    """どんな入力でも「例外を投げず、レポートを書く」契約を守るか。"""

    def test_missing_manifest_writes_failure_report(self):
        with tempfile.TemporaryDirectory() as directory:
            report_path = os.path.join(directory, "ue_report.json")
            result = onepochi_rc.handle(
                os.path.join(directory, "no_such.json"), report_path)
            self.assertEqual(result["result"], "failed")
            self.assertTrue(os.path.isfile(report_path),
                            "レポートが書かれていない")
            report = _read(report_path)
            self.assertEqual(report["result"], "failed")
            self.assertTrue(report["errors"])
            self.assertIn("マニフェストが見つかりません", report["errors"][0])

    def test_empty_manifest_path_does_not_raise(self):
        with tempfile.TemporaryDirectory() as directory:
            report_path = os.path.join(directory, "ue_report.json")
            result = onepochi_rc.handle("", report_path)
            self.assertEqual(result["result"], "failed")

    def test_report_path_may_be_omitted_as_empty_string(self):
        """Remote Control からは引数を省略できないので空文字で渡す。"""
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = os.path.join(directory, "onepochi.json")
            # 存在しないファイルを指すが、レポートは隣に書かれるはず
            result = onepochi_rc.handle(manifest_path, "")
            self.assertEqual(result["result"], "failed")
            expected = os.path.join(directory, "ue_report.json")
            self.assertTrue(os.path.isfile(expected),
                            "レポートがマニフェストの隣に書かれていない")

    def test_report_directory_is_created(self):
        with tempfile.TemporaryDirectory() as directory:
            report_path = os.path.join(directory, "deep", "nested", "ue_report.json")
            onepochi_rc.handle(os.path.join(directory, "no_such.json"), report_path)
            self.assertTrue(os.path.isfile(report_path),
                            "レポート用のフォルダが作られていない")

    def test_report_is_marked_as_remote_control(self):
        with tempfile.TemporaryDirectory() as directory:
            report_path = os.path.join(directory, "ue_report.json")
            onepochi_rc.handle(os.path.join(directory, "no_such.json"), report_path)
            self.assertEqual(_read(report_path)["trigger"], "remoteControl")

    def test_report_has_the_expected_shape(self):
        """Blender 側は result / errors / warnings / assets だけ見れば済む。"""
        with tempfile.TemporaryDirectory() as directory:
            report_path = os.path.join(directory, "ue_report.json")
            onepochi_rc.handle(os.path.join(directory, "no_such.json"), report_path)
            report = _read(report_path)
            for key in ("result", "errors", "warnings", "assets"):
                self.assertIn(key, report)
            self.assertIsInstance(report["errors"], list)
            self.assertIsInstance(report["warnings"], list)
            self.assertIsInstance(report["assets"], list)

    def test_report_is_utf8_json(self):
        """日本語のエラーメッセージが読める形で書かれているか（Q-18）。"""
        with tempfile.TemporaryDirectory() as directory:
            report_path = os.path.join(directory, "ue_report.json")
            onepochi_rc.handle(os.path.join(directory, "no_such.json"), report_path)
            with io.open(report_path, encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn("マニフェスト", text)
            # ensure_ascii=False で書いているのでエスケープされていない
            self.assertNotIn("\\u30de", text)


@unittest.skipIf(INSIDE_UE, "UE 内では importer の import が成功するため対象外")
class TestWithoutUnreal(unittest.TestCase):
    """`unreal` が無い環境（= 素の Python）での失敗経路。"""

    def setUp(self):
        if not os.path.isfile(SAMPLE_MANIFEST):
            raise unittest.SkipTest(
                "サンプルのマニフェストがありません: " + SAMPLE_MANIFEST)

    def test_importer_import_failure_is_reported_in_japanese(self):
        with tempfile.TemporaryDirectory() as directory:
            report_path = os.path.join(directory, "ue_report.json")
            result = onepochi_rc.handle(SAMPLE_MANIFEST, report_path)
            self.assertEqual(result["result"], "failed")
            report = _read(report_path)
            joined = " ".join(report["errors"])
            self.assertIn("onepochi_ue", joined)
            self.assertIn("読み込みに失敗", joined)

    def test_sys_path_is_reported_for_diagnosis(self):
        """「なぜ import できないか」を切り分けられる情報を残す（docs/99 の教訓）。"""
        with tempfile.TemporaryDirectory() as directory:
            report_path = os.path.join(directory, "ue_report.json")
            onepochi_rc.handle(SAMPLE_MANIFEST, report_path)
            joined = " ".join(_read(report_path)["errors"])
            self.assertIn("sys.path", joined)


class TestPing(unittest.TestCase):
    def test_ping_returns_true(self):
        self.assertTrue(onepochi_rc.ping())


if __name__ == "__main__":
    unittest.main(verbosity=2)
