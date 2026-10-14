"""`tools/setup_ue_project.py`（UE プロジェクトへの設定投入）の単体テスト。UE / Blender 不要。

守りたいこと:
    - 既存の内容を残す（他のセクション・他のキー・改行コード・BOM）
    - 2回流しても変わらない（冪等）
    - **緩めない**（安全側の設定が別の値でも書き換えない）
    - 書いた結果が `check_rc_config.py` の点検を通る（二重管理のずれを検出する）

実行:
    python tools/tests/test_setup_ue_project.py
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import check_rc_config  # noqa: E402
import setup_ue_project as setup  # noqa: E402


def _force_utf8_stdout():
    """Windows の既定コンソール（cp932）でも日本語を出せるようにする（docs/99 P-9）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_stdout()


def _effective(text, key):
    """UE と同じく**最後の行**を採用した値を返す（`+` 配列は全部）。"""
    in_section = False
    values = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            in_section = line[1:-1] == setup.SECTION
            continue
        if in_section and "=" in line and not line.startswith(";"):
            name, _, value = line.partition("=")
            if name.strip().lstrip("+-.!") == key:
                values.append(value.strip())
    return values


class TestMergeIni(unittest.TestCase):
    def test_new_file_has_everything_needed(self):
        new, changes, warnings = setup.merge_remote_control_ini(None)
        self.assertEqual(_effective(new, "bEnableRemotePythonExecution"), ["True"])
        self.assertEqual(_effective(new, "bAllowAnyRemoteFunctionCall"), ["False"])
        allow = _effective(new, "CustomAllowedRemoteFunctionCalls")
        self.assertEqual(len(allow), 1)
        self.assertIn("ExecutePythonCommandEx", allow[0])
        self.assertTrue(changes)
        self.assertEqual(warnings, [])

    def test_idempotent(self):
        first, _, _ = setup.merge_remote_control_ini(None)
        second, changes, warnings = setup.merge_remote_control_ini(first)
        self.assertEqual(second, first)
        self.assertEqual(changes, [])
        self.assertEqual(warnings, [])

    def test_keeps_other_sections_and_inserts_inside_our_section(self):
        original = (
            "[/Script/Engine.Engine]\n"
            "FooBar=1\n"
            "\n"
            "[" + setup.SECTION + "]\n"
            "SomethingElse=42\n"
            "\n"
            "[/Script/Other.After]\n"
            "Baz=2\n")
        new, _, _ = setup.merge_remote_control_ini(original)
        self.assertIn("[/Script/Engine.Engine]\nFooBar=1\n", new)
        self.assertIn("SomethingElse=42", new)
        self.assertIn("[/Script/Other.After]\nBaz=2\n", new)
        # 追加した行は、後ろの別セクションではなく自分のセクションに入っている
        ours_start = new.index("[" + setup.SECTION + "]")
        after_start = new.index("[/Script/Other.After]")
        block = new[ours_start:after_start]
        self.assertIn("bEnableRemotePythonExecution=True", block)
        self.assertIn("ExecutePythonCommandEx", block)
        self.assertNotIn("bEnableRemotePythonExecution", new[after_start:])

    def test_preserves_crlf(self):
        original = "[" + setup.SECTION + "]\r\nSomethingElse=42\r\n"
        new, _, _ = setup.merge_remote_control_ini(original)
        self.assertIn("\r\n", new)
        self.assertNotIn("\n", new.replace("\r\n", ""))

    def test_required_key_is_corrected_in_place(self):
        original = "[" + setup.SECTION + "]\nbEnableRemotePythonExecution=False\n"
        new, changes, _ = setup.merge_remote_control_ini(original)
        self.assertEqual(_effective(new, "bEnableRemotePythonExecution"), ["True"])
        self.assertEqual(new.count("bEnableRemotePythonExecution"), 1)
        self.assertTrue(any("False → True" in item for item in changes))

    def test_last_duplicate_wins_like_ue(self):
        original = ("[" + setup.SECTION + "]\n"
                    "bEnableRemotePythonExecution=True\n"
                    "bEnableRemotePythonExecution=False\n")
        new, _, _ = setup.merge_remote_control_ini(original)
        self.assertEqual(_effective(new, "bEnableRemotePythonExecution")[-1], "True")

    def test_never_loosens_or_overwrites_safety_settings(self):
        original = ("[" + setup.SECTION + "]\n"
                    "bAllowAnyRemoteFunctionCall=True\n"
                    "bRestrictServerAccess=False\n")
        new, _, warnings = setup.merge_remote_control_ini(original)
        self.assertEqual(_effective(new, "bAllowAnyRemoteFunctionCall"), ["True"])
        self.assertEqual(_effective(new, "bRestrictServerAccess"), ["False"])
        joined = "\n".join(warnings)
        self.assertIn("bAllowAnyRemoteFunctionCall", joined)
        self.assertIn("bRestrictServerAccess", joined)

    def test_existing_allow_line_with_other_formatting_is_not_duplicated(self):
        original = (
            "[" + setup.SECTION + "]\n"
            "bEnableRemotePythonExecution=True\n"
            '+CustomAllowedRemoteFunctionCalls=(FunctionName=("Foo","ExecutePythonCommandEx"),'
            'ClassPath="/Script/PythonScriptPlugin.PythonScriptLibrary")\n')
        new, changes, _ = setup.merge_remote_control_ini(original)
        self.assertEqual(len(_effective(new, "CustomAllowedRemoteFunctionCalls")), 1)
        self.assertFalse(any("CustomAllowedRemoteFunctionCalls" in item for item in changes))

    def test_unrelated_allow_line_does_not_count(self):
        original = (
            "[" + setup.SECTION + "]\n"
            '+CustomAllowedRemoteFunctionCalls=(ClassPath="/Script/Engine.Foo",'
            'FunctionName=("Bar"))\n')
        new, _, _ = setup.merge_remote_control_ini(original)
        self.assertEqual(len(_effective(new, "CustomAllowedRemoteFunctionCalls")), 2)

    def test_appends_section_to_file_without_it(self):
        original = "[/Script/Engine.Engine]\nFooBar=1"  # 末尾改行なし
        new, _, _ = setup.merge_remote_control_ini(original)
        self.assertTrue(new.startswith("[/Script/Engine.Engine]\nFooBar=1\n\n["))
        self.assertTrue(new.endswith("\n"))


