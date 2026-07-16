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

## Q-08: 実装済み排他ロック（RepositoryLock）の設計判断（2026-07-16）

本項はQ-07（single-task gate）と同様、SPEC.md §20記載の`runner.lock`設計（`<AUTOMATION_ROOT>`側、task_id込みスキーマ）とは別に、`controller.py`へ実際に実装・出荷された`RepositoryLock`についての記録である。

- **なぜロックを対象リポジトリ側（`.autoloop/run.lock`）へ置くか**: ロックの目的は「同じ対象Gitリポジトリへの同時操作を防ぐ」ことであり、単位は対象リポジトリそのものである。AutoLoop本体（`C:\PROJECT\autoloop`）側や自動化用の別ディレクトリへ置くと、同じ対象リポジトリを異なる`config.json`・異なる起動元・異なるAutoLoopのコピーから操作した場合にロックが共有されず、排他制御そのものが機能しない。対象リポジトリのGitルートを正規化した絶対パスを`repository`フィールドの正本とすることで、相対パス・末尾区切り文字・Windowsの大文字小文字・シンボリックリンク経由などの別表記でも同じロックへ収束させている。
- **なぜ既存`.runtime/autoloop.lock`（Lockクラス）を置き換えるか**: 旧実装はpidと起動時刻だけを記録し、生存確認を一切行わなかった。クラッシュ後に残ったロックファイルは、次回起動が永久に「another AutoLoop is already running」で失敗し続ける原因になり、人間が手動でファイルを削除する以外に復旧手段がなかった。`RepositoryLock`は生存確認・stale判定・明示解除コマンドを備え、この復旧不能な状態を解消する。旧ロックファイル（`.runtime/autoloop.lock`）が既存プロジェクトに残っていても自動削除・自動移行はしない。それは単なる旧runtimeディレクトリ内の残骸であり、新しい`RepositoryLock`は`.autoloop/run.lock`のみを見るため、共存していても機能上の問題は生じない。
- **なぜstaleを自動削除しないか**: 起動のたびに自動的にstale判定・削除を行うと、同時に起動しようとした別プロセスがまだロックファイル書き込みの途中（あるいは起動直後でまだ生存確認に反映されていない）場合に、誤って有効なロックを削除してしまう競合状態を生みかねない。stale判定とその解除を`-UnlockStale`という別個の明示操作に分離することで、人間（または自動化ラッパー）が「本当に終わっているか、単に遅いだけか」を確認したうえで意図的に解除する運用を強制する。
- **なぜPIDだけで判定しないか（PID再利用対策）**: OSはプロセス終了後に同じPID番号を別プロセスへ再利用することがある。PIDの存在有無だけで生存確認すると、元のAutoLoopプロセスがクラッシュした直後にOSが同じPIDを別の無関係なプロセスへ割り当てた場合、誤って「まだ生きている」と判定してしまう。Windowsでは`GetProcessTimes`（`ctypes`経由、追加ライブラリ不要）によるプロセス開始時刻を記録・比較し、PIDが存在してもその開始時刻がロック記録と一致しない場合は「別プロセスによるPID再利用＝実質的にstale」と分類する。
- **なぜforeign_hostをactive相当として扱うか**: `hostname`が現在のマシンと異なるロックは、そもそもこのマシンからPIDの生存確認ができない（別マシンのプロセステーブルは参照不可能）。判定不能な状態を「たぶん大丈夫だろう」と楽観的に扱うと、共有ドライブやネットワーク越しの運用で複数マシンが同時に同じ対象リポジトリを操作してしまう危険がある。安全側に倒し、判定不能な remote lock は常にブロックする（`active`と同じ扱い、自動削除しない）。
- **なぜ人間やIDEによる直接編集は防げないか**: このロックは`controller.py`が読み書きする`.autoloop/run.lock`というファイル上の合意であり、OS自体のファイル編集権限を制限するものではない。人間が別のエディタ・IDE・Claude Code等の別AIセッションで同じ作業ツリーを直接編集する行為は、そもそも`controller.py`を経由しないため、ロックの対象にならない。ロック取得時にstderrへ警告（"Do not edit this worktree from another AI coding session or IDE until AutoLoop exits."）を表示するのはこのための運用上の注意喚起であり、技術的な強制力はない。
- **worktree分離との違い**: worktree分離（対象リポジトリの作業ツリーとは別の専用worktree/ブランチでAutoLoopを動かす）は、人間や別AIセッションが元の作業ツリーを触っても物理的に競合しなくなる、より強い技術的保証である。今回実装した排他ロックは「AutoLoop同士の二重起動防止」という狭い範囲の問題を解決するものであり、worktree分離が提供するであろう「人間の直接編集からの隔離」までは提供しない。worktree分離は本項の対象外で、次の推奨作業として別途実装する。

**変更ファイル**: `controller.py`、`controller_tests.py`、`README.md`、`SPEC.md`、`USECASE.md`、`SEQUENCE.md`、`CLASS.md`、`TESTCASE.md`（実装状況注記のみ）、`install.ps1`、`examples/run-autoloop.ps1`、本ファイル。Planner、tasks.yamlキュー、複数Agent、フェイルオーバー、worktree分離、single-task gate仕様そのものの変更、OracleCouncilはいずれも対象外・未着手。
