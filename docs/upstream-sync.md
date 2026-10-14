# フォーク元への自動追随

`KazumaProject/JapaneseKeyboard` の正式リリースを毎日 03:23 UTC に確認し、
更新PRを作成します。ビルド・テストに成功した更新だけを `dev` に自動マージし、
署名付きAPKをGitHub Releasesに自動公開します。
Actions の **Sync upstream release → Run workflow** でも実行できます。
スケジュールは既定ブランチ `dev` で有効になり、実行時刻はGitHubの混雑で遅れる場合があります。

## 取り込み方法

- `.github/upstream.json` に取り込んだタグとコミットを記録します。初期値は `v1.7.128` です。
- 正式リリースの最新版だけを対象とし、プレリリースや開発ブランチには追随しません。
- 通常のGitマージで、`com.example.sumire` のアプリID、署名設定、Sumire Sync連携を保持します。
- PRブランチは `upstream/<タグ>-<devのSHA先頭12文字>`。同じ更新の再実行でforce pushは行いません。
- **Merge commit** で上流の履歴を保持します。更新PRを手動マージする場合もSquash/Rebaseは使わないでください。
- 検証済みの `dev` コミットから、上流と同名のタグ（例: `v1.7.128`）とGitHub Releaseを作成します。
- 更新がない日も未公開リリースを確認し、公開に失敗していれば再試行します。
- 手動のフォーク版（例: `1.7.128.2`）は定期実行では公開しません。
- 手動版の後に新しい上流が出た場合、`app/build.gradle` のバージョン名は新しい上流に合わせ、
  `versionCode` は上流値と現在のフォーク値+1の大きい方にします。バージョン番号だけの競合は処理しますが、
  その他の設定・コードの競合は従来どおり停止して手動確認を求めます。

## 自動マージの条件

同一ワークフロー内で以下を実行し、すべて成功した場合のみ検証済みSHAをマージします。
`GITHUB_TOKEN` が作成したPRに別のPRワークフローが起動することには依存しません。

1. 更新スクリプトの単体・ローカルGit統合テスト
2. フォークのアプリID、signature権限、Provider、通知呼び出しの保持チェック
3. `:app:assembleLiteStandardDebug`
4. `:app:testLiteStandardDebugUnitTest`（Sync bridgeの既存テストを含む）
5. `:app:lintLiteStandardDebug`

Full版のモデル取得・ネイティブビルド、実機/エミュレータでの入力・IPC動作はこの条件には含まれません。
検証用ジョブには書き込みトークンや署名Secretを渡しません。
マージ直前にPRのSHAと `dev` のSHAを再確認します。ブランチ保護や必須レビューを迂回しません。

## APKの自動公開

`Sync upstream release` が `Android Release CI` を再利用ワークフローとして直接呼び出します。
`GITHUB_TOKEN` によるタグ作成だけでは別ワークフローが起動しないため、タグpushイベントには依存しません。
手動の場合は **Android Release CI → Run workflow → ref: dev** で実行できます。
公開対象は `dev` に取り込み済みのコミットに限定し、公開直前にも同一SHAでLiteビルド・全単体テスト・Lintを実行します。

- Full Standard: arm64-v8a / x86_64
- Lite Standard: arm64-v8a / x86_64
- Lite F-Droid: universal
- `SHA256SUMS`: 上記5つのAPKのハッシュ

5つのAPKをすべてビルドし、署名検証、以前の公開済みFull arm64 APKとの証明書一致、
アプリID、バージョン名・コードを確認します。versionCodeが以前より増えていなければ停止します。
署名ビルドのみに既存Secretを渡し、Gradle configuration cacheには保存しません。
署名付きビルドと公開は別ジョブとし、書き込みトークンは公開ジョブだけに渡します。

ビルド成功後にタグを作成し、ドラフトに6ファイルをアップロード、GitHub上のハッシュを照合してから公開します。
公開済みの同じバージョンはスキップし、タグの移動や公開済みAPKの置き換えは行いません。
中断したドラフトは同じタグのコミットから再ビルドして復旧します。
初回の署名比較用に、既存の正常な公開リリースが必要です。

