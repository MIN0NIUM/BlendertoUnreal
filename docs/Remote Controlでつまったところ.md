# Remote Control でつまったところ — DCC → UE 自動化の最初の関門

- 作成: 2026-10-14
- 環境: **Unreal Engine 5.8.3** / Windows 11 / Blender 5.2 LTS
- 位置づけ: ブログ記事の下書き素材。**すべて実測とエンジンのソースで裏を取った内容**のみ記載する
- 関連: [docs/99 P-29 / P-32](99_作業ルール.md) / [docs/12 §2.6](12_実装状況.md) / `tools/check_rc_config.py`

> **この記事で一番言いたいこと**
> `.ini` に設定を書いたのに効かないときは、**キーの綴りより先に「ファイル名」を疑う**。
> UE の設定クラスは `UCLASS(config = Foo)` で読み込み先が決まり、
> `Foo` が `Engine` でなければ **`DefaultEngine.ini` に書いても無言で無視される**。

---

## 1. Remote Control とは何か

**起動中の UE エディタを、外部から HTTP で操作するための公式プラグイン**（Virtual Production 向け）。
既定で `http://127.0.0.1:30010` に REST サーバが立つ。

| ルート | 用途 |
|---|---|
| `GET /remote/info` | 使えるルートの一覧（疎通確認に最適） |
| `PUT /remote/object/property` | UObject のプロパティを読み書き |
| `PUT /remote/object/call` | UObject の関数を呼ぶ |
| `GET /remote/presets` | Remote Control プリセットの一覧 |

### なぜ DCC → UE の自動化でこれが要るのか

Blender から UE へワンボタンで取り込むツールを作ると、UE 側の処理を**誰が起動するか**で3通りある。

| 経路 | 長所 | 短所 |
|---|---|---|
| エディタ内のボタン | 確実 | **人が UE を触る必要がある**＝ワンボタンにならない |
| commandlet（`UnrealEditor-Cmd.exe`） | CI で回せる | **起動中のエディタと競合する**。エディタ専用サブシステムが取れないことがある |
| **Remote Control** | **アーティストが開いているエディタにそのまま届く** | **セキュリティ設定の壁がある** ← 今回の話 |

アーティストは UE を**開きっぱなし**で作業する。そこへ Blender から送り込むなら Remote Control 一択になる。
そして本命は `PythonScriptLibrary.ExecutePythonCommandEx` — **UE 側の Python をリモートで1行実行する**ための関数で、
これさえ通れば、あとは UE 側の Python モジュールに処理を全部書ける。

> **この壁は「UE を外から自動化する人は全員通る」**と言ってよい。
> Remote Control の Python 実行は**既定で全部 OFF**で、しかも拒否のされ方が複数ある。
> ただし後述の §4.4 だけは今回固有の未解明事項で、誰もが踏むとは限らない。

---

## 2. つまったところ（時系列）

### 症状: 昨日まで動いていたものが、エディタを再起動したら動かなくなった

```
HTTP 400
{"errorMessage": "Object Default__PythonScriptLibrary cannot be accessed remotely,
                  check remote control project settings."}
```

Project Settings から手で設定を入れ直すと直る。**しかし再起動するとまた消える。**
これを毎朝やるのは論外なので、恒久対策を探すことにした。

ここから**都合3回、見立てを外した**。順に書く。

| # | 見立て | 実際 |
|---|---|---|
| 1 | クライアント許可リスト（`AllowlistedClients`）が `127.0.0.1` を弾いている | **シロ**。プロパティ読みは通っていた |
| 2 | 「セキュリティ設定を確認済み」フラグ（`bSecuritySettingsReviewed`）が false だから他が効かない | **シロ**。ソース全文検索で、宣言以外に**参照が1つも無い** |
| 3 | `Config/DefaultEngine.ini` に書けば恒久化できる | **そのファイルは読まれない**（本命の原因） |

---

## 3. 切り分けのやり方 — **エラー文の違いが最良の手がかり**

「拒否された」で止まらず、**対象を変えて同じ操作を試す**と、どのゲートで落ちているかが分かる。
4段階のプローブを用意した。

| 段 | 試すこと | 通れば分かること |
|---|---|---|
| ① | `GET /remote/info` | サーバは生きている |
| ② | 適当なオブジェクトの**プロパティ読み** | **クライアント許可リストとパスフレーズはシロ** |
| ③ | **Python でない関数**の呼び出し | 関数許可リストの状態 |
| ④ | `ExecutePythonCommandEx` | 本命 |