class TestMergeUproject(unittest.TestCase):
    def _text(self, plugins):
        return json.dumps({"FileVersion": 3, "EngineAssociation": "5.8",
                           "Plugins": plugins}, indent="\t")

    def test_adds_missing_plugins(self):
        new, changes = setup.merge_uproject(self._text([{"Name": "Other", "Enabled": True}]))
        names = {item["Name"]: item["Enabled"] for item in json.loads(new)["Plugins"]}
        self.assertEqual(names, {"Other": True, "PythonScriptPlugin": True, "RemoteControl": True})
        self.assertEqual(len(changes), 2)

    def test_enables_disabled_plugin_without_duplicating(self):
        text = self._text([{"Name": "RemoteControl", "Enabled": False}])
        new, _ = setup.merge_uproject(text)
        entries = [item for item in json.loads(new)["Plugins"] if item["Name"] == "RemoteControl"]
        self.assertEqual(entries, [{"Name": "RemoteControl", "Enabled": True}])

    def test_unchanged_text_is_returned_as_is(self):
        text = self._text([{"Name": "PythonScriptPlugin", "Enabled": True},
                           {"Name": "RemoteControl", "Enabled": True}])
        new, changes = setup.merge_uproject(text + "   ")  # 再整形されないことを空白で確かめる
        self.assertEqual(new, text + "   ")
        self.assertEqual(changes, [])

    def test_creates_plugins_array_when_missing(self):
        new, _ = setup.merge_uproject('{"FileVersion": 3}')
        self.assertEqual(len(json.loads(new)["Plugins"]), 2)

    def test_invalid_json_is_an_error(self):
        with self.assertRaises(setup.SetupError):
            setup.merge_uproject("{ not json")


