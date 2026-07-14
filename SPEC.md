# 汎用自動継続開発ランナー仕様書

文書バージョン: 0.1.0  
仮称: Generic AutoLoop
対象環境: Windows 11 / PowerShell / Python 3.11以上  
対象リポジトリ: `<REPOSITORY_ROOT>`

> Codex CLIおよびClaude Codeの具体的なオプションは、実装開始時にインストール済みバージョンの`--help`と公式仕様で再確認する。本書では、非対話実行、セッションID保存、resumeが可能であることを前提とする。

---

## 1. 目的

対象リポジトリで現在行っている次の作業を自動化する。

1. 前回の実装結果を確認する
2. 次に行う作業を1件決定する
3. `instructions/instructions.md`へ指示書を書く
4. Codex CLIまたはClaude Codeで実装する
5. テスト結果と変更内容を確認する
6. `instructions/result.md`へ結果を記録する
7. commit・pushする
8. 同じAIセッションをresumeして次の作業へ進む
9. リリース可能、承認待ち、障害発生のいずれかで停止する

ChatGPTとの手作業による指示書の受け渡しは必須としない。

### 1.1 別リポジトリへ移植する場合

この `loop` ディレクトリは、任意のGitリポジトリへコピーして利用するテンプレートである。
利用開始時に、次の値を対象リポジトリごとの設定へ置き換える。

- `<REPOSITORY_ROOT>`: `git rev-parse --show-toplevel` で得られる対象リポジトリのルート
- `<AUTOMATION_ROOT>`: 対象リポジトリの外に置く状態・ログ保存先
- `<PACKAGE_NAME>`: `allowed_paths` やテスト例で使用する場合だけ、対象プロジェクトのパッケージ名
- `instructions/`、`tests/`、`src/` などのパス: 対象リポジトリの実際の構成に合わせた許可パス

リポジトリ名、製品名、ソースパッケージ名を前提にした処理を実装してはならない。
言語、ビルドシステム、テストコマンド、既定ブランチは設定で指定し、存在しない構成を前提にしない。

---

## 2. 基本方針

### 2.1 正本

判断の優先順位は次のとおりとする。

1. 現在のGitリポジトリ
2. `instructions/instructions.md`
3. `instructions/result.md`
4. `hikitsugi.md`
5. `FIX_PLAN.md`
6. AIセッションの過去コンテキスト

AIの会話履歴と現在のファイルが食い違う場合は、必ず現在のGitとファイルを優先する。

### 2.2 1回に1タスク

一度の開発サイクルで実行する作業は1件だけとする。

複数のFIX_PLAN項目を、依存関係の確認なしに並行実装してはならない。

### 2.3 Git操作の分離

AI Agentは原則として次を担当する。

- ファイルの調査
- 実装
- テスト
- 文書更新
- 完了報告

Controllerは次を担当する。

- Git状態確認
- pull
- 変更範囲確認
- stage
- commit
- push
- HEADとorigin/mainの一致確認

AI Agent自身に無制限な`git add .`、reset、clean、force pushを実行させない。

---

## 3. 対象範囲

### 3.1 MVPで自動化するもの

- 通常のソースコード実装
- Fake / Unit / Contractテスト
- ドキュメント更新
- `instructions/result.md`への結果追記
- CodexおよびClaudeの非対話起動
- 同一セッションのresume
- 利用枠切れの検出
- Agent停止後の再開
- commit・push
- 次作業の選択
- リリース準備完了判定

### 3.2 MVPでは自動化しないもの

- live評価
- 実Claude / 実Codexをランナー内部から呼ぶ評価
- WebSearchや実HTTPを伴う高コスト試験
- ユーザーの明示承認が必要な処理
- force push
- reset、stash、clean
- ファイル削除
- 秘密情報や認証設定の変更
- GitHub Releaseの公開
- main以外からの強制マージ

これらに到達した場合は`approval_required`で停止する。

---

## 4. 全体構成