## フォーク独自の手動リリース

上流の正式リリースを待たずに、`dev` に統合した変更を公開する場合の手順です。
ビルド専用ブランチは不要です。通常の機能統合や `dev` へのpushだけでは公開されません。

1. 公開する変更を検証して `dev` に統合します。
2. リリース準備用PRで `app/build.gradle` の `versionName` を、取り込み済みの上流番号に
   正の整数を1つ追加した番号（例: `1.7.128.2`）へ変更します。
   `versionCode` も過去に配布したAPK（一時ビルドを含む）から増やします。
   `.github/upstream.json` は上流の記録なので変更しません。
3. 検証・統合後、そのコミットに対応するタグ（例: `v1.7.128.2`）を作成してpushするか、
   **Android Release CI → Run workflow → ref: 対象コミットSHA** を実行します。
   どちらも検証後に署名付きAPKをGitHub Releasesへ公開する操作です。

タグ・アプリバージョンの一致と、上流番号の一致（例: 上流 `v1.7.128` に対して `1.7.128.2`）を確認します。
公開対象のコミットは `origin/dev` に統合済みである必要があります。
既存タグは動かさず、公開済みAPKも上書きしません。再実行は既存タグのコミットを使います。
新しい上流（例: `v1.7.129`）への自動追随と公開は、従来どおり継続します。

リリースをまだ行わない場合は、バージョン変更・タグpush・Android Release CIの手動実行をせず、
追加修正を通常のPRで `dev` に統合してください。

## GitHub側の設定

- Actionsを有効にしてください（フォークでは無効になっている場合があります）。
- Settings → Actions → General → Workflow permissions の
  **Allow GitHub Actions to create and approve pull requests** を有効にします。
  デフォルト権限はreadのままで構いません。必要な書き込み権限はジョブごとに指定しています。
- Settings → General の **Allow merge commits** が必要です。
- GitHubの「Allow auto-merge」設定やPATは不要です。検証後のジョブがマージAPIを呼びます。
- 必須レビュー等を設定した場合、その条件が満たされるまでマージAPIは失敗します。
- 署名Secret: `KEYSTORE_BASE64` / `KEYSTORE_PASSWORD` / `KEY_ALIAS` / `KEY_PASSWORD`。
  未設定の場合は対応する `ANDROID_KEYSTORE_*` / `ANDROID_KEY_*` にフォールバックします。
  既存リリースと同じ署名を使ってください。必要なモデルに認証が必要な場合は `HF_TOKEN` も設定します。

## 停止した場合

競合、上流履歴の書き換え、連携の欠落、自動化コードの変更、テスト失敗があれば停止します。
Actionsの失敗ログとアップロードされた `fork-validation-*` のレポートを確認してください。
競合時には正常な更新コミットを作れないためPRは作成されず、Actionsに競合ファイルを表示します。
Actionsの通知設定で失敗通知を受け取れるようにしてください。

- 一時的なビルド失敗: 同じワークフローを再実行します。
- マージ後の公開失敗: 次回の自動追随で再試行されます。すぐ復旧する場合は
  `Android Release CI` を手動実行します。署名・バージョン不一致なら原因を修正してから再実行してください。
- 公開済みリリースの欠損: 自動上書きをせず停止します。既存のタグとAPKを確認し、手動で復旧してください。
- 検証中に `dev` が進んだ: 新しいワークフローを開始します。旧PRは手動で閉じてください。
- 競合やコード修正が必要: 通常の作業ブランチで上流タグをマージ・修正し、
  `.github/upstream.json` を更新してPRを作成します。手動更新PRにも Fork validation が実行されます。
- 既存の自動更新ブランチを手で変更・PRを閉じた: 自動処理はその状態を上書きしません。
  手動で検証・マージするか、不要なPR/ブランチを整理してから再実行します。

停止するにはActionsで **Sync upstream release → Disable workflow** を選択します。
