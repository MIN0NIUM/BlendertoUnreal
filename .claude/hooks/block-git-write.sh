#!/usr/bin/env bash
# ワンポチツール プロジェクト安全フック
#
# 目的: Claude が勝手に git init / commit / push / tag、および
#       gh pr create / gh release create を実行することを機械的に禁止する。
#       （ユーザー方針: Q-03。Git 操作はすべてユーザーが行う）
#
# 仕組み: PreToolUse フックとして Bash / PowerShell ツール呼び出しの直前に走り、
#         禁止パターンに一致したら exit 2 で呼び出しをブロックする。
#         stdin には {"tool_name":..., "tool_input":{"command":...}} が渡る。
#         jq が無い環境なので payload 全体を grep する（保守的＝誤検知寄り）。
set -u
payload="$(cat)"

# gh の公開系は単独で判定
if printf '%s' "$payload" | grep -Eqi 'gh[[:space:]]+(pr|release)[[:space:]]+create'; then
  echo "BLOCKED by project hook: gh pr/release create は禁止です。ユーザーに依頼してください（docs/90_オープン課題.md Q-03）。" >&2
  exit 2
fi

# git + 書き込み系サブコマンドの同時出現で判定
if printf '%s' "$payload" | grep -Eqi '(^|[^a-zA-Z0-9_.-])git([^a-zA-Z0-9_-]|$)'; then
  if printf '%s' "$payload" | grep -Eqi '(^|[^a-zA-Z0-9_-])(init|commit|push|tag)([^a-zA-Z0-9_-]|$)'; then
    echo "BLOCKED by project hook: git init / commit / push / tag は禁止です。" >&2
    echo "このプロジェクトでは Git 操作はすべてユーザーが行います（docs/90_オープン課題.md Q-03）。" >&2
    echo "コミットやプッシュが必要な場合は、作業内容を報告してユーザーに依頼してください。" >&2
    exit 2
  fi
fi

exit 0