class TestResolve(unittest.TestCase):
    def test_file_and_folder(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "My.uproject")
            open(path, "w").close()
            self.assertEqual(setup.resolve_uproject(path), path)
            self.assertEqual(setup.resolve_uproject(root), path)

    def test_errors(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(setup.SetupError):
                setup.resolve_uproject(root)                        # 0 個
            for name in ("A.uproject", "B.uproject"):
                open(os.path.join(root, name), "w").close()
            with self.assertRaises(setup.SetupError):
                setup.resolve_uproject(root)                        # 複数
            with self.assertRaises(setup.SetupError):
                setup.resolve_uproject(os.path.join(root, "nope"))  # 無い
            with self.assertRaises(setup.SetupError):
                setup.resolve_uproject("")                          # 空


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = self._temp.name
        self.uproject = os.path.join(self.root, "Proj.uproject")
        with open(self.uproject, "w", encoding="utf-8") as handle:
            handle.write('{\n\t"FileVersion": 3,\n\t"Plugins": []\n}')
        self.ini = os.path.join(self.root, "Config", "DefaultRemoteControl.ini")

    def tearDown(self):
        self._temp.cleanup()

    def _run(self, *args):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = setup.main([self.uproject] + list(args))
        return code, buffer.getvalue()

    def test_dry_run_writes_nothing(self):
        code, output = self._run("--dry-run")
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.ini))
        self.assertEqual(os.listdir(self.root), ["Proj.uproject"])
        self.assertIn("ExecutePythonCommandEx", output)

    def test_apply_creates_ini_and_enables_plugins(self):
        code, _ = self._run("--yes")
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(self.ini))
        with open(self.uproject, encoding="utf-8") as handle:
            names = [item["Name"] for item in json.load(handle)["Plugins"]]
        self.assertEqual(sorted(names), ["PythonScriptPlugin", "RemoteControl"])

    def test_result_passes_the_checker(self):
        """書いた結果が `check_rc_config.py` の点検を通る。キーの二重管理のずれを検出する。"""
        self._run("--yes")
        saved = check_rc_config.PROJECT
        try:
            check_rc_config.set_project(self.root)
            _, problems, _ = check_rc_config.inspect_inis()
        finally:
            check_rc_config.set_project(saved)
        self.assertEqual(problems, [])

    def test_existing_file_is_backed_up_and_second_run_is_noop(self):
        os.makedirs(os.path.dirname(self.ini))
        with open(self.ini, "w", encoding="utf-8") as handle:
            handle.write("[" + setup.SECTION + "]\nbEnableRemotePythonExecution=False\n")
        self._run("--yes")
        backups = [name for name in os.listdir(os.path.dirname(self.ini)) if ".bak-" in name]
        self.assertEqual(len(backups), 1)
        with open(os.path.join(os.path.dirname(self.ini), backups[0]), encoding="utf-8") as handle:
            self.assertIn("bEnableRemotePythonExecution=False", handle.read())

        with open(self.ini, encoding="utf-8") as handle:
            after_first = handle.read()
        code, output = self._run("--yes")
        self.assertEqual(code, 0)
        self.assertIn("すでに設定済み", output)
        with open(self.ini, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), after_first)
        backups_after = [name for name in os.listdir(os.path.dirname(self.ini)) if ".bak-" in name]
        self.assertEqual(len(backups_after), 1)  # 変更が無ければバックアップも増やさない

    def test_no_plugins_leaves_uproject_untouched(self):
        with open(self.uproject, encoding="utf-8") as handle:
            before = handle.read()
        self._run("--yes", "--no-plugins")
        with open(self.uproject, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), before)

    def test_bom_is_preserved(self):
        os.makedirs(os.path.dirname(self.ini))
        with open(self.ini, "wb") as handle:
            handle.write(b"\xef\xbb\xbf[" + setup.SECTION.encode() + b"]\nA=1\n")
        self._run("--yes", "--no-plugins")
        with open(self.ini, "rb") as handle:
            self.assertTrue(handle.read().startswith(b"\xef\xbb\xbf"))

    def test_non_utf8_ini_is_refused_and_left_alone(self):
        os.makedirs(os.path.dirname(self.ini))
        raw = "; 設定\n[A]\nB=1\n".encode("cp932")
        with open(self.ini, "wb") as handle:
            handle.write(raw)
        code, _ = self._run("--yes")
        self.assertEqual(code, 2)
        with open(self.ini, "rb") as handle:
            self.assertEqual(handle.read(), raw)

    def test_saved_override_is_reported_but_not_modified(self):
        saved = os.path.join(self.root, "Saved", "Config", "WindowsEditor")
        os.makedirs(saved)
        saved_ini = os.path.join(saved, "RemoteControl.ini")
        content = "[" + setup.SECTION + "]\nbEnableRemotePythonExecution=False\n"
        with open(saved_ini, "w", encoding="utf-8") as handle:
            handle.write(content)
        _, output = self._run("--yes", "--no-plugins")
        self.assertIn("Saved 側", output)
        with open(saved_ini, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
