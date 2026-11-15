# Azure App Service（Linux）への公開

この手順は**利用者が公開するときに実行するもの**です。ローカルの実装・テストではAzureリソースの作成やデプロイを行っていません。本番のDBとファイルはSupabaseに保存し、Web Appのローカルディスクに研究データを永続保存しません。

## 1. 公開前にローカルでそろえる

1. 提供原本`resources/worksheets/hiragana_collection_a4.pdf`と、全8ページのQR・順序・黒丸位置を記録した`config/pages.study.json`を初期設定として使用します。同意文は提供済みの版`2026-11-10-v1`です。`pages.test.json`と3ページのテスト用紙は公開用ではありません。以降は研究者画面の「配布用紙の管理」でPDFを複数登録し、配布するPDFをチェックして確定します。
2. READMEのSupabase手順に従い、SQL Editorで`migrations/001_initial.sql`、`migrations/002_worksheet_revisions.sql`、`migrations/003_expire_empty_participants.sql`を順に実行し、非公開の`handwriting-private`バケットを確認します。既存環境には未適用のSQLだけを追加実行します。004は写真0枚のIDの自動削除を有効にするSQLです（下記参照）。
3. `scripts/set_password.py`で本番の研究者パスワードハッシュを生成します。開発用パスワードをそのまま流用しないでください。
4. 次の鍵生成コマンドを2回実行し、研究者セッション用の`SESSION_SECRET`と、参加者リンク用の`PARTICIPANT_TOKEN_SECRET`へ別々の値を設定します。パスワード管理ツールなどで保管し、参加者用の鍵は収集期間中に変更しません。

既存DBを更新するときは、002適用後の初回起動で以前の`PAGE_CONFIG`を維持し、既存参加者を以前の用紙版へ紐付けます。研究者画面で正式原本を確認・確定してから初期設定ファイルを変更します。テスト版からの移行は開発モードで済ませてから本番起動してください。本番モードはDB内の現在版も検証するため、環境変数だけを本番へ変えても、テスト版の配布を開始しません。

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
.\.venv\Scripts\python.exe -m pytest -q
```

Supabase接続先は**Connect → Session pooler**の5432番ポートを使います。AzureからIPv4で接続でき、`DATABASE_URL`のスキームを`postgresql+psycopg://`、SSLを`?sslmode=require`にします。パスワードの記号はURLエンコードしてください。TLS暗号化に加えて証明書の検証を必須にする場合は、Supabaseの証明書を配置して`sslmode=verify-full&sslrootcert=...`にします。[Supabase公式接続ガイド](https://supabase.com/docs/guides/database/connecting-to-postgres)

## 2. Web Appを作成する（公開時のみ）