```text
Generic AutoLoop Controller
│
├─ Repository Synchronizer
│  ├─ git status
│  ├─ git fetch
│  ├─ git pull --ff-only
│  └─ HEAD / origin/main確認
│
├─ Planner
│  ├─ result.mdを読む
│  ├─ FIX_PLAN.mdを読む
│  ├─ 次作業を1件決める
│  └─ instructions.mdを作成する
│
├─ Executor
│  ├─ Codex CLI
│  └─ Claude Code
│
├─ Verifier
│  ├─ git diff確認
│  ├─ テスト結果確認
│  ├─ 禁止ファイル確認
│  └─ 完了条件判定
│
├─ Session Manager
│  ├─ session ID保存
│  ├─ resume
│  ├─ context limit判定
│  └─ session rotation
│
├─ Git Publisher
│  ├─ 対象ファイルだけstage
│  ├─ commit
│  └─ push
│
└─ State Store
   ├─ state.json
   ├─ lock
   └─ logs
```

---

## 5. ディレクトリ構成

自動化システムの状態は、対象リポジトリ外へ保存する。

```text
<AUTOMATION_ROOT>\
├─ config.json
├─ state.json
├─ runner.lock
├─ prompts\
│  ├─ planner.txt
│  ├─ executor.txt
│  ├─ resume.txt
│  └─ handoff.txt
├─ schemas\
│  ├─ planner-result.schema.json
│  └─ executor-result.schema.json
└─ logs\
   └─ YYYYMMDD-HHMMSS\
      ├─ controller.log
      ├─ agent-stdout.jsonl
      ├─ agent-stderr.txt
      ├─ git-before.txt
      ├─ git-after.txt
      └─ result.json
```

リポジトリ内に一時ログ、stdout、stderr、session ID、利用量情報を保存してはならない。

---

## 6. 指示書の形式

`instructions/instructions.md`の先頭にYAML Front Matterを追加する。

```yaml
---
protocol_version: 1
task_id: X-8.20
task_revision: 1
status: ready
previous_task_id: X-8.19
expected_base_commit: cd8422e
preferred_executor: claude
fallback_executor: codex
session_policy: resume
allow_source_edit: true
allow_test_execution: true
allow_commit: true
allow_push: true
allow_live: false
allow_external_ai: false
allow_web_search: false
max_resume_count: 3
max_wall_minutes: 90
commit_message: "feat: implement X-8.20"
required_tests:
  - "py -m pytest"
allowed_paths:
  - "src/<PACKAGE_NAME>/**"
  - "tests/**"
  - "QandA.md"
  - "SPEC.md"
  - "CLASS.md"
  - "TESTCASE.md"
  - "FIX_PLAN.md"
  - "hikitsugi.md"
  - "instructions/result.md"
---
```

Front Matterより下には、人間とAIが読む従来形式の詳細指示を書く。

### 6.1 status

使用可能な値は次とする。

```text
draft
ready
running
completed
approval_required
blocked
failed
release_ready
```

Controllerが実行を開始できるのは`ready`だけとする。

### 6.2 expected_base_commit

実行開始時のHEADが指定commitを含まない場合は停止する。

HEADと`origin/main`が一致しない場合も停止する。

### 6.3 allowed_paths

Controllerは作業終了後、変更された全ファイルを検査する。

`allowed_paths`に一致しない変更が1件でもあればcommitせず、`unexpected_change`として停止する。

---

## 7. result.mdの機械可読形式

従来の文章による記録に加えて、各タスクの末尾へ機械可読ブロックを追加する。

```markdown
<!-- AUTOLOOP_RESULT_BEGIN:X-8.20 -->

```json
{
  "protocol_version": 1,
  "task_id": "X-8.20",
  "status": "completed",
  "executor": "claude",
  "session_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "base_commit": "cd8422e",
  "result_commit": "abcdef1",
  "tests": [
    {
      "command": "py -m pytest",
      "exit_code": 0,
      "summary": "291 passed, 6 deselected"
    }
  ],
  "changed_files": [
    "src/<PACKAGE_NAME>/models.py",
    "tests/unit/test_example.py",
    "instructions/result.md"
  ],
  "live_executed": false,
  "external_ai_executed": false,
  "next_recommendation": "X-8.21",
  "completed_at": "2026-07-14T14:00:00+09:00"
}
```

<!-- AUTOLOOP_RESULT_END:X-8.20 -->
```