実測結果（対策前）:

```
① GET /remote/info                     → 200
② EditorStyleSettings.SelectionColor   → 200
③ KismetSystemLibrary.GetEngineVersion → 400 "Executing function ... is not allowed by
                                               remote control settings."
④ ExecutePythonCommandEx               → 400 "Object Default__PythonScriptLibrary
                                               cannot be accessed remotely"
```

**③と④で文面が違う**のが決定的だった。

- ③は「**関数**が許可リストに無い」
- ④は「**オブジェクト**に遠隔アクセスできない」＝ **もっと手前の、Python 専用のゲート**

つまり④は関数許可リストの問題では**ない**。別のフラグが原因だと、この1点で確定した。

②が通ったことで、見立て1（許可リスト）と「パスフレーズ」の2つも同時に消えた。
**1回のプローブで容疑者が2人減る**ので、順番に試すより早い。

---

## 4. 原因 — ソースで裏を取る

UE 5.8 はエンジンのソースが手元にある（`Engine/Plugins/VirtualProduction/RemoteControl/`）。
**推測で断定せず、ここを読んだ。**

### 4.1 ④の拒否条件は、たった1つのフラグ

```cpp
// RemoteControl/Source/RemoteControl/Private/RemoteControlModule.cpp:2563
bool FRemoteControlModule::CanBeAccessedRemotely(UObject* Object) const
{
    if (!GetDefault<URemoteControlSettings>()->bEnableRemotePythonExecution)
    {
        static const FName PythonScriptName = "PythonScriptLibrary";
        static const FName PythonScriptSettingsName = "PythonScriptPluginSettings";
        const FName ObjectClassName = Object->GetClass()->GetFName();
        if (ObjectClassName == PythonScriptName || ObjectClassName == PythonScriptSettingsName)
        {
            return false;      // ← ④のエラーはここだけから出る
        }
    }
    if (Object == GetDefault<URemoteControlSettings>())
    {
        return false;          // 設定オブジェクト自身は遠隔から読めない
    }
    return true;
}
```

**`bEnableRemotePythonExecution` 以外は見ていない。**
ついでに分かったこと: **設定オブジェクト自身が遠隔アクセス拒否**なので、
「実行時に設定がどうなっているか」を Remote Control から読むことは**できない**。
だからソースを読むのが最短だった。

### 4.2 `ExecutePythonCommandEx` は既定の許可リストに入っていない

```cpp
// RemoteControlSettings.cpp:25 — 既定で許可される関数（Epic Stage App 用の11件）
AllowedRemoteFunctionCalls = { /* StageAppFunctionLibrary, Actor::SetActorLabel, ... */ };
```

Python 関数は1つも入っていない。だから `CustomAllowedRemoteFunctionCalls` に**名指しで足す**必要がある。
**③のゲートと④のゲートは別物なので、両方通さないと動かない。**

### 4.3 本命 — **`DefaultEngine.ini` に書いても読まれない**

```cpp
// RemoteControlSettings.h:180
UCLASS(config = RemoteControl)
class REMOTECONTROLCOMMON_API URemoteControlSettings : public UDeveloperSettings
```

UE の設定クラスは `UCLASS(config = <名前>)` で**読み込むファイル群**が決まる。
`config = RemoteControl` なら読まれるのは **`RemoteControl.ini` 階層だけ**:

```
{ENGINE}/Config/BaseRemoteControl.ini
{PROJECT}/Config/DefaultRemoteControl.ini      ← ここに書く（配布される / Git に入る）
{PROJECT}/Config/Windows/WindowsRemoteControl.ini
{PROJECT}/Saved/Config/WindowsEditor/RemoteControl.ini   ← UI で入れるとここ（後勝ち / ユーザー単位）
```

階層の定義は `Engine/Source/Runtime/Core/Public/Misc/ConfigHierarchy.h` の
`GConfigLayers` / `GPluginLayers`（プラグイン用）にベタ書きされている。**どちらにも
`{PROJECT}/Config/Default{TYPE}.ini` は含まれる**ので、プラグインの設定でもプロジェクト側に固定できる。

**`DefaultEngine.ini` に `[/Script/RemoteControlCommon.RemoteControlSettings]` を書いても、
エラーも警告も出ずに完全に無視される。**これが一番たちが悪かった。

