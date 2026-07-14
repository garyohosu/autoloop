# TESTCASE.md — 汎用自動継続開発ランナーのテストケース

対象: `SPEC.md` v0.1.0 / `USECASE.md` / `SEQUENCE.md` / `CLASS.md` / `UI.md`
方針: 実 Agent(Codex / Claude)や実 GitHub は使わず、Fake / Unit / Contract テストで検証する(SPEC §3.1)。live 評価は対象外(§3.2)。
テスト用リポジトリ: 一時ディレクトリに作る fixture リポジトリ + fixture remote(bare リポジトリ)を用いる。

---

## 1. ユニットテスト

### 1.1 InstructionDocument / InstructionFrontMatter(SPEC §6)

| ID | 観点 | 入力 | 期待結果 |
|---|---|---|---|
| T-101 | Front Matter 正常解析 | 仕様例どおりの instructions.md | 全フィールドが型どおり取得できる |
| T-102 | status 不正値 | `status: unknown` | 解析エラー(TaskStatus 8値以外を拒否) |
| T-103 | 必須フィールド欠落 | task_id なし | 解析エラー |
| T-104 | allowed_paths のグロブ照合 | `src/<PACKAGE_NAME>/**` と各種パス | 一致/不一致が仕様どおり判定される |
| T-105 | sha256 | 同一内容/異なる内容 | 同一入力で同一ハッシュ、変更で変化 |

### 1.2 ResultDocument / ResultBlock(SPEC §7)

| ID | 観点 | 入力 | 期待結果 |
|---|---|---|---|
| T-111 | 完全ブロック解析 | BEGIN/END マーカー揃った result.md | 最新ブロックの JSON が取得できる |
| T-112 | 複数ブロック | 同一 task_id のブロックが2つ | 最新の完全なブロックだけを読む |
| T-113 | 途中書き込み | BEGIN のみで END なし | `is_partial_block=True`、自動続行しない(UI: exit 1) |
| T-114 | result_commit null 許容 | `"result_commit": null` | 解析成功(QandA Q-03) |
| T-115 | JSON 破損 | 不正 JSON のブロック | 解析エラーとして失敗分類 |

### 1.3 FailureClassifier(SPEC §16)

| ID | 観点 | 入力(stdout/stderr 例) | 期待分類 |
|---|---|---|---|
| T-121 | 利用枠切れ | "You've hit your weekly limit" / "QUOTA_EXCEEDED" | usage_limit |
| T-122 | コンテキスト上限 | "prompt too long" / "max context" | context_limit |
| T-123 | 一時エラー | "rate limit" / "network timeout" | transient_error |
| T-124 | 認証エラー | "AUTH_REQUIRED" / "login required" | authentication_error |
| T-125 | 正常終了 | exit 0 + 完全な結果ブロック | none |
| T-126 | 複合判定 | exit 0 だが結果ブロック不完全 | exit code だけで completed にしない |

### 1.4 LockManager(SPEC §20)

| ID | 観点 | 前提 | 期待結果 |
|---|---|---|---|
| T-131 | lock 取得 | lock なし | 取得成功、pid/hostname/task_id が記録される |
| T-132 | 二重起動拒否 | 生存 PID の lock あり | 取得失敗(UI: exit 10) |
| T-133 | stale 判定 | 存在しない PID の lock | `is_stale=True`、自動解除はしない(UI: exit 13) |
| T-134 | clear-lock | stale lock | ログを残して解除。生存 PID なら拒否 |

### 1.5 SessionManager(SPEC §10)

| ID | 観点 | 前提 | 期待結果 |
|---|---|---|---|
| T-141 | session ID 保存 | Fake Agent が初回出力で ID を返す | state.json に保存される |
| T-142 | ID 明示 resume | 保存済み ID あり | resume 呼び出しに ID が渡る(--last/--continue 相当は使わない) |
| T-143 | rotation: resume 超過 | resume_count > max_resume_count | 新規セッション作成、rotation として記録 |
| T-144 | rotation: 5タスク | task_count_in_session = 5 | 新規セッション作成 |
| T-145 | rotation: context_limit | 分類が context_limit | 新規セッション + resume_count に数えない |

---

## 2. コンポーネント/Contract テスト(Fake Git・Fake Agent 使用)

### 2.1 RepositorySynchronizer / preflight(SPEC §9、QandA Q-04)

| ID | 観点 | 前提 | 期待結果 |
|---|---|---|---|
| T-201 | 正常 preflight | clean・HEAD=origin/main・ready 指示書 | PLANNING/EXECUTING へ進める |
| T-202 | dirty worktree | 未 commit 変更あり | 開始しない。PAUSED_DIRTY_WORKTREE、reset/stash/clean を呼ばない(UI: exit 12) |
| T-203 | 未追跡ファイル | untracked あり | 開始しない |
| T-204 | HEAD不一致 | origin/main が先行 | 開始しない |
| T-205 | expected_base_commit 不含 | HEAD が指定 commit を含まない | 開始しない |
| T-206 | status≠ready | draft/completed 指示書 | 開始しない(UI: 例外状態表示) |
| T-207 | task_id 二重実行 | result.md に同 task_id の completed ブロックあり | 開始しない(MVP合格条件12) |
| T-208 | ネットワーク障害 | fetch 失敗 | exit 12、リトライしない(UI §5) |

### 2.2 Verifier(SPEC §14)

