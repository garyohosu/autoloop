# dream.md

## 2026-07-14 17:00 Dreamingタイム

### 今回やったこと
- AutoLoop controllerへ安全修正を実装: サイクル採番の継続(既存runtime最大値+1、上書き禁止)、dirty worktree既定拒否(`dirty_worktree`)、`allow_dirty_worktree`/`allowed_dirty_paths`設定、既存dirtyファイルのSHA-256保護(`protected_dirty_changed`)、終了コード仕様(成功=0、human_confirmation=2、失敗=1)
- テストを19件追加(合計45件、全成功)。README・config.example.jsonを更新
- 専用テストリポジトリ(C:\PROJECT\autoloop-fieldtest)で実地試験: dirty拒否とクリーン実行(cycle-003、exit 0)の両方を確認
- `1c5f2dc`としてコミットし、origin/mainへfast-forward push

### 気づいたこと
- 作業中にリモートが強制更新され(サニタイズ版公開)、ローカル履歴と分岐した。旧mainはbackup-local-20260714ブランチに退避し、サニタイズ版へ修正を移植して解決
- npmシムの`codex`(.ps1)は`shell=False`のPopenから起動できない。`node.exe + codex.js`の直接指定が必要
- fieldtestのdirty状態がそのまま新ゲートの実地検証になった(狙いどおりagent未起動で停止)

### 改善点
- config.jsonのagent.commandはOS依存の起動形式差(シム問題)をREADMEに明記すると親切
- protected判定は「run開始時のdirty集合」を基準にしており、連続モードでagentが作ったファイルは保護対象にならない(仕様どおりだが明文化した)

### 次に試すとよさそうなこと
- OracleCouncil本体ではなく`git worktree`でのクリーン実地試験(未コミット21件があるため)
- human_confirmation終了コード2を利用した呼び出し側(oracle-council等)のハンドリング
- タイムアウト時の子プロセス完全クリーンアップ(既知の制限)