> **手がかりは最初から目の前にあった。**
> UI で設定を入れたとき、UE はそれを
> `Saved/Config/WindowsEditor/**RemoteControl**.ini` に書き出していた。
> このファイル名は `UCLASS(config = ...)` の指定から作られる。
> **つまり「Saved 側のファイル名」が、そのまま「Default 側のファイル名」を教えていた。**

### 4.4 やり残し — 1キーだけ project ini から効かない（**未解明**）

`Config/DefaultRemoteControl.ini` に6項目を書いた結果、再起動後の Project Settings はこうなった。

| 設定 | UE の既定 | 再起動後 | 判定 |
|---|---|---|---|
| Restrict Server Access | false | **✔ 入った** | ini が効いている |
| Custom Allowed Remote Function Calls | 空 | **1件入った** | ini が効いている |
| **Enable Remote Python Execution** | false | **入らなかった** | ← これだけ効かない |

同じファイル・同じセクション・同じ書式・同じ大文字小文字。**この1キーだけ**反映されない。
次を潰したが、どれも原因ではなかった:

- 別プロジェクトが起動していた → **違う**（`GeneralProjectSettings.ProjectID` を RC で読んで照合）
- 上位の層が上書き → **違う**（`UserRemoteControl.ini` などは存在せず、全 `.ini` を走査しても記述なし）
- プラグイン階層から漏れている → **違う**（`GPluginLayers` にも project 層がある）
- CoreRedirect でキー名が変わった → **違う**
- 宣言が特殊 → **違う**（`bRestrictServerAccess` と同じ `UPROPERTY(Config, EditAnywhere)`）
- コードが false に戻している → **見つからず**

**UE 5.8 の挙動として未解明。断定しない。**
実用上は §5 の運用で困らないので、ここで止めた。

---

## 5. 対策

### 5.1 プロジェクト側に固定する（Git に入る / チーム全員に効く）

`<Project>/Config/DefaultRemoteControl.ini` を**新規作成**する。

```ini
[/Script/RemoteControlCommon.RemoteControlSettings]
bEnableRemotePythonExecution=True
bRestrictServerAccess=True
bAllowAnyRemoteFunctionCall=False
bAllowConsoleCommandRemoteExecution=False
bSecuritySettingsReviewed=True
+CustomAllowedRemoteFunctionCalls=(ClassPath="/Script/PythonScriptPlugin.PythonScriptLibrary",FunctionName=("ExecutePythonCommandEx"),bAllowChildClasses=False)
```

**緩めていない点**（ここは手を抜かないほうがいい）:

| 設定 | 理由 |
|---|---|
| `bAllowAnyRemoteFunctionCall=False` | True にすると**何でも呼べる**。許可は `ExecutePythonCommandEx` の1件だけに絞る |
| `bRestrictServerAccess=True` | 緩めない側。**`127.0.0.1` は許可リストの判定対象外**なので自分のアクセスは通る（`RemoteControlDefaultPreprocessors.cpp:419`） |
| `bAllowConsoleCommandRemoteExecution=False` | コンソールコマンドの遠隔実行は不要 |
| `AllowlistedClients` | **触らない**。②でシロと実測できたので、根拠の無い変更をしない |

`+` は配列プロパティへの追加。**先頭の `+` を忘れると追加されない。**

### 5.2 1つだけ手で入れる

§4.4 のとおり `Enable Remote Python Execution` だけは ini から入らなかったので、
**Project Settings > Plugins > Remote Control > Security で1回チェックを入れる。**

ここで効いてくるのが §5.1 の `bRestrictServerAccess=True` を ini に固定したこと。
**このチェックボックスは `Restrict Server Access` が OFF だと押せない**
（`meta = (editCondition = bRestrictServerAccess)`）。
固定しておけば常に押せる状態なので、**最悪 `Saved` が消えてもチェック1つで復旧する**。
対策前は毎回4項目を入れ直していたので、作業量は 1/4 になった。

### 5.3 「設定が消えた」を人間の記憶に頼らない

`Saved/Config/WindowsEditor/RemoteControl.ini` は **`Default` より後に読まれるので後勝ち**。
ここに古い値が残っていると、せっかく固定した設定を**打ち消し返す**。
これは目視で気づけないので、**チェッカーを書いた**（§7）。

---

## 6. 結果

