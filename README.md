# 研究用 筆跡収集アプリ

日本語・スマホ向けのFastAPIアプリです。参加者は説明に同意して参加者IDを受け取り、記入用紙PDFをダウンロード・印刷し、記入した用紙の写真を提出します。研究者はPDF・元写真・補正PNG・対応表CSVを選んでZIPで回収し、配布用紙の差し替えも研究者画面から行えます。

本番構成は **Azure App Service（Linux）＋Supabase PostgreSQL＋非公開Storage** です。Azureリソースはまだ作成していません。

## ローカルで開く

Python 3.12を使用します。プロジェクトフォルダのPowerShellで実行してください。`python`がWindowsストアのエイリアスの場合は、最初の行を`py -3.12 -m venv .venv`へ置き換えます。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts/bootstrap.py
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

- 参加者画面：<http://127.0.0.1:8000>
- 研究者画面：<http://127.0.0.1:8000/researcher>
- ローカル用研究者パスワード：`data/local-researcher-password.txt`

初回の`bootstrap.py`はランダムな秘密値を含む`.env`を作成します。既存`.env`は変更しません。ローカルモードはSQLiteと`data/`内の非公開ファイル保存を使うため、外部アカウントなしで操作できます。`.env`、DB、提出画像はGitやデプロイZIPへ含めません。

同じWi-Fiのスマホから確認する場合は、PCの`ipconfig`でIPv4アドレスを調べます。例えば`192.168.1.20`なら`.env`を`APP_BASE_URL=http://192.168.1.20:8000`、`ALLOWED_HOSTS=localhost,127.0.0.1,testserver,192.168.1.20`へ変更し、`--host 0.0.0.0`で起動します。スマホでは`http://192.168.1.20:8000`を開きます。Windowsが接続確認を表示した場合は、利用するプライベートネットワークの接続を許可してください。

## 参加者の操作

1. 研究説明を読み、同意して登録します。氏名・メールアドレスの入力はありません。参加者IDと専用の再開リンクが表示されるので、リンクを保存します。
2. 「記入用紙をダウンロード（PDF）」から用紙を保存します。提供された`hiragana_collection_a4.pdf`はA4・8ページです。A4・実際のサイズ（100%）・片面で印刷し、氏名などの個人情報を書かずに記入します。
3. 記入した用紙を1枚ずつ撮影するか、撮影済み画像を選択します。用紙の端・四隅の黒丸・QRが写真内へ収まるようにします。順番は任意です。
4. 認識ページ・補正プレビュー・登録状況を確認します。同じQRを登録すると差し替えの確認が出ます。読取・補正・新しい画像の保存がすべて成功してから置き換えます。
5. 登録時の用紙版で必要なページがそろったら提出します。設定された順番で`P001.pdf`のようなPDFを生成します。提出後は写真を変更できません。

再開リンクには本人のデータへアクセスする鍵が含まれます。参加者同士で共有しないでください。鍵はURLの`#`以降に置き、APIではBearerヘッダーで送るため通常のURLアクセスログへ入りません。参加者IDだけではアクセスできません。

同意日時・同意本文・同意文の版をDBへ保存します。連打や通信再試行は同じ登録キーを使い、DBの一意制約で二重登録を防ぎます。番号はDBが発行するため同時登録でも重複しません。欠番は再利用しません。

## 研究者の回収操作

研究者画面へログインすると、参加者ID・登録日時・提出状況・登録ページ数を確認できます。対象者を「提出済み全員」または個別選択、種類を「PDF・元写真・補正済みPNG・対応表CSV」から複数選び、ZIPで取得します。参加者IDのフォルダに分類し、選択した種類だけを収録します。未提出者の画像も個別選択で回収できますが、PDFは提出後に取得できます。

対応表CSVには参加者ID・QR・ページ番号・元写真名・補正PNG名・提出PDF名を含めます。研究者向け一覧・PDF確認・ダウンロード・用紙変更のAPIでもサーバーが認証を確認します。

## 研究者画面で配布用紙を差し替える

1. `/researcher`へログインし、「配布用紙」の欄を開きます。現在のPDF名、ページ数、ページ順とQR一覧を確認できます。
2. 新しいPDFを選び、「PDFを確認」を押します。サーバーが全ページのA4寸法・QR・四隅の黒丸を確認します。暗号化、不正なPDF、読めないQR、重複QR、対応できない用紙は理由を表示して拒否し、現在の配布用紙は維持します。
3. 表示されたページ数・QR一覧と確認用PDFを原本と照合します。
4. 「この用紙に差し替える」を押して確定します。確定前は参加者への配布に反映しません。