Controllerは最新の完全なブロックだけを読む。

開始マーカーだけがあり終了マーカーがない場合は、途中書き込みとして扱い、自動続行しない。

---

## 8. Controllerの状態遷移

```text
IDLE
  ↓
SYNCING
  ↓
PREFLIGHT
  ↓
PLANNING
  ↓
PLAN_VERIFY
  ↓
EXECUTING
  ↓
RESULT_VERIFY
  ↓
COMMITTING
  ↓
PUSHING
  ↓
NEXT_DECISION
  ├─ READY_NEXT → PLANNING
  ├─ APPROVAL_REQUIRED → STOP
  ├─ BLOCKED → STOP
  ├─ RELEASE_READY → STOP
  └─ FAILED → STOP
```

追加の一時停止状態:

```text
PAUSED_USAGE_LIMIT
PAUSED_CONTEXT_LIMIT
PAUSED_TRANSIENT_ERROR
PAUSED_DIRTY_WORKTREE
```

---

## 9. 起動前確認

ControllerはAgent起動前に必ず次を実行する。

```powershell
git status --short
git fetch origin main
git pull --ff-only
git rev-parse HEAD
git rev-parse refs/remotes/origin/main
git diff --check
```

次の場合は実行を開始しない。

- worktreeがdirty
- 未追跡ファイルがある
- pullがfast-forwardにならない
- HEADとorigin/mainが異なる
- instructionsの`expected_base_commit`を含まない
- 同じ`task_id`が既に完了済み
- 別のControllerがlockを保持している
- `status`が`ready`ではない

`reset`、`stash`、`clean`、ファイル移動による自動復旧は禁止する。

---

## 10. セッション管理

### 10.1 state.json

```json
{
  "protocol_version": 1,
  "controller_state": "EXECUTING",
  "current_task_id": "X-8.20",
  "current_executor": "claude",
  "claude_session_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "codex_session_id": null,
  "planner_session_id": "yyyyyyyy-yyyy-yyyy-yyyy-yyyyyyyyyyyy",
  "resume_count": 1,
  "task_count_in_session": 2,
  "last_instruction_sha256": "...",
  "last_result_sha256": "...",
  "started_at": "2026-07-14T13:00:00+09:00",
  "updated_at": "2026-07-14T13:20:00+09:00"
}
```

### 10.2 session IDの使用

無人運転では`--last`または`--continue`だけに依存しない。

必ず初回出力からsession IDを取得し、以後はIDを明示してresumeする。

Codexの概念例:

```powershell
codex exec resume <SESSION_ID> -
```

Claudeの概念例:

```powershell
claude -p --resume <SESSION_ID>
```

`--last`や`--continue`は、手動復旧時だけ使用してよい。

### 10.3 resume時の共通指示

```text
前回セッションを再開します。

過去の会話だけを根拠にせず、まず現在の次の状態を読み直してください。

- git status --short
- git diff
- git log -5
- instructions/instructions.md
- instructions/result.md
- hikitsugi.md
- FIX_PLAN.md

Gitとファイルを正本としてください。

既に完了した実装、テスト、live評価、commit、pushを重複実行しないでください。
未完了部分だけを続行してください。
```

### 10.4 セッション更新

次のいずれかで新しいセッションへ切り替える。

- context limitを検出
- resume回数が3回を超えた
- 5タスクを同一セッションで処理した
- task_idの系列が変わった
- 前回セッションの状態が取得できない
- Agent自身が新規セッションを推奨した
- リリース前の独立レビューを行う