```
① GET /remote/info                     → 200
② EditorStyleSettings.SelectionColor   → 200
③ KismetSystemLibrary.GetEngineVersion → 400（これは**正常**。許可していない関数なので）
④ ExecutePythonCommandEx               → 200 {"ReturnValue": true}
```

**③が 400 のままなのは成功の証拠**でもある。
「何でも呼べる」状態にせず、**必要な1関数だけ**を通したということだから。

これで Blender の「ワンポチ」ボタンから、起動中の UE へ取り込みを依頼できるようになった。

### 得られた教訓

1. **エラーメッセージの文面の違いは、最良の切り分け材料。**
   「拒否された」で止めず、**対象を変えて同じ操作を試す**と、どのゲートかが1回で分かる
2. **`.ini` が効かないときは、キーより先にファイル名を疑う。**
   `UCLASS(config = Foo)` なら `DefaultFoo.ini`。**エンジンのヘッダを1行読めば確定する**
3. **もっともらしい仮説ほど外れる。**
   「確認済みフラグが false だから」は筋が通って見えたが、
   そのフラグはソース上**どこからも参照されていなかった**。
   実際の原因は「ファイルの置き場所」という**別の次元**にあった
4. **設定の真偽は、設定自身には聞けない。**
   Remote Control は設定オブジェクトへの遠隔アクセスを拒否する（正しい設計）。
   **外から観測できないものは、ソースを読むのが最短**

---

## 7. おまけ — 設定チェッカー

対策を入れても「次に壊れたとき誰が気づくか」が残る。
**UE を起動していなくても動く**チェッカーを書いた。実物は `tools/check_rc_config.py`。

やること:

1. `Config/DefaultRemoteControl.ini` と `Saved/.../RemoteControl.ini` を読んで表にする
2. **`DefaultEngine.ini` に同じセクションがあったら「読まれません」と警告する**（同じ罠を二度踏まない）
3. Saved 側が Default を打ち消していたら警告する
4. ①〜④のプローブを投げ、**落ちたゲートに応じた対処を日本語で出す**
5. 終了コード: `0` 許可 / `1` 拒否 / `2` UE に届かない（CI で分岐できる）

以下は**記事に貼れる最小版**（依存ライブラリなし / Python 3.8+）。

```python
"""UE Remote Control の Python 実行が通るかを切り分ける最小チェッカー。"""
import json, sys, urllib.error, urllib.request

HOST, PORT = "127.0.0.1", 30010
PY_LIB = "/Script/PythonScriptPlugin.Default__PythonScriptLibrary"


def call(path, body=None, method="PUT", timeout=10):
    """RC へ1回投げる。**例外を投げずに** (status, text) を返す。"""
    url = "http://{0}:{1}{2}".format(HOST, PORT, path)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, res.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:          # 400 は「情報」。握りつぶさない
        return err.code, err.read().decode("utf-8", "replace")
    except Exception as err:
        return None, str(err)


def main():
    # ① サーバは生きているか
    status, _ = call("/remote/info", method="GET")
    if status != 200:
        print("✖ UE に届きません。エディタの起動と Remote Control API の有効化を確認")
        return 2

    # ② プロパティ読み → 通れば「許可リスト」と「パスフレーズ」はシロ
    ok_read, _ = call("/remote/object/property", {
        "objectPath": "/Script/UnrealEd.Default__EditorStyleSettings",
        "propertyName": "SelectionColor", "access": "READ_ACCESS"})

    # ③ Python でない関数 → 関数許可リストの状態を見る（400 でも異常ではない）
    status3, _ = call("/remote/object/call", {
        "objectPath": "/Script/Engine.Default__KismetSystemLibrary",
        "functionName": "GetEngineVersion"})

    # ④ 本命
    status4, body4 = call("/remote/object/call", {
        "objectPath": PY_LIB, "functionName": "ExecutePythonCommandEx",
        "parameters": {"PythonCommand": "print('rc probe')"}})

    if status4 == 200:
        print("✔ Python 実行が許可されています")
        return 0

    print("✖ Python 実行が拒否されました: {0}".format(body4))
    if "cannot be accessed remotely" in body4:
        # オブジェクト単位の拒否 ＝ Python 専用ゲート
        print("  → bEnableRemotePythonExecution が実行時に効いていません")
        print("     Config/DefaultRemoteControl.ini に書く（DefaultEngine.ini では読まれません）")
        print("     .ini は起動時にしか読まれないので、書いたらエディタを再起動すること")
    elif "not allowed by remote control settings" in body4:
        # 関数単位の拒否 ＝ 許可リストに無い
        print("  → CustomAllowedRemoteFunctionCalls に ExecutePythonCommandEx を足す")
    if ok_read != 200:
        print("  ※ プロパティ読みも通りません。許可リスト / パスフレーズも疑ってください")
    print("  （参考: ③ 非 Python 関数 = HTTP {0}）".format(status3))
    return 1


if __name__ == "__main__":
    sys.exit(main())
```