**差し替え後に新規登録する参加者から、新しい用紙と必要ページ一覧を適用します。登録済みの参加者は、登録時のPDF・QR・ページ順・必要一覧を継続します。** 記入途中のデータや過去の提出が新しい用紙へ混ざることはありません。既存参加者にも新しい用紙を使ってもらいたい場合は、研究者と参加者で切り替える時点を決め、別の参加登録として扱ってください。

アップロードしたPDFと設定は版ごとにDBと非公開Storageへ保存します。サーバー再起動やAzure再デプロイ後も、確定した配布版を維持します。複数の研究者画面が同時に操作した場合は、後から古い画面で上書きしないよう競合を検出します。通信が途切れた場合は現在の配布版を再確認し、同じ確定操作を安全に再試行できます。

この操作に環境変数変更・コード編集・再デプロイは不要です。同意文を変更する操作は含めていません。

## 原本と初期設定

提供された原本は`resources/worksheets/hiragana_collection_a4.pdf`にそのまま保存しています。全8ページのQRは原本から実際に読み取り、順番は`HIRA1-01`〜`HIRA1-08`です。A4上の黒丸中心は左上からmm単位で`(10,10)`・`(200,10)`・`(200,287)`・`(10,287)`です。ページ順・QR・補正座標を`config/pages.study.json`へ記録しています。

`PAGE_CONFIG=config/pages.study.json`は、新しいDBを初期化するときの設定です。初期化後の配布版はDBで管理するため、ファイルを書き換えるだけでは配布版は切り替わりません。通常の変更は研究者画面から行ってください。

同意文は提供いただいた本文で、版は`2026-11-10-v1`です。`config/pages.test.json`と`resources/worksheets/test-worksheet.pdf`は自動テスト用の3ページ構成で、`TEST-02`・`TEST-03`は開発専用の架空コードです。実際の配布原本の続きや総ページ数を推測したものではありません。

初期設定の`worksheet.path`は`resources/worksheets/`内のPDFを指定します。配布PDFは静的ファイルとして公開せず、参加者や研究者の認証を通したAPIで取得します。本番は正式原本・校正済みの設定・確定済み同意文が必要で、テスト用紙設定のままでは起動しません。

画像処理はQRと四隅の黒丸から用紙外枠を推定し、写真内に収まるかを確認してA4へ射影補正します。向きをそろえ、グレースケールと滑らかな背景補正を使い、二値化した画像を保存しません。読取不能、対象外QR、四隅不足、端切れ、強い影、ぼけは理由を表示します。JPEG・PNG・WebPを使用できます。HEICはJPEGへ変換して選択してください。

## 研究者パスワード

```powershell
.\.venv\Scripts\python.exe scripts/set_password.py
```

12文字以上のパスワードを2回入力します。出力された`RESEARCHER_PASSWORD_HASH='...'`を`.env`へ設定して再起動してください。Azureには右辺の外側の引用符を除いたハッシュを設定します。平文は保存せず、ソルト付きPBKDF2ハッシュをサーバーで照合します。

研究者セッションは8時間です。`SESSION_SECRET`を変更すると既存の研究者セッションを無効にできます。参加者リンクの再試行用の`PARTICIPANT_TOKEN_SECRET`は別の値を使い、収集期間中に変更しません。両方とも32文字以上のランダム値を設定し、秘密値はブラウザやGitへ渡しません。

## Supabaseへ接続する

