# SEQUENCE.md — 汎用自動継続開発ランナーのシーケンス設計

対象: `SPEC.md` v0.1.0 / `USECASE.md`
前提: QandA Q-01(Phase 1〜2 は人間が指示書作成)、Q-03(`result_commit` は Executor 時点で null)、Q-04(ready 検査は PREFLIGHT と PLAN_VERIFY の両方)

---

## 1. メインフロー(1タスクの正常系)

UC-01/UC-02/UC-03/UC-04/UC-05/UC-06 に対応する。SPEC §8 の状態遷移(IDLE→SYNCING→PREFLIGHT→PLANNING→PLAN_VERIFY→EXECUTING→RESULT_VERIFY→COMMITTING→PUSHING→NEXT_DECISION)を1本のシーケンスで表す。

```mermaid
sequenceDiagram
    autonumber
    actor Dev as 開発者/スケジューラ
    participant CT as Controller
    participant ST as State Store<br>(state.json/lock/logs)
    participant GL as ローカルGit
    participant GH as GitHub(origin)
    participant PL as Planner<br>(Codex CLI)
    participant EX as Executor<br>(Claude Code)

    Dev->>CT: 起動
    CT->>ST: runner.lock 作成(pid/hostname/task_id)
    alt 既存PIDが生存
        CT-->>Dev: 終了(二重起動防止 UC-10)
    end

    Note over CT,GH: SYNCING / PREFLIGHT(SPEC §9)
    CT->>GL: git status --short / git diff --check
    CT->>GH: git fetch origin main
    CT->>GL: git pull --ff-only
    CT->>GL: rev-parse HEAD / origin-main 一致確認
    CT->>CT: 既存指示書があれば ready / expected_base_commit / task_id二重実行を検査(Q-04)
    alt dirty / ff不可 / HEAD不一致 / 指示書検査NG
        CT->>ST: PAUSED_DIRTY_WORKTREE 等を記録・lock解放
        CT-->>Dev: 停止(自動復旧しない)
    end

    alt Phase 1〜2(人間作成の ready 指示書を使用)
        Note over CT: PLANNINGをスキップし検査済み指示書で続行(Q-01)
    else Phase 3以降(Planner が指示書を生成)
        Note over CT,PL: PLANNING / PLAN_VERIFY(SPEC §12)
        CT->>PL: 非対話起動(result.md/FIX_PLAN.md/hikitsugi.md 等を読ませる)
        PL->>GL: 正本ファイル・git log を読む
        PL->>PL: 次作業を1件選定し instructions.md 生成
        PL-->>CT: {"decision":"ready","next_task_id":"X-8.20"}
        CT->>CT: Planner結果Schema検証 + ready/expected_base_commit/task_id二重実行検査(Q-04)
        alt decision が approval_required / blocked / release_ready
            CT->>ST: 状態記録・lock解放
            CT-->>Dev: 停止(UC-09)
        end
    end

    Note over CT,EX: EXECUTING(SPEC §11/§13)
    CT->>EX: 非対話起動(初回)またはsession ID指定でresume
    EX-->>CT: 初回出力からsession ID
    CT->>ST: session ID / resume_count を state.json へ保存
    EX->>GL: 指示書確認・allowed_paths内のみ変更
    EX->>EX: required_tests 実行(py -m pytest)
    EX->>GL: git diff --check
    EX->>GL: instructions/result.md へ結果ブロック追記(result_commit: null、Q-03)
    EX-->>CT: 終了(exit code + 構造化stdout)

    Note over CT,GL: RESULT_VERIFY(SPEC §14)
    CT->>GL: git diff --name-only / --stat / --check
    CT->>CT: allowed_paths検査・禁止ファイル検査・結果ブロック完全性・テスト結果一致
    alt 検証失敗(unexpected_change / テスト失敗 / 結果ブロック不完全)
        CT->>ST: 失敗分類を記録(commitしない)・lock解放
        CT-->>Dev: 停止(UC-09)
    end

    Note over CT,GH: COMMITTING / PUSHING(SPEC §15)
    CT->>GL: git add -- <許可された個別ファイル>
    CT->>GL: git commit -m <commit_message>
    CT->>GH: git push origin main
    CT->>GL: HEAD と origin/main の一致確認・clean確認
    alt push失敗 / 不一致
        CT->>ST: 状態記録・lock解放
        CT-->>Dev: 停止
    end
    CT->>ST: result_commit SHA を logs/result.json と state.json に記録(Q-03)
    CT->>ST: タスクを completed として記録

    Note over CT: NEXT_DECISION(SPEC §8/§18)
    alt READY_NEXT かつ 5タスク未満
        CT->>CT: PLANNING へ戻る
    else 停止条件(approval_required/blocked/release_ready/5タスク完了 等)
        CT->>ST: lock解放・最終状態記録
        CT-->>Dev: 停止
    end
```

