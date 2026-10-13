# フォーク元への自動追随

`KazumaProject/JapaneseKeyboard` の正式リリースを毎日 03:23 UTC に確認し、
更新PRを作成します。ビルド・テストに成功した更新だけを `dev` に自動マージします。
Actions の **Sync upstream release → Run workflow** でも実行できます。
スケジュールは既定ブランチ `dev` で有効になり、実行時刻はGitHubの混雑で遅れる場合があります。

## 取り込み方法

- `.github/upstream.json` に取り込んだタグとコミットを記録します。初期値は `v1.7.128` です。
- 正式リリースの最新版だけを対象とし、プレリリースや開発ブランチには追随しません。
- 通常のGitマージで、`com.example.sumire` のアプリID、署名設定、Sumire Sync連携を保持します。
- PRブランチは `upstream/<タグ>-<devのSHA先頭12文字>`。同じ更新の再実行でforce pushは行いません。
- **Merge commit** で上流の履歴を保持します。更新PRを手動マージする場合もSquash/Rebaseは使わないでください。
- `dev` への統合までが対象です。APKの署名配布・リリースタグ作成は別途実施します。

## 自動マージの条件

同一ワークフロー内で以下を実行し、すべて成功した場合のみ検証済みSHAをマージします。
`GITHUB_TOKEN` が作成したPRに別のPRワークフローが起動することには依存しません。

1. 更新スクリプトの単体・ローカルGit統合テスト
2. フォークのアプリID、signature権限、Provider、通知呼び出しの保持チェック
3. `:app:assembleLiteStandardDebug`
4. `:app:testLiteStandardDebugUnitTest`（Sync bridgeの既存テストを含む）
5. `:app:lintLiteStandardDebug`

Full版のモデル取得・ネイティブビルド、実機/エミュレータでの入力・IPC動作はこの条件には含まれません。
ビルド用ジョブには書き込みトークンや署名Secretを渡しません。
マージ直前にPRのSHAと `dev` のSHAを再確認します。ブランチ保護や必須レビューを迂回しません。

## GitHub側の設定

- Actionsを有効にしてください（フォークでは無効になっている場合があります）。
- Settings → Actions → General → Workflow permissions の
  **Allow GitHub Actions to create and approve pull requests** を有効にします。
  デフォルト権限はreadのままで構いません。必要な書き込み権限はジョブごとに指定しています。
- Settings → General の **Allow merge commits** が必要です。
- GitHubの「Allow auto-merge」設定やPATは不要です。検証後のジョブがマージAPIを呼びます。
- 必須レビュー等を設定した場合、その条件が満たされるまでマージAPIは失敗します。

## 停止した場合

競合、上流履歴の書き換え、連携の欠落、自動化コードの変更、テスト失敗があれば停止します。
Actionsの失敗ログとアップロードされた `fork-validation-*` のレポートを確認してください。
競合時には正常な更新コミットを作れないためPRは作成されず、Actionsに競合ファイルを表示します。
Actionsの通知設定で失敗通知を受け取れるようにしてください。

- 一時的なビルド失敗: 同じワークフローを再実行します。
- 検証中に `dev` が進んだ: 新しいワークフローを開始します。旧PRは手動で閉じてください。
- 競合やコード修正が必要: 通常の作業ブランチで上流タグをマージ・修正し、
  `.github/upstream.json` を更新してPRを作成します。手動更新PRにも Fork validation が実行されます。
- 既存の自動更新ブランチを手で変更・PRを閉じた: 自動処理はその状態を上書きしません。
  手動で検証・マージするか、不要なPR/ブランチを整理してから再実行します。

停止するにはActionsで **Sync upstream release → Disable workflow** を選択します。