**ポイントは「④だけ見ない」こと。**
②と③を一緒に投げておくと、④が落ちたときに**原因が1つに絞れる**。
チェッカーに書くべきはチェックそのものより、**落ちたときに次に何をすればいいか**。

---

## 8. その後 — 壁の向こうで待っていたもの

Remote Control が通った直後に、**実機でしか出ないバグが2つ**出た。
記事のオチとしてはここが本題かもしれない。どちらも**単体テストでは絶対に出ない**。

### 8.1 「取り込みは成功、レポートだけ消える」

```
TypeError: Object of type TextureCompressionSettings is not JSON serializable
```

UE から**読み直した**値（`compression_settings`）をそのままレポートに入れていた。
UE の enum は `json.dump` できない。アセットは正しく生成されているのに、
**証拠だけが残らない**という、いちばん損な失敗の仕方だった。

対策は2つ。**記録に入れる前に素の型へ落とす**（enum → `TC_NORMALMAP` のような名前）、
そして**保険として `json.dump(..., default=str)`**。
値を正しく入れるのは呼び側の責任だが、1つの値のために証拠を全部失うよりはいい。

> 「書いたら読み直して確かめる」は鉄則だが、
> **読み直した値は UE の型**なので、記録に入れる前にもう一段いる。

### 8.2 「比較項目は作ったが、誰も見ていなかった」

LOD の三角形数を厳密比較する項目をレポートに出していたのに、
**検査する側のリストに登録し忘れていた**。値は合っていたので、
レポートを目で見るぶんには正しく見える。
**ずれても誰も気づかない状態**が、実機で1回通すまで分からなかった。

### 8.3 最終結果

Blender → UE の往復テストが **10 サンプルすべて緑**。
テクスチャはサフィックスから UE の設定が**実測で**一致した。

```
✔ textureCount:       {'declared': 3, 'imported': 3, 'missing': 0, 'failed': 0}
✔ textureCompression: {'_BC': 'TC_DEFAULT', '_N': 'TC_NORMALMAP', '_ORM': 'TC_MASKS'}
✔ textureSrgb:        {'_BC': True, '_N': False, '_ORM': False}
✔ textureParameters:  {'expected': ['BaseColor','Normal','ORM'],
                       'observed': ['BaseColor','Normal','ORM']}
✔ lodTriangleCounts:  {'declared': [1280, 320, 80], 'measured': [1280, 320, 80]}
```

**教訓の総まとめ**: Remote Control の設定で3回、実機のバグで2回つまずいた。
5回とも共通していたのは、**「確かめずに通った気になっていた」**こと。

- 設定は**書いただけ**では効かない → エラー文の違いで**どのゲートか**を絞る
- 値は**設定しただけ**では効かない → 読み直して比べる
- 比較項目は**作っただけ**では効かない → 検査リストに載せる
- 機能は**単体テストが通っただけ**では効かない → **端から端まで1回通す**

---

## 付録: 出典（UE 5.8.3 / すべて実機・実ソースで確認）

| 内容 | 場所 |
|---|---|
| 設定クラスの config 名 | `RemoteControlSettings.h:180` |
| ④の拒否条件 | `RemoteControlModule.cpp:2563` `CanBeAccessedRemotely` |
| ③の拒否条件 | `WebRemoteControlInternalUtils.cpp:536` `ValidateFunctionCall` |
| 既定の許可関数リスト | `RemoteControlSettings.cpp:25` |
| loopback が許可リストの対象外 | `RemoteControlDefaultPreprocessors.cpp:419` / `RemoteControlWebSocketServer.cpp:405` |
| `bSecuritySettingsReviewed` が未参照 | `RemoteControlSettings.h:420`（宣言のみ。他に参照なし） |
| 設定ファイルの階層 | `Core/Public/Misc/ConfigHierarchy.h` `GConfigLayers` / `GPluginLayers` |