[Azure Portal](https://portal.azure.com)で **リソースの作成 → Web App** を開き、次を指定します。

| 項目 | 入力する値 |
| --- | --- |
| サブスクリプション | 利用する契約 |
| リソースグループ | 例：`handwriting-research-rg` |
| 名前 | 全世界で重複しない任意の名前。例：`handwriting-研究室固有文字列`（実際は使用可能な英数字とハイフン） |
| 公開 | コード |
| ランタイムスタック | Python 3.12 |
| オペレーティングシステム | Linux |
| リージョン | 研究環境で利用する地域。例：Japan East |
| App Serviceプラン | 画像補正を動かせる有料プラン（例：Basic B1以上）。確認してから作成 |

リソース作成後は課金が発生する場合があります。Python 3.12が選べることをPortalで確認してください。以降は`<APP_NAME>`を作成した名前に置き換えます。Web Appの **設定 → 構成／全般設定** でHTTPSのみを有効にします。

Azure CLIを使う場合の同等の例です。以下の作成コマンドを実行するまではリソースは増えません。

```powershell
az login
$researchGroup = "handwriting-research-rg"
$researchPlan = "handwriting-research-plan"
$researchApp = "YOUR-GLOBALLY-UNIQUE-APP-NAME"
az group create --name $researchGroup --location japaneast
az appservice plan create --name $researchPlan --resource-group $researchGroup --is-linux --sku B1
az webapp create --name $researchApp --resource-group $researchGroup --plan $researchPlan --runtime "PYTHON:3.12"
az webapp update --name $researchApp --resource-group $researchGroup --https-only true
```

## 3. 環境変数を入力する

Web Appの **設定 → 環境変数 → アプリ設定** を開き、次を1件ずつ登録して保存します。Portalの言語や表示更新によっては **構成 → アプリケーション設定** と表示されます。値全体を囲む引用符は入れません。パスワードハッシュの`$`はそのまま入力します。

| 名前 | 本番で入力する値 |
| --- | --- |
| `APP_ENV` | `production` |
| `BACKEND_MODE` | `supabase` |
| `APP_BASE_URL` | `https://<APP_NAME>.azurewebsites.net`。独自ドメイン利用時はそのHTTPS URL |
| `ALLOWED_HOSTS` | `<APP_NAME>.azurewebsites.net`。独自ドメインも利用する場合はカンマ区切りで追加 |
| `PAGE_CONFIG` | `config/pages.study.json`。新規DBの初期用紙設定。初期化後の配布版はDBで管理 |
| `COOKIE_SECURE` | `true` |
| `SESSION_SECRET` | 先ほど生成した本番専用のランダム文字列 |
| `PARTICIPANT_TOKEN_SECRET` | セッション鍵とは別に生成した32文字以上のランダム文字列。収集期間中は固定 |
| `RESEARCHER_PASSWORD_HASH` | `scripts/set_password.py`の出力の右辺。外側の引用符は除く |
| `DATABASE_URL` | `postgresql+psycopg://postgres.<PROJECT_REF>:<URL_ENCODED_PASSWORD>@<POOLER_HOST>:5432/postgres?sslmode=require` |
| `SUPABASE_URL` | `https://<PROJECT_REF>.supabase.co` |
| `SUPABASE_SECRET_KEY` | Supabaseのサーバー用Secret key（`sb_secret_...`）。Publishable keyを使わない |
| `SUPABASE_STORAGE_BUCKET` | `handwriting-private` |
| `DATA_DIR` | `/tmp/handwriting`。本番で研究データをここへ保存する設定にはなりません |
| `MAX_UPLOAD_MB` | `25` |
| `SCM_DO_BUILD_DURING_DEPLOYMENT` | `true`。ZIPデプロイ時にrequirements.txtから依存関係をインストール |

従来のJWT形式のキーだけがある場合は`SUPABASE_SECRET_KEY`の代わりに`SUPABASE_SERVICE_ROLE_KEY`へservice_roleキーを入力できます。新しいキーを優先してください。研究者パスワードの平文、DBパスワード、秘密鍵をフロントエンド、Git、デプロイZIPに含めません。

App Serviceのアプリ設定は実行時の環境変数として渡されます。秘密値をさらに分離して保管したい場合はAzure Key Vault参照も使えます。[App Service設定](https://learn.microsoft.com/azure/app-service/configure-common?tabs=portal)、[Key Vault参照](https://learn.microsoft.com/azure/app-service/app-service-key-vault-references)

## 4. 起動コマンドを設定する

Web Appの **設定 → 構成 → 全般設定 → スタートアップコマンド** に、次の1行を入力して保存します。

```text
gunicorn app.main:app --worker-class uvicorn_worker.UvicornWorker --bind 0.0.0.0:8000 --workers 2 --timeout 180 --access-logfile - --error-logfile -
```

リポジトリの`startup.sh`も同じ起動内容です。Portalのコマンド欄には`sh startup.sh`を設定しても動作します。`uvicorn-worker`はrequirements.txtでインストールします。複数workerでも参加者採番・再試行・提出の整合性はPostgreSQLの制約とトランザクションで守ります。

CLIで起動コマンドとビルド設定だけを登録する場合は、次を実行します。秘密値はコマンド履歴へ残さないようPortalから設定してください。

```powershell
az webapp config set --name $researchApp --resource-group $researchGroup --startup-file "gunicorn app.main:app --worker-class uvicorn_worker.UvicornWorker --bind 0.0.0.0:8000 --workers 2 --timeout 180 --access-logfile - --error-logfile -"
az webapp config appsettings set --name $researchApp --resource-group $researchGroup --settings SCM_DO_BUILD_DURING_DEPLOYMENT=true
```

FastAPIのPython 3.12アプリは起動コマンドを明示してデプロイします。[Microsoft公式Pythonデプロイガイド](https://learn.microsoft.com/en-us/azure/app-service/quickstart-python)

## 5. ZIPを作ってデプロイする

プロジェクトフォルダのPowerShellで次を実行します。

```powershell
.\.venv\Scripts\python.exe scripts/package_deploy.py --output artifacts/deploy.zip
az webapp deploy --name $researchApp --resource-group $researchGroup --src-path artifacts/deploy.zip --type zip
```

`package_deploy.py`はアプリのコード・静的ファイル・設定JSON・`resources/worksheets/`の初期配布PDF・requirements.txt・startup.shをまとめます。`.env`、仮想環境、参加者の元写真、ローカルDB、テスト成果物は含めません。`config/pages.study.json`と`hiragana_collection_a4.pdf`がZIPに含まれていることを確認します。原本PDFは認証を通したAPIから配布します。再デプロイ時は`--output artifacts/deploy-02.zip`のように別名を指定してください。既存ZIPは自動で上書きしません。

研究者画面から登録したPDFはSupabaseの非公開Storage、PDFごとの設定と配布対象の選択はPostgreSQLへ保存します。複数のPDFを保管し、任意の組み合わせを選べます。App Serviceの一時ディスクやデプロイZIPへ書き戻さないため、再起動・再デプロイ・複数workerでも保持されます。選択の変更は新規登録者から適用し、既存参加者のPDF一覧と必要ページは登録時の版を維持します。この複数PDF機能は既存の用紙版テーブルを使い、追加のSQL適用は不要です。

ビルドが終わったら **概要 → 再起動** を実行し、 **監視 → ログストリーム** を開いて起動エラーがないことを確認します。環境変数や秘密値をログに貼り付けないでください。CLIでログを開く場合は次です。

```powershell
az webapp log config --name $researchApp --resource-group $researchGroup --application-logging filesystem --level information
az webapp log tail --name $researchApp --resource-group $researchGroup
```

## 6. 公開後に実機で確認する

1. `https://<APP_NAME>.azurewebsites.net/api/health`を開き正常応答を確認します。
2. スマホで`https://<APP_NAME>.azurewebsites.net`を開き、同意・参加者番号発行・専用リンクの再開を確認します。同意後に記入用紙PDFをダウンロードでき、未認証では`/api/participant/worksheet`が拒否されることを確認します。
3. ダウンロードした用紙をA4・実際のサイズ（100%）・片面で印刷し、記入します。実際の全ページを順不同で撮影し、プレビューが正しい向きか、薄い鉛筆線や濁点が残っているか、外枠が切れていないかを確認します。
4. 途中で1枚不足したときの提出制限と、同じQRの差し替えを確認し、全ページをそろえて提出します。
5. `/researcher`へ本番パスワードでログインし、対象者と種類を指定してZIPをダウンロードします。PDF順、画像の対応、選ばなかった種類が入っていないことを確認します。
6. 別ブラウザの未認証状態で研究者APIを開き、拒否されることを確認します。SupabaseのStorageでバケットが非公開のままか確認します。
7. 研究者画面でPDFを複数登録し、配布するものをチェックして確定します。新規参加者に①・②…の個別ダウンロードボタンが表示され、すべてのPDFの必要ページが提出対象になることを確認します。登録済み参加者のPDF一覧が以前のままであることも確認します。同時に選択するPDF間でQRを重複させないでください。

本番の実写真・Supabase・Azureの資格情報がそろうまでは、この公開先での実機確認は未実施です。ローカルのテストだけを根拠に本番の原本寸法やスマホ全機種を保証しません。

## 起動しないとき

| 症状 | 確認する箇所 |
| --- | --- |
| 本番設定の検証エラー | `APP_ENV`、HTTPS URL、`ALLOWED_HOSTS`、十分な`SESSION_SECRET`、ハッシュ、Supabase設定、production用紙設定と校正済みフラグ |
| DBの配布版がテスト設定というエラー | 開発モードの研究者画面で正式原本を確認・差し替え確定してから、本番モードへ移行 |
| `ModuleNotFoundError` | ZIP直下にrequirements.txtとapp/があること。`SCM_DO_BUILD_DURING_DEPLOYMENT=true`で再デプロイ |
| DBへ接続できない | Session poolerのホストとユーザー名、URLエンコード済みパスワード、`sslmode=require`、Supabaseプロジェクトが起動中か |
| Storageで失敗 | バケット名、Private設定、サーバーSecret key、ファイルサイズ制限。Publishable keyに差し替えない |
| ログイン後すぐ未認証 | HTTPSで開いているか、`COOKIE_SECURE=true`、正しいホスト名、各workerが同じセッション鍵を使っているか |
| 写真処理に時間がかかる／worker終了 | ログストリームのメモリ・タイムアウトを確認し、App Serviceの性能を上げる。原本座標を変更して回避しない |

## 写真0枚のIDの自動削除

003で削除関数・期限切れ認証情報の表を作り、004でSupabase Cronの`hiragana-purge-empty-participants`を登録します。現在の環境には2026年11月14日に両方を適用し、ジョブが有効なことを確認しました。スケジュールは`0 * * * *`（毎時0分）、対象は登録から24時間以上で写真が0枚の未提出IDです。Azure App Serviceの稼働状態には依存しません。Supabase DB自体が休止している間は実行されません。

新しいアプリを公開する前に003を適用してください。004を適用すると次回の毎時実行から削除が始まります。003・004の適用操作では、その場で削除関数を呼び出しません。写真を登録する処理が行をロック中なら削除を見送り、写真が確定した後は対象外になります。削除と期限切れ認証情報の保存は同じトランザクションで行います。

ジョブの状態と実行履歴はSupabaseのCron画面、またはSQL Editorで確認できます。

```sql
SELECT jobid, jobname, schedule, active
FROM cron.job WHERE jobname = 'hiragana-purge-empty-participants';

SELECT status, start_time, end_time, return_message
FROM cron.job_run_details
WHERE jobid IN (SELECT jobid FROM cron.job WHERE jobname = 'hiragana-purge-empty-participants')
ORDER BY start_time DESC LIMIT 10;
```

自動削除を停止する場合は、対象ジョブだけを無効にします。

```sql
SELECT cron.alter_job(jobid, active := false)
FROM cron.job WHERE jobname = 'hiragana-purge-empty-participants';
```

削除処理を検証する`python scripts/verify_expiry_postgres.py`は、接続先DBに検証専用の一時スキーマを作り、その中で24時間の境界・写真の保護・同時登録・トランザクションのロールバックを検証してスキーマを削除します。本番の参加者・写真・Cronジョブは変更しません。

## Dockerを使う場合

`Dockerfile`も同梱しています。Python 3.12とOpenCVの実行環境を固定したい場合に利用できます。公開済みイメージやContainer Registryは作成していません。標準の公開手順は上記のPythonランタイムへのZIPデプロイです。

```powershell
docker build -t handwriting-collector .
```

将来コンテナ方式へ切り替えるときも、同じ環境変数・Supabase接続を使い、App Serviceのコンテナの待受ポートを8000に設定します。イメージへ秘密値や研究データを入れません。
