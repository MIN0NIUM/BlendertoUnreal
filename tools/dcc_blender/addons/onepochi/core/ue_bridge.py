"""UE への取り込み依頼（Remote Control API 経由 / docs/02_仕様書.md §2.1）。

これが「真のワンポチ」の要。第1層（Blender -> アセット元データ）が終わったあと、
**起動中の UE エディタ**に第2層（アセット元データ -> UE）を実行させる。

    Blender                         UE エディタ
    ───────                         ──────────
    onepochi.json を書く
    HTTP PUT /remote/object/call ──> PythonScriptLibrary.ExecutePythonCommandEx
                                     └ onepochi_rc.handle(manifest, report)
                                        └ onepochi_ue.importer.import_from_manifest()
    ue_report.json を読む        <──  ue_report.json を書く

設計上の約束:
    - **結果は HTTP のレスポンスではなく `ue_report.json` で受け取る。**
      レスポンスの形に依存すると UE のバージョン差で壊れるため（docs/99 P-6 の教訓）。
      HTTP は「依頼が届いたか」の判定にだけ使う。
    - bpy に依存しない。`tests/test_core.py` から単体テストできる。
    - 標準ライブラリのみ。Blender の Python に requests は入っていない。

**UE 側の前提**: Remote Control API プラグインに加えて、
`docs/02_仕様書.md §2.1` の2つのセキュリティ設定が必要（既定では両方 OFF）。
失敗時は `classify_failure()` がどの設定が足りないかを日本語で返す。
"""

import json
import os
import socket

try:                                     # Python 3
    from urllib import error as urllib_error
    from urllib import request as urllib_request
except ImportError:                      # pragma: no cover - Blender は常に Python 3
    import urllib2 as urllib_request     # noqa: F401
    urllib_error = urllib_request        # noqa: F811

#: Remote Control API の既定の受け口（UE の Project Settings で変更可能）
DEFAULT_BASE_URL = "http://127.0.0.1:30010"

#: 関数呼び出しのルート
CALL_ROUTE = "/remote/object/call"

#: Python 実行を担う UE のオブジェクトと関数
PYTHON_LIBRARY_PATH = "/Script/PythonScriptPlugin.Default__PythonScriptLibrary"
PYTHON_FUNCTION = "ExecutePythonCommandEx"

#: UE 側の受け口モジュール（tools/ue_plugin/onepochi_rc.py）
RECEIVER_MODULE = "onepochi_rc"

#: 既定のタイムアウト（秒）。インポートはメッシュが重いと時間がかかる
DEFAULT_TIMEOUT = 180.0

#: 接続確認だけの短いタイムアウト
PING_TIMEOUT = 5.0


class BridgeError(Exception):
    """UE への依頼が失敗した。`message` は日本語でそのまま UI に出せる。

    `hint` には「ユーザーが何をすれば直るか」を入れる。
    """

    def __init__(self, message, hint=None, detail=None):
        super(BridgeError, self).__init__(message)
        self.message = message
        self.hint = hint
        self.detail = detail

    def lines(self):
        """UI / ログにそのまま出せる行のリスト。"""
        result = [self.message]
        if self.hint:
            result.extend("  " + line for line in self.hint.splitlines())
        if self.detail:
            result.append("  詳細: {0}".format(self.detail))
        return result

    def __str__(self):
        return "\n".join(self.lines())


# --- エラーの切り分け ---------------------------------------------------------

#: UE が返すエラー文の断片 -> (日本語の説明, 対処)
#: 断片は UE 5.8.3 の RemoteControlModule.cpp /
#: WebRemoteControlInternalUtils.cpp の文言に対応する（実測）
_FAILURE_PATTERNS = (
    (
        "cannot be accessed remotely",
        "UE の Remote Control で Python の実行が許可されていません。",
        "原因と対処を自動で切り分けます:\n"
        "    python tools/check_rc_config.py\n"
        "  設定を入れるには（Config/DefaultRemoteControl.ini を書きます）:\n"
        "    tools/setup_ue_project.bat \"<.uproject のパス>\"\n"
        "  手で入れるなら Config/DefaultRemoteControl.ini に\n"
        "  bEnableRemotePythonExecution=True を置くこと（docs/99 P-29）。\n"
        "  ※ DefaultEngine.ini に書いても読まれません\n"
        "  ※ Project Settings から入れても、エディタ再起動で失われます",
    ),
    (
        "is not allowed by remote control settings",
        "ExecutePythonCommandEx の呼び出しが許可リストに入っていません。",
        "原因と対処を自動で切り分けます:\n"
        "    python tools/check_rc_config.py\n"
        "  設定を入れるには（Config/DefaultRemoteControl.ini を書きます）:\n"
        "    tools/setup_ue_project.bat \"<.uproject のパス>\"\n"
        "  手で入れるなら Config/DefaultRemoteControl.ini に次の1行を置くこと:\n"
        "    +CustomAllowedRemoteFunctionCalls=("
        'ClassPath="/Script/PythonScriptPlugin.PythonScriptLibrary",'
        'FunctionName=("ExecutePythonCommandEx"),bAllowChildClasses=False)\n'
        "  ※ Allow Any Remote Function Call を ON にしても通りますが、"
        "許可範囲が広くなるので勧めません",
    ),
    (
        "Could not find object",
        "UE の Python プラグインが見つかりません。",
        "UE の Plugins で「Python Editor Script Plugin」を有効にしてください。",
    ),
)


