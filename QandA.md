# QandA.md — 汎用自動継続開発ランナー設計上の未確定事項

## Q-01: Phase 1 における instructions.md の作成者
- **状況**: SPEC.md §23 Phase 1 では Planner が未実装だが、Controller は `status: ready` の指示書を前提に動作する(§6.1、§24-1)。
- **論点**: Phase 1〜2 の期間、`instructions/instructions.md` は誰が作成するか。
- **暫定方針**: Phase 1〜2 では人間(開発者)が指示書を作成・commit する。Phase 3 以降は Planner が生成する。USECASE / SEQUENCE はこの前提で記述する。
- **状態**: 暫定方針で続行(要確認)

## Q-02: resume 上限値の不一致
- **状況**: §6 Front Matter 例は `max_resume_count: 3`、§10.4 は「resume回数が3回を超えた」で rotation、§25 推奨初期運用は「最大resume: 2」。
- **論点**: システム既定値と指示書ごとの上限の関係。
- **暫定方針**: 上限は指示書の `max_resume_count`(タスク単位)を正とし、config.json の既定値で補完する。§25 の「2」は初期運用時の config 推奨値と解釈する。
- **状態**: 暫定方針で続行(要確認)

## Q-03: result.md の result_commit の確定タイミング(Codex レビュー指摘)
- **状況**: SPEC §7 の結果ブロックは `result_commit` を含むが、Executor が result.md を書く時点では commit SHA が未確定。commit SHA は result.md の内容自体に依存するため、事前確定は原理的に不可能(循環)。
- **論点**: `result_commit` を誰が・いつ確定させるか。
- **暫定方針**: Executor は `result_commit: null` で結果ブロックを書く。確定 SHA は Controller が commit 後に `logs/<run>/result.json` と `state.json` に記録し、result.md 内は null のままとする(次タスクの Planner は git log で確認可能)。SEQUENCE.md はこの前提で記述する。
- **状態**: 暫定方針で続行(SPEC.md の修正候補)

## Q-04: 指示書の ready 検査タイミング(Codex レビュー残存指摘)
- **状況**: SPEC §9 は起動前確認(PREFLIGHT)で「`status` が `ready` ではない」場合に実行開始しないと定めるが、§8/§12 では PLANNING(PREFLIGHT の後)で Planner が指示書を生成する。既存指示書の検査と Planner 生成物の検査のタイミングが SPEC 上で一本化されていない。
- **論点**: `status: ready`・`expected_base_commit`・task_id 二重実行の検査を PREFLIGHT と PLAN_VERIFY のどちらで行うか。
- **暫定方針**: 両方で行う。Phase 1〜2(人間作成の既存指示書)は PREFLIGHT で検査。Phase 3 以降(Planner 生成)は PLAN_VERIFY で同一の検査を実施し、PREFLIGHT では Git 状態のみ検査する。USECASE.md UC-02 はこの前提で記述済み。
- **状態**: 暫定方針で続行(2回目レビューでも残存した論点。SPEC.md の修正候補)

## Q-05: CLASS.md の Mermaid 静的メソッド記法(レビュー指摘が矛盾)
- **状況**: Codex レビュー1回目は「`$` は戻り値型の後ろ(`) Config$`)」、2回目は「`$` は閉じ括弧直後(`)$ Config`)」と逆の指摘をした。Mermaid 公式仕様では両形式とも許容されるが、レビューが収束しなかった。
- **対応**: 再試行上限に達したため、`$` 分類子を削除して確実に描画可能な形へ変更した。静的メソッドであること(Config.load / InstructionDocument.parse / ResultDocument.parse_latest_block / is_partial_block)は実装時にクラスメソッドとして扱う。
- **状態**: 解決済み(記録のみ)

