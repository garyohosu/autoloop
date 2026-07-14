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

## 2026-07-14 18:15 Dreamingタイム

### 今回やったこと
- README自律導入機能を公開版へ復元: `install.ps1`(Agent自動検出、WindowsのCodexはnode.exe+codex.jsへ解決)、README冒頭のAI CLI向け指示・Quick Start・最短プロンプト
- HUMAN_CONFIRMATION誤検知修正(「不要」等の否定行を除外)と、`.autoloop/`をdirty判定から除外する修正(インストール直後の`-Once`が動くように)
- テスト48件全成功。専用テストリポジトリで install→dirty拒否→クリーン実行(exit 0)を実地確認
- `1ecc81f feat: restore README-driven AutoLoop setup` をorigin/mainへpush

### 気づいたこと
- インストール直後は`.autoloop/`が未追跡になり旧仕様ではdirty拒否される、という導線の欠陥をREADME理解テストの設計中に発見できた
- サンドボックス内の非対話エージェントはGitHub取得・keyring認証・ネストCLI起動ができず、URLだけ渡す無人E2Eは環境依存で完走しない
- `codex exec`はstdinがパイプのまま開いていると起動待ちでハングする(`$null |`で回避)

### 改善点
- README理解テストは対話型AI CLI(Web取得可能)で行うのが現実的
- installerの生成する`verification_commands`既定は対象プロジェクトに合わせて調整が必要

### 次に試すとよさそうなこと
- 対話型AI CLIに最短プロンプトだけを渡す実地E2E(人間の画面で確認)
- installerにmacOS/Linux向けシェル版を用意するか検討
