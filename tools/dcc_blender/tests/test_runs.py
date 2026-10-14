"""実行ログの保持（`core/runs.py`）の単体テスト（bpy 不要）。

掃除の判定は**時刻を注入**して検証する。実時間に依存させない。

実行:
    python tools/dcc_blender/tests/test_runs.py
"""

import datetime
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DCC_ROOT = os.path.dirname(HERE)
ADDON_ROOT = os.path.join(DCC_ROOT, "addons")
if ADDON_ROOT not in sys.path:
    sys.path.insert(0, ADDON_ROOT)

from onepochi.core import manifest as manifest_mod  # noqa: E402
from onepochi.core import runs  # noqa: E402


def _force_utf8_stdout():
    """Windows の既定コンソール（cp932）でも日本語を出せるようにする（docs/99 P-9）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_stdout()


def _run_id(stamp):
    return stamp.strftime("%Y%m%d_%H%M%S") + "_abc123"


class TestRunId(unittest.TestCase):
    def test_parses_the_id_that_manifest_actually_makes(self):
        """`make_run_id()` の形式に追従していること。

        形式を変えたらこのテストが落ちる（二重管理の検出）。
        """
        now = datetime.datetime(2026, 10, 11, 1, 30, 0)
        run_id = manifest_mod.make_run_id(now=now)
        parsed = runs.parse_run_id_time(run_id)
        self.assertEqual(parsed, now)

    def test_unparsable_returns_none(self):
        for value in ("", None, "latest", "手で作ったフォルダ", "2026"):
            self.assertIsNone(runs.parse_run_id_time(value))


class TestRecord(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._directory.name, "runs")

    def tearDown(self):
        self._directory.cleanup()

    def test_record_writes_manifest_result_and_latest(self):
        run_id = _run_id(datetime.datetime(2026, 10, 11, 1, 30, 0))
        manifest_path = os.path.join(self._directory.name, "onepochi.json")
        with io.open(manifest_path, "w", encoding="utf-8") as handle:
            handle.write("{}")

        directory = runs.record(run_id, manifest={"runId": run_id},
                                manifest_path=manifest_path,
                                result={"result": "success"}, root=self.root)
        self.assertIsNotNone(directory)
        self.assertTrue(os.path.isfile(os.path.join(directory, runs.MANIFEST_COPY_NAME)))
        self.assertTrue(os.path.isfile(os.path.join(directory, runs.RESULT_NAME)))

        with io.open(os.path.join(directory, runs.RESULT_NAME),
                     encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["runId"], run_id)
        self.assertEqual(os.path.normcase(payload["manifestPath"]),
                         os.path.normcase(os.path.abspath(manifest_path)))

        latest = runs.read_latest(self.root)
        self.assertEqual(latest["runId"], run_id)
        self.assertEqual(os.path.normcase(latest["manifestPath"]),
                         os.path.normcase(os.path.abspath(manifest_path)))

    def test_record_does_not_raise_on_bad_root(self):
        """ログの保存に失敗しても書き出しを失敗にしないこと（None を返すだけ）。"""
        blocker = os.path.join(self._directory.name, "blocked")
        with io.open(blocker, "w", encoding="utf-8") as handle:
            handle.write("ファイルなのでディレクトリは作れない")
        result = runs.record(_run_id(datetime.datetime(2026, 10, 11, 1, 0, 0)),
                             root=blocker)
        self.assertIsNone(result)

    def test_read_latest_missing_returns_none(self):
        self.assertIsNone(runs.read_latest(self.root))


class TestPrune(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._directory.name, "runs")
        self.now = datetime.datetime(2026, 10, 11, 1, 30, 0)
        self.ids = {}
        for label, days in (("today", 0), ("d10", 10), ("d29", 29),
                            ("d31", 31), ("d90", 90)):
            stamp = self.now - datetime.timedelta(days=days)
            run_id = _run_id(stamp)
            self.ids[label] = run_id
            runs.record(run_id, result={"label": label}, root=self.root)

    def tearDown(self):
        self._directory.cleanup()

    def test_lists_newest_first(self):
        listed = runs.list_runs(self.root)
        self.assertEqual(listed[0], self.ids["today"])
        self.assertEqual(listed[-1], self.ids["d90"])

    def test_prunes_only_older_than_retention(self):
        removed = runs.prune(retention_days=30, root=self.root, now=self.now)
        self.assertEqual(sorted(removed),
                         sorted([self.ids["d31"], self.ids["d90"]]))
        remaining = runs.list_runs(self.root)
        self.assertEqual(sorted(remaining),
                         sorted([self.ids["today"], self.ids["d10"],
                                 self.ids["d29"]]))

    def test_keeps_minimum_even_when_everything_expired(self):
        """全部期限切れでも最低1件は残す（調査材料を失わない）。"""
        far_future = self.now + datetime.timedelta(days=365)
        removed = runs.prune(retention_days=30, root=self.root, now=far_future)
        self.assertEqual(len(removed), 4)
        self.assertEqual(runs.list_runs(self.root), [self.ids["today"]])

    def test_ignores_directories_that_are_not_run_ids(self):
        """人が置いたフォルダを消さないこと。"""
        hand_made = os.path.join(self.root, "調査用メモ")
        os.makedirs(hand_made)
        runs.prune(retention_days=0, root=self.root, now=self.now)
        self.assertTrue(os.path.isdir(hand_made))

    def test_latest_json_is_not_treated_as_a_run(self):
        self.assertNotIn(runs.LATEST_NAME, runs.list_runs(self.root))


class TestRunsRoot(unittest.TestCase):
    def test_env_overrides_default(self):
        saved = os.environ.get(runs.ROOT_ENV)
        try:
            os.environ[runs.ROOT_ENV] = r"C:\somewhere\else"
            self.assertEqual(runs.runs_root(), r"C:\somewhere\else")
            self.assertEqual(runs.runs_root(r"C:\explicit"), r"C:\explicit")
        finally:
            if saved is None:
                os.environ.pop(runs.ROOT_ENV, None)
            else:
                os.environ[runs.ROOT_ENV] = saved

    def test_default_is_under_localappdata_on_windows(self):
        saved = os.environ.pop(runs.ROOT_ENV, None)
        try:
            resolved = runs.runs_root()
            self.assertTrue(resolved.endswith(os.path.join("OnePochi", "runs"))
                            or resolved.endswith(os.path.join(".onepochi", "runs")),
                            resolved)
        finally:
            if saved is not None:
                os.environ[runs.ROOT_ENV] = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