1. [Supabase Dashboard](https://supabase.com/dashboard)で研究用プロジェクトを作成し、DBパスワードを保管します。
2. **SQL Editor → New query**で`migrations/001_initial.sql`、続けて`migrations/002_worksheet_revisions.sql`を実行します。既に初期版の環境がある場合は、未適用の002を追加実行します。これが用紙の版管理と参加者への紐付けを追加します。
3. **Storage**で`handwriting-private`を開き、**Public bucketがオフ**か確認します。別名を使う場合は非公開バケットを作成し、環境変数も変更します。サイズ制限は元写真の上限25 MBと全ページPDFの大きさを許容する値にし、公開・匿名アクセスポリシーは追加しません。
4. **Connect → Session pooler**の接続URLをコピーします。IPv4で利用できる5432番ポートを使い、`postgresql://`を`postgresql+psycopg://`へ変更し、`?sslmode=require`を付けます。DBパスワードの記号はURLエンコードしてください。
5. **Project Settings → API Keys**のサーバー用Secret key（`sb_secret_...`）を取得し、`SUPABASE_SECRET_KEY`へ設定します。旧JWT形式の`service_role`を使う場合だけ、代わりに`SUPABASE_SERVICE_ROLE_KEY`へ設定します。
6. `.env`へ`BACKEND_MODE=supabase`、`DATABASE_URL`、`SUPABASE_URL`、秘密鍵、バケット名を設定し、再起動して別の参加者で提出・回収を確認します。

旧バージョンのDBを引き継ぐ更新では、002適用後の初回起動は**以前の`PAGE_CONFIG`を維持**し、既存参加者を以前の用紙版へ紐付けます。その後、研究者画面から正式原本を確認・確定し、`PAGE_CONFIG`を`config/pages.study.json`へ変更します。以前の用紙がテスト設定なら、この切り替えは開発モードで行ってから本番へ移します。本番ではDB内の現在版も検証し、テスト版が配布中のままだと起動しません。今回のローカル環境では、この移行と正式原本の適用を実施済みです。

Supabaseの[接続方法](https://supabase.com/docs/guides/database/connecting-to-postgres)、[非公開バケット](https://supabase.com/docs/guides/storage/buckets/fundamentals)、[APIキー](https://supabase.com/docs/guides/getting-started/api-keys)に基づく構成です。サーバー秘密鍵はサーバー環境変数にだけ設定します。

## 保存・再試行

元写真・補正PNG・提出PDF・配布用紙PDFは非公開Storage、各保存先と状態はDBで管理します。「提出ID＋QR」は一意です。提出時は参加者行をロックし、必要ページ確認・PDF生成・Storage保存が成功してから提出済みにします。失敗時は未提出のままで再試行できます。

差し替え画像と配布用紙は毎回別の保存先へ書き込み、途中失敗で以前のファイルを壊しません。DB更新の応答だけ失われた場合は、保存ファイルを保持し、状態確認と再試行で回復します。配布用紙の旧版は既存参加者のために保持します。

## テスト

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

テストは一時DBと一時保存先を使い、順不同の登録・PDF内の画像順・差し替え・必須不足・QR失敗・同時登録・認証・選択ZIP・保存障害を確認します。配布用紙の確認と確定の分離、旧参加者の版維持、新規参加者への反映、競合拒否、再起動後の保持も検証します。

研究者の用紙差し替えを実ブラウザで再実行するには、サーバーを起動した状態で次を実行します。Windowsではインストール済みGoogle Chromeを使います。

```powershell
npm.cmd install --prefix .browser-tools --no-save --package-lock=false playwright
node tests/browser_revision.cjs
```

`tests/browser_e2e.cjs`と`scripts/verify_local.py`は初期の3ページテスト設定を前提とした検証補助です。実際の8ページ収集環境へテスト用写真を送らないでください。依存関係の直接の版は`requirements.txt`、検証時の全依存関係は`requirements.lock.txt`に記録しています。[検証記録](docs/VERIFICATION.md)も参照してください。

Supabaseの実接続とAzure上の実機確認は資格情報設定後に行います。現在Azureの作成・課金・公開操作は行っていません。

## 公開するときに行う操作

| 開く場所 | 入力・実行するもの |
| --- | --- |
| ローカル研究者画面 | 現在の配布PDFを確認。変更時は新PDF選択→確認→差し替え確定 |
| Supabase SQL Editor | 001・002のマイグレーションを実行し、非公開バケットを確認 |
| ローカルPowerShell | `scripts/set_password.py`で本番パスワードハッシュを生成。`python -c "import secrets; print(secrets.token_urlsafe(48))"`を2回実行して別々の秘密鍵を生成 |
| Azure Portal／PowerShell | [Azure手順](docs/AZURE.md)に従い、Web App作成・環境変数登録・ZIPデプロイ・HTTPS確認 |
| 公開したスマホ画面 | 同意→原本PDF取得→印刷・記入→撮影→提出→研究者回収まで確認 |

提供原本と同意文の設定は済んでいます。公開前に利用者が用意するのはSupabase・Azureの接続情報と本番専用の秘密値です。