新規セッションでは、過去の会話ではなくGitと引継ぎファイルから状態を復元する。

---

## 11. Agent起動方式

### 11.1 Codex CLI

初回の概念例:

```powershell
Get-Content $PromptFile -Raw |
    codex exec `
        --json `
        --sandbox workspace-write `
        -
```

再開の概念例:

```powershell
Get-Content $ResumePromptFile -Raw |
    codex exec resume $SessionId `
        --json `
        -
```

セッションを永続化しないオプションは使用しない。

全権限を無条件で許可するモードはMVPでは使用しない。

### 11.2 Claude Code

初回の概念例:

```powershell
claude -p `
    --output-format json `
    --allowedTools "Read,Edit,Bash"
```

再開の概念例:

```powershell
claude -p `
    --resume $SessionId `
    --output-format json `
    --allowedTools "Read,Edit,Bash"
```

必要なプロンプトはstdinまたはファイル参照で渡し、長い指示書全体をコマンドライン引数へ埋め込まない。

---

## 12. Plannerの責務

Plannerは次を読む。

- `instructions/result.md`
- `hikitsugi.md`
- `FIX_PLAN.md`
- `QandA.md`
- `SPEC.md`
- `TESTCASE.md`
- Git log
- 最新commitの差分
- 通常テスト結果

Plannerが行える変更は原則として次だけとする。

```text
instructions/instructions.md
```

必要なら、計画確定に伴う設計文書更新を別タスクとして作成する。

Plannerは次のいずれかを返す。

```json
{
  "decision": "ready",
  "next_task_id": "X-8.20"
}
```

```json
{
  "decision": "approval_required",
  "reason": "live評価が必要"
}
```

```json
{
  "decision": "blocked",
  "reason": "仕様判断が未確定"
}
```

```json
{
  "decision": "release_ready",
  "reason": "alpha release条件を満たした"
}
```

Plannerは一度に複数の次作業を`ready`にしてはならない。

---

## 13. Executorの責務

Executorは次を行う。

1. 指示書を読む
2. preflight結果を確認する
3. 許可された範囲だけを変更する
4. 指定テストを実行する
5. `git diff --check`を実行する
6. `instructions/result.md`へ結果を書く
7. Controllerへ終了する

Executorは次を行ってはならない。

- 指示書にない追加実装
- live評価
- Agent呼び出し回数の勝手な追加
- 失敗したlive評価の再試行
- `git add .`
- reset、stash、clean
- force push
- ユーザーファイルの移動
- 評価結果やstdoutのGit追加
- 自分で次タスクを実装し始める

次タスクの提案はできるが、実行はPlannerとControllerの次サイクルで行う。

---

## 14. 検証処理

Agent終了後、Controllerは次を検証する。

### 14.1 ファイル検証

- 変更ファイルが`allowed_paths`内
- 禁止ファイルが追加されていない
- 生stdout、stderr、評価artifactが追加されていない
- `instructions/result.md`に対象task_idの完全な結果ブロックがある
- `dream.md`など既存の未追跡個人ファイルに触れていない

### 14.2 テスト検証

- 指定テストがすべて実行済み
- exit codeが0
- テスト件数がresult.mdとログで一致
- live / expensiveテストが意図せず実行されていない

### 14.3 Git検証

```powershell
git diff --check
git status --short
git diff --name-only
git diff --stat
```

検証に失敗した場合、Controllerはcommitしない。

---

## 15. commit・push

Controllerが対象ファイルを明示してstageする。

```powershell
git add -- <許可された個別ファイル>
```

次は禁止する。

```powershell
git add .
git add -A
git commit -a
git push --force
```

commit後に次を確認する。

```powershell
git status --short
git push origin main
git rev-parse HEAD
git rev-parse refs/remotes/origin/main
```

HEADとorigin/mainが一致し、worktreeがcleanになった場合だけタスクを`completed`とする。

---

## 16. 利用枠切れと障害分類