## Q-06: 未追跡個人ファイル(dream.md)と preflight の未追跡ファイル拒否の緊張
- **状況**: SPEC §9 は「未追跡ファイルがある」場合に実行を開始しないと定めるが、§14.1 は「dream.md など既存の未追跡個人ファイルに触れていない」ことの検証を求めており、未追跡個人ファイルの存在を前提にしている(Codex レビュー指摘)。
- **論点**: dream.md 等の個人ファイルを worktree に置いたまま自動運転する方法。
- **暫定方針**: 個人ファイルは `.git/info/exclude`(ローカル除外)へ登録して preflight の未追跡検査から除外し、Verifier は除外済みファイルへの変更有無を別途検査する。TESTCASE T-217 はこの前提で記述した。
- **状態**: 暫定方針で続行(SPEC.md の修正候補)

## Q-07: 実装済み single-task gate（`allow_task_chaining`）の設計判断（2026-07-16）

本項は SPEC.md 記載の大規模な Planner/session resume 設計とは別に、`controller.py` へ実際に実装・出荷された small-scope 機能についての記録である。QandA Q-01〜Q-06 が扱う `InstructionFrontMatter`/`TaskStatus`（8値）とは別物で、`task_id`/`status`（5値: pending/in_progress/completed/blocked/failed）のみを読む軽量な front matter ゲートである。

- **なぜ `allow_task_chaining` の既定値を `true` にするか**: この機能追加より前に書かれた既存の `config.json` は `allow_task_chaining` キー自体を持たない。既定を `false`（ゲート有効）にすると、`task_file` が存在しない既存プロジェクトでゲートが即座に `task_gate_invalid` を返し、既存の「Agentが自由にタスクを選ぶ」運用を無条件に壊してしまう。既定を `true`（従来動作）にすることで、キーを追加しない限り挙動が変わらないことを保証する。
- **なぜ `task_file` をdirty-worktree検査から除外しないか**: `task_file` の `status` 変更はAutoLoopにとって「AutoLoop自身の内部帳簿（`.runtime/`・`.autoloop/`）」ではなく、そのタスクが完了・保留・失敗したという実質的な作業成果である。除外すると、Agentが `status: completed` に書き換えた事実を次回起動時に見落とし、人間や別Agentが古いタスク状態のまま次の判断をしてしまう危険がある。
- **なぜ `task_id` の変更を自動承認しないか**: ゲートの目的は「指示書に書かれた1件だけを実行する」ことであり、Agent自身が `task_id` を書き換えられるなら、承認されていない別タスクへ自己判断で移ることを防げず、ゲートの意味がなくなる。`task_id` が変化した場合は無条件で `human_confirmation`（exit 2）とし、人間の確認なしに次のタスクへ進ませない。
- **なぜ `task_file` 不正時に fail close するか（`task_gate_invalid`）**: ゲートファイルが存在しない・読めない・front matter が壊れている・`task_id`/`status` が空または未知の値である場合、どのタスクが承認されているかをプログラムが確定できない。この状態でAgentを起動すると、承認されていない作業を実行してしまう可能性があるため、判断不能な場合は常に「起動しない」側に倒す（`no_pending_task` ではなく `task_gate_invalid`、exit 1）。
- **`pending` と `in_progress` をどう扱うか**: 両方を「着手が承認された状態」として同一に扱う。サイクル開始時・Agent実行後の継続判定のどちらでも、`pending` と `in_progress` は同じ経路（Agent起動 / `continue`）に合流する。`in_progress` は `pending` の同義語として、Agentが「作業に着手したが未完了」であることをより具体的に示したいときに使う目的だけを持つ。既存の `completed`/`blocked`/`failed` はいずれも「今は着手できない・停止する」側に分類する。

**変更ファイル**: `src/oracle_council/...` に相当する対象はなし（本リポジトリの対象は `controller.py`/`controller_tests.py`/`README.md`/`config.example.json`/`install.ps1`/本ファイルのみ）。排他ロック、Planner、tasks.yamlキュー、複数Agent、フェイルオーバー、worktree分離は本項の対象外で未着手のまま。