| ID | 観点 | 前提 | 期待結果 |
|---|---|---|---|
| T-211 | 正常検証 | allowed_paths 内変更+テスト成功+完全ブロック | verify 成功 → COMMITTING へ |
| T-212 | 許可外変更 | allowed_paths 外のファイル変更 | unexpected_change、commit しない(MVP合格条件8) |
| T-213 | 禁止ファイル | stdout ログ等が worktree に追加 | commit しない |
| T-214 | テスト失敗 | required_tests の exit≠0 | commit しない(MVP合格条件9) |
| T-215 | テスト未実行 | 結果ブロックに required_tests の一部がない | commit しない |
| T-216 | 件数不一致 | result.md の summary とログの件数が不一致 | 検証失敗 |
| T-217 | dream.md 保護 | `.git/info/exclude` 等で除外済みの個人ファイル dream.md を Executor が変更 | 検証失敗(SPEC §14.1)。※単なる未追跡ファイルは T-203 で preflight が先に拒否するため、本ケースは除外設定済みファイルを対象とする(QandA Q-06) |

### 2.3 GitPublisher(SPEC §15)

| ID | 観点 | 前提 | 期待結果 |
|---|---|---|---|
| T-221 | 個別 stage | changed_files 3件 | `git add -- <各ファイル>` のみ呼ばれる。`add .`/`-A`/`commit -a` は API 自体が存在しない(MVP合格条件10) |
| T-222 | push 後確認 | push 成功 | HEAD=origin/main 確認後に completed(MVP合格条件11) |
| T-223 | push 失敗 | fixture remote を先行させる | 停止。force push しない。commit は保持(UI: exit 1) |
| T-224 | result_commit 記録 | commit 成功 | SHA が logs/result.json と state.json に記録される(QandA Q-03) |

### 2.4 Planner(SPEC §12、Phase 3)

| ID | 観点 | 前提 | 期待結果 |
|---|---|---|---|
| T-231 | ready 決定 | Fake Planner が ready + task_id を返す | Schema 検証通過、instructions.md が生成される |
| T-232 | 4値以外の decision | "maybe" 等 | Schema 検証エラー → 停止 |
| T-233 | 複数 ready | 2タスクを ready にする出力 | 拒否して停止 |
| T-234 | approval_required | live 必要の決定 | 停止、exit 2(UI §4) |
| T-235 | release_ready | §22 の条件を満たす fixture | 停止、exit 4 |
| T-236 | 許可外ファイル変更 | Planner が instructions.md 以外を変更 | 検出して停止 |

---

## 3. シナリオテスト(Controller 全体、Fake Agent)

| ID | シナリオ | 期待結果 | MVP合格条件 |
|---|---|---|---|
| T-301 | 単一タスク正常完走 | ready 検出→実行→検証(allowed_paths 内であることを確認)→commit→push→completed→必ず停止、exit 0 | 1,2,3,8,9,10,11 |
| T-302 | resume 継続 | Fake Agent を途中終了させ再起動 | 同じ session ID で resume、重複実装なし | 4,5 |
| T-303 | usage_limit → fallback | Fake Claude が利用枠切れ出力 | 差分を破棄せず Fake Codex が新規セッションで未完了分のみ続行 | 6,7 |
| T-304 | fallback 不可 | fallback_executor 未許可 | PAUSED_USAGE_LIMIT で停止、exit 5 | 6 |
| T-305 | transient リトライ | 1回目 rate limit、2回目成功 | 30秒待機後の resume で完走(待機は Fake クロック) | - |
| T-306 | transient 3回失敗 | 3回連続 rate limit | 停止または fallback、無限リトライしない | - |
| T-307 | live 検出 | 指示書に `allow_live: false` + live を要する作業 | approval_required で自動停止 | 13 |
| T-308 | 5タスク上限 | READY_NEXT が続く fixture | 5タスク完了で停止 | 14 |
| T-309 | release_ready 停止 | Planner が release_ready | 停止、GitHub Release は発行しない | 15 |
| T-310 | 連続2タスク失敗 | 2タスク連続でテスト失敗 | 停止 | - |
| T-311 | 同一原因の連続テスト失敗 | 同じテストが同じ原因で2回失敗 | 停止(SPEC §18) | - |
| T-312 | max_wall_minutes 超過 | Fake クロックで制限超過 | 停止 | - |
| T-313 | 停止時の lock 解放 | T-301〜T-312 の全停止経路 | いずれも lock 解放+state.json 最終記録(SEQUENCE 補足) | - |

---

## 4. UI テスト(exit code / 表示)

| ID | 観点 | 期待結果 |
|---|---|---|
| T-401 | exit code 網羅 | UI.md §4 の 0/1/2/3/4/5/10/11/12/13/14 が対応シナリオで返る |
| T-402 | 終了サマリ | 結果・task_id・commit・テスト要約・停止理由・次の操作・ログパスが表示される |
| T-403 | 秘密情報マスク | Fake Agent 出力に APIキー風文字列を混入 → console/log に出力されない(SPEC §21) |
| T-404 | status 空状態 | state.json 不在で「初回実行前」表示、exit 0 |
| T-405 | status 破損 | 不正 JSON の state.json で exit 14、上書き・削除しない |
| T-406 | ログ配置 | 全ログが `<AUTOMATION_ROOT>` 配下。リポジトリ内に一時ファイルなし(SPEC §5) |

---

## 5. テスト除外(MVP では自動テストしない)

- 実 Codex CLI / 実 Claude Code の起動・resume(手動受け入れ試験で確認)
- 実 GitHub への push(fixture bare リポジトリで代替)
- live 評価・WebSearch・実 HTTP(SPEC §3.2)
- Windows タスクスケジューラ登録(Phase 5 の手動確認)