ControllerはAgentの終了コードだけでなく、構造化stdout、stderr、Git状態を組み合わせて分類する。

### 16.1 usage_limit

例:

```text
You've hit your session limit
You've hit your weekly limit
QUOTA_EXCEEDED
usage limit
```

処理:

1. 現在のGit差分を保存する
2. 変更を破棄しない
3. result.mdが途中なら完成扱いにしない
4. `PAUSED_USAGE_LIMIT`へ移行する
5. fallbackが許可されていれば別Agentへ引き継ぐ
6. fallback不可なら停止する

### 16.2 context_limit

例:

```text
context limit
prompt too long
conversation too long
max context
```

処理:

1. 同じAgentの新しいセッションを作成
2. Gitとファイルから引継ぎプロンプトを生成
3. 既存作業を再実行しないよう指示
4. resume回数ではなくsession rotationとして記録する

### 16.3 transient_error

例:

```text
rate limit
overloaded
temporary unavailable
network timeout
```

処理:

- 最大2回
- 30秒、120秒の順で待つ
- 同じAgent・同じsession IDでresumeする
- 3回目は停止またはfallbackする

### 16.4 authentication_error

例:

```text
AUTH_REQUIRED
authentication failed
login required
```

自動復旧しない。

`approval_required`または`blocked`で停止する。

### 16.5 safety_classifier_unavailable

Claude Codeが安全判定サービス停止でShellを使えない場合:

- 読み取りだけで完了できる作業は継続可
- commit、push、実装が必要なら停止
- 同じコマンドを連続して繰り返さない
- fallback Agentへの引継ぎを許可する

---

## 17. Agent間引継ぎ

ClaudeからCodex、またはCodexからClaudeへ切り替える場合、会話コンテキストそのものは渡さない。

引継ぎの正本は次とする。

- Git diff
- `instructions/instructions.md`
- `instructions/result.md`
- `hikitsugi.md`
- Controllerの状態情報
- 前Agentの終了分類
- 実行済みテスト一覧

引継ぎプロンプト:

```text
前のAgentが途中で停止しました。

作業を最初から再実行しないでください。

最初に以下を確認してください。

- git status --short
- git diff
- git log -5
- instructions/instructions.md
- instructions/result.md
- hikitsugi.md

前Agentの停止理由:
<分類>

既に実行済み:
<実行済み作業>

未完了:
<未完了作業>

live評価、外部AI呼び出し、テスト、ファイル生成を重複実行しないでください。
現在の差分を保護したまま、未完了部分だけを完了してください。
```

---

## 18. 自動停止条件

次の場合は必ず停止する。

- `approval_required`
- `blocked`
- `release_ready`
- worktreeが予期せずdirty
- 許可外ファイルが変更された
- テスト失敗
- 同じテストが2回連続で同じ原因により失敗
- 同じtask_idの二重実行
- resume回数超過
- 1タスクの制限時間超過
- 1回のController起動で5タスク完了
- 連続2タスク失敗
- mainとorigin/mainの不一致
- push失敗
- 認証エラー
- live実行が必要
- Plannerが次作業を一意に決められない

---

## 19. live実行の扱い

MVPでは、次を含む作業を自動実行しない。

```text
AUTOLOOP_LIVE=1
--adapter-mode real
WebSearch
実HTTP Evidence
X-8 holdout live
有料API
外部Agent呼び出し
```

Plannerは次の状態を生成する。

```json
{
  "decision": "approval_required",
  "task_id": "X-8.XX",
  "reason": "live評価には明示承認が必要",
  "requested_approval": "X-8.XXのlive実行を各1回、合計最大N回だけ承認します"
}
```

人間が承認して、新しい指示書revisionをcommitするまで進めない。

一度失敗したlive実行を、resumeやfallbackによって自動再試行してはならない。

---

## 20. ロックと二重起動防止

起動時に次を作成する。

```text
<AUTOMATION_ROOT>\runner.lock
```

lockには次を記録する。