def classify_failure(status, body):
    """HTTP のステータスと本文から `BridgeError` を作る。

    UE は失敗を 400 + `{"errorMessage": "..."}` で返す。
    文面から「どの設定が足りないか」を特定して日本語で返す。
    """
    text = body if isinstance(body, str) else str(body)
    message_from_ue = text
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            message_from_ue = parsed.get("errorMessage") or text
    except (ValueError, TypeError):
        pass

    for needle, message, hint in _FAILURE_PATTERNS:
        if needle.lower() in message_from_ue.lower():
            return BridgeError(message, hint, message_from_ue.strip())

    if status == 404:
        return BridgeError(
            "Remote Control API のルートが見つかりません（404）。",
            "UE の Plugins で「Remote Control API」が有効か確認してください。",
            message_from_ue.strip(),
        )
    return BridgeError(
        "UE が依頼を受け付けませんでした（HTTP {0}）。".format(status),
        None,
        message_from_ue.strip(),
    )


def _connection_error(base_url, error):
    return BridgeError(
        "UE に接続できませんでした（{0}）。".format(base_url),
        "次を確認してください:\n"
        "  1. Unreal Editor が起動しているか\n"
        "  2. Remote Control API プラグインが有効か\n"
        "  3. ポート番号が合っているか（既定 30010。"
        "Project Settings > Plugins > Remote Control で変更できます）",
        "{0}: {1}".format(type(error).__name__, error),
    )


# --- HTTP ---------------------------------------------------------------------