---

## 2. 障害分類と fallback(UC-07 / UC-08)

SPEC §16(利用枠切れと障害分類)・§17(Agent間引継ぎ)・§10(セッション管理)の分岐。

```mermaid
sequenceDiagram
    autonumber
    participant CT as Controller
    participant ST as State Store
    participant EX as Executor<br>(Claude Code)
    participant FB as Fallback Executor<br>(Codex CLI)
    participant GL as ローカルGit

    CT->>EX: 実行中(EXECUTING)
    EX-->>CT: 異常終了(exit code / stdout / stderr)
    CT->>GL: git status / diff で現状確認
    CT->>CT: 終了コード+構造化stdout+stderr+Git状態で分類(SPEC §16)

    alt usage_limit(利用枠切れ)
        CT->>ST: 差分を保存(破棄しない)・PAUSED_USAGE_LIMIT
        alt fallback_executor が許可されている
            CT->>CT: 引継ぎプロンプト生成(Git diff/instructions/result/hikitsugi/停止分類/実行済みテスト、SPEC §17)
            CT->>FB: 新規セッションで起動(会話コンテキストは渡さない)
            FB->>GL: git status/diff/log と正本ファイルを確認
            FB->>GL: 未完了部分だけを続行(重複実行禁止)
            FB-->>CT: 終了 → RESULT_VERIFY へ
        else fallback不可
            CT-->>CT: 停止
        end
    else context_limit
        CT->>EX: 同じAgentの新規セッション作成(session rotation)
        CT->>ST: rotationとして記録(resume回数に数えない)
        EX->>GL: Gitと引継ぎファイルから状態復元・未完了のみ続行
    else transient_error(rate limit / timeout 等)
        loop 最大2回(30秒→120秒待機)
            CT->>EX: 同じsession IDでresume(resume時共通指示 SPEC §10.3)
        end
        alt 3回目も失敗
            CT-->>CT: 停止 または fallback へ
        end
    else authentication_error
        CT->>ST: approval_required / blocked を記録
        CT-->>CT: 停止(自動復旧しない)
    else safety_classifier_unavailable
        alt 読み取りのみで完了可能
            CT->>EX: 継続
        else 実装・commit・pushが必要
            CT-->>CT: 停止(fallback引継ぎは許可)
        end
    end
```

---

## 3. セッション resume / rotation(UC-07)

```mermaid
sequenceDiagram
    autonumber
    participant CT as Controller
    participant ST as State Store
    participant AG as Agent(Claude/Codex)

    CT->>ST: state.json から session ID / resume_count を読む
    alt rotation条件(context limit / resume>max / 同一セッション5タスク / 系列変更 / 状態取得不能 / Agent推奨 / リリース前レビュー)
        CT->>AG: 新規セッション起動
        AG-->>CT: 新session ID
        CT->>ST: session ID更新・task_count_in_session=0
        Note over AG: 過去会話ではなくGitと引継ぎファイルから状態復元(SPEC §10.4)
    else resume可能
        CT->>AG: session ID明示でresume(--last/--continueは使わない)
        Note over AG: resume時共通指示: git status/diff/log と正本ファイルを読み直し、完了済み作業を重複実行しない(SPEC §10.3)
        CT->>ST: resume_count をインクリメント
    end
```

---

## 4. 補足

- 図1の Planner 起動(PLANNING)は Phase 3 以降。Phase 1〜2 では開発者が事前に `status: ready` の指示書を commit しておき、PREFLIGHT で検査する(QandA Q-01/Q-04)。
- live 実行を含むタスクは Planner が `approval_required` を返して停止し、人間が承認済み指示書 revision を commit するまで進まない(SPEC §19、UC-11)。失敗した live 実行は resume/fallback で自動再試行しない。
- 図2の fallback 起動は常に新規セッションで行い、前 Agent の会話コンテキストは渡さない(SPEC §17)。
- Controller はどの経路で停止する場合も、最終状態を state.json・logs に記録したうえで `runner.lock` を解放してから終了する(図2の各停止分岐にも適用)。PAUSED_* での停止も同様で、再開時は新しい Controller 起動が lock を取り直す。プロセス異常終了で残った lock は stale lock として解除できる(SPEC §20)。