```json
{
  "pid": 12345,
  "hostname": "PC-NAME",
  "started_at": "2026-07-14T13:00:00+09:00",
  "task_id": "X-8.20"
}
```

既存PIDが生存している場合、新しいControllerは終了する。

PIDが存在しない古いlockは、ログを残したうえでstale lockとして解除できる。

---

## 21. ログと秘密情報

ログに保存してよいもの:

- Agentイベント種別
- session ID
- 終了コード
- task ID
- 実行時刻
- テストコマンド
- テスト要約
- 変更ファイル名
- commit SHA

保存してはならないもの:

- APIキー
- OAuth token
- Cookie
- 環境変数全体
- 認証ファイル
- 生Evidence本文
- Claude / Codexの秘密設定
- ユーザー個人情報
- 生の長大なprompt全文

stderrを保存する場合は、リポジトリ外へ保存し、Gitへ追加しない。

---

## 22. リリース準備完了条件

Plannerは次をすべて満たした場合に限り`release_ready`を返せる。

- 通常テストが全件pass
- worktreeがclean
- HEADとorigin/mainが一致
- 直近タスクが完了
- α版の必須ブロッカーが解消
- READMEのセットアップ手順が現在の実装と一致
- 設定例が存在
- 既知の制限が記載
- live依存部分が「実験的」と明示
- 誤回答を公開した既知の評価結果がない
- 未解決項目がリリース阻害か将来対応か分類済み
- バージョン候補が決定済み

`release_ready`は自動でGitHub Releaseを公開する意味ではない。

---

## 23. MVP実装順序

### Phase 1: 単一タスク実行

- Python Controller作成
- lock
- preflight
- CodexまたはClaudeを1回起動
- 結果検証
- commit・push
- 必ず停止

### Phase 2: session resume

- session ID取得
- state.json保存
- Codex resume
- Claude resume
- 最大resume回数
- context limit時の新規session

### Phase 3: Planner追加

- result.md解析
- FIX_PLAN解析
- 次タスク1件選定
- instructions.md生成
- Planner結果Schema検証

### Phase 4: Agent fallback

- Claude利用枠切れ検出
- Codexへの引継ぎ
- Codex利用枠切れ検出
- Claudeへの引継ぎ
- 重複実行防止

### Phase 5: 連続運転

- 最大5タスク
- 自動停止条件
- release_ready判定
- Windowsタスクスケジューラ起動

### Phase 6: live承認ゲート

MVP運用が安定した後に別仕様として実装する。

---

## 24. MVP合格条件

1. `ready`の指示書を1件検出できる
2. clean worktreeだけで開始する
3. Agentを非対話で起動できる
4. session IDを保存できる
5. Agent停止後に同じsession IDでresumeできる
6. 利用枠切れを検出できる
7. 途中差分を破棄せず別Agentへ引き継げる
8. 許可外ファイルがあればcommitしない
9. 指定テスト成功時だけcommitする
10. 個別ファイルだけをstageする
11. push後にHEADとorigin/mainの一致を確認する
12. 同じtask_idを二重実行しない
13. live作業では自動停止する
14. 最大5タスクで停止する
15. `release_ready`で停止する

---

## 25. 推奨する初期運用

最初の版では次の構成とする。

```text
Planner: Codex CLI
Executor: Claude Code
Fallback Executor: Codex CLIの別セッション
Controller: Python
Git操作: Controller
最大連続タスク: 1
最大resume: 2
live: 全面禁止
起動: 手動
```

1タスク自動実行が安定してから、最大連続タスクを3、次に5へ増やす。

最初から無限ループにはしない。

---

## 26. 将来拡張

- Webダッシュボード
- Windows通知
- Slack / メール通知
- GitHub Issueとの同期
- PRベース運用
- PlannerとReviewerの独立Agent化
- 実装Agentの性能比較
- タスク所要時間集計
- トークン・利用額集計
- 自動リリースノート作成
- GitHub Actionsによる独立テスト
- ランナー自身による実装結果監査