def post_json(url, payload, timeout=DEFAULT_TIMEOUT):
    """JSON を PUT して (status, 本文) を返す。接続できない場合は BridgeError。

    Remote Control API のルートは **PUT**（POST ではない）。
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib_request.Request(
        url, data=data, method="PUT",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib_request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib_error.HTTPError as error:
        body = ""
        try:
            body = error.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        raise classify_failure(error.code, body)
    except socket.timeout as error:
        raise BridgeError(
            "UE が {0:.0f} 秒以内に応答しませんでした。".format(timeout),
            "シェーダコンパイルや大きなメッシュの取り込みで時間がかかっている"
            "可能性があります。UE の Output Log を確認してください。",
            str(error),
        )
    except urllib_error.URLError as error:
        reason = getattr(error, "reason", error)
        if isinstance(reason, socket.timeout):
            raise BridgeError(
                "UE が {0:.0f} 秒以内に応答しませんでした。".format(timeout),
                "UE の Output Log を確認してください。", str(reason))
        raise _connection_error(url, reason)
    except OSError as error:
        raise _connection_error(url, error)


def is_reachable(base_url=DEFAULT_BASE_URL, timeout=PING_TIMEOUT):
    """Remote Control API が応答するかだけを確かめる（UI の状態表示用）。"""
    url = base_url.rstrip("/") + "/remote/info"
    try:
        request = urllib_request.Request(url, method="GET")
        with urllib_request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except Exception:  # noqa: BLE001
        return False


# --- Python 実行の依頼 --------------------------------------------------------


def execute_python(statement, base_url=DEFAULT_BASE_URL, timeout=DEFAULT_TIMEOUT):
    """UE 側で Python の文を実行する。(status, 本文) を返す。"""
    url = base_url.rstrip("/") + CALL_ROUTE
    payload = {
        "objectPath": PYTHON_LIBRARY_PATH,
        "functionName": PYTHON_FUNCTION,
        "parameters": {
            "pythonCommand": statement,
            "executionMode": "ExecuteStatement",
            "fileExecutionScope": "Private",
        },
        "generateTransaction": False,
    }
    return post_json(url, payload, timeout)


def _as_posix(path):
    return str(path).replace("\\", "/")


def build_import_statement(ue_plugin_dir, manifest_path, report_path=None,
                           dry_run=False, allow_failed=False):
    """UE 側に送る1行の Python 文を組み立てる。

    **1行（セミコロン区切り）に収める。**`if` などの複合文を混ぜると
    実行モードによって解釈が変わるため、分岐は入れない。
    `sys.path` への重複追加は無害なので毎回入れる。

    `importlib.reload` を入れているのは開発中にコードを変えたとき、
    UE を再起動せずに反映させるため。
    """
    parts = [
        "import sys, importlib",
        'sys.path.insert(0, r"{0}")'.format(_as_posix(ue_plugin_dir)),
        "import {0}".format(RECEIVER_MODULE),
        "importlib.reload({0})".format(RECEIVER_MODULE),
        '{0}.handle(r"{1}", r"{2}", {3}, {4})'.format(
            RECEIVER_MODULE,
            _as_posix(manifest_path),
            _as_posix(report_path or ""),
            bool(dry_run),
            bool(allow_failed),
        ),
    ]
    return "; ".join(parts)


def read_report(report_path):
    """`ue_report.json` を読む。無ければ None。"""
    if not report_path or not os.path.isfile(report_path):
        return None
    try:
        with open(report_path, "r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except Exception:  # noqa: BLE001
        return None


def request_import(ue_plugin_dir, manifest_path, report_path=None,
                   dry_run=False, allow_failed=False,
                   base_url=DEFAULT_BASE_URL, timeout=DEFAULT_TIMEOUT):
    """UE に取り込みを依頼し、`ue_report.json` の内容を返す。

    戻り値は dict:
        {"ok": bool, "report": dict or None, "messages": [str, ...]}

    HTTP が通っても **レポートが無ければ失敗扱い**にする。
    「依頼は届いたが中で落ちた」を成功と誤認しないため。
    """
    messages = []
    report_path = report_path or default_report_path(manifest_path)

    # 前回のレポートを消しておく。残っていると古い結果を成功と誤認する
    if os.path.isfile(report_path):
        try:
            os.remove(report_path)
        except OSError as error:
            messages.append("前回のレポートを削除できませんでした: {0}".format(error))

    statement = build_import_statement(ue_plugin_dir, manifest_path, report_path,
                                       dry_run, allow_failed)
    status, body = execute_python(statement, base_url, timeout)
    messages.append("UE への依頼は成功しました（HTTP {0}）".format(status))

    log_lines = _extract_log(body)
    if log_lines:
        messages.extend(log_lines)

    report = read_report(report_path)
    if report is None:
        raise BridgeError(
            "UE は依頼を受け付けましたが、レポートが生成されませんでした。",
            "UE の Output Log で [OnePochi] の行を確認してください。\n"
            "レポートの予定地: {0}".format(report_path),
            "\n".join(log_lines) if log_lines else None,
        )

    ok = report.get("result") in ("success", "dryRun")
    return {"ok": ok, "report": report, "messages": messages,
            "reportPath": report_path}


def default_plugin_dir():
    """`tools/ue_plugin` の絶対パスを返す（UE 側の受け口モジュールの置き場）。

    このファイルは `tools/dcc_blender/addons/onepochi/core/ue_bridge.py` にあるので、
    4つ上が `tools/`。配置を変えたらここも直すこと。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    tools = here
    for _ in range(4):
        tools = os.path.dirname(tools)
    return os.path.join(tools, "ue_plugin")


def settings_from_preset(settings):
    """プリセットの `ue` ブロックから接続設定を取り出す（D-2）。"""
    ue_settings = (settings or {}).get("ue") or {}
    return {
        "base_url": ue_settings.get("remoteControlUrl") or DEFAULT_BASE_URL,
        "timeout": float(ue_settings.get("timeoutSeconds") or DEFAULT_TIMEOUT),
        "plugin_dir": ue_settings.get("pluginDir") or default_plugin_dir(),
    }


def default_report_path(manifest_path):
    """マニフェストの隣に `ue_report.json` を置く。"""
    directory = os.path.dirname(os.path.abspath(manifest_path))
    return os.path.join(directory, "ue_report.json")


def _extract_log(body):
    """レスポンスから UE のログ行を取り出す（取れなければ空リスト）。

    レスポンスの形は UE のバージョンで変わりうるので、
    **取れなくても失敗にしない。**あくまで診断の補助。
    """
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, dict):
        return []

    lines = []
    for key in ("logOutput", "LogOutput"):
        entries = parsed.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict):
                text = entry.get("output") or entry.get("Output") or ""
                kind = entry.get("type") or entry.get("Type") or ""
            else:
                text = str(entry)
                kind = ""
            text = text.strip()
            if not text:
                continue
            lines.append("UE: {0}{1}".format(
                "[{0}] ".format(kind) if kind else "", text))
    for key in ("commandResult", "CommandResult"):
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            lines.append("UE: {0}".format(value.strip()))
    return lines
