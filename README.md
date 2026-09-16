# crypto-news-bot

ARB・ETH・SOL・WLD のニュースと関連データを収集し、銘柄ごとに別々のDiscordチャンネルへ
Webhookで通知するツールです。有料API・LLM APIは一切使わず、判定はすべてキーワードと
ルールで行います。

投稿は3種類あります。

- **日次まとめ**（`--digest`）: 毎朝8:00に前日分のニュースとデータを1チャンネル1投稿でまとめる
- **速報**（`--alert`）: 30分ごとにチェックし、重大なニュースや急な値動きがあるときだけ `@here` 付きで即時投稿する
- **週次振り返り**（`--weekly`）: 毎週月曜8:00に、先週大きく動いた日とその直前のニュースをまとめる

---

## 1. セットアップ手順

### 1-1. Python のインストール確認

Windows に Python 3 がインストールされていることを確認してください（コマンドプロンプトで
`python --version` を実行して `Python 3.x.x` と表示されればOK）。

### 1-2. 必要なライブラリのインストール

**`セットアップ.bat` をダブルクリックするだけ**でインストールが終わります。

コマンドで行いたい場合は、このフォルダ（`crypto-news-bot`）でコマンドプロンプトまたは
PowerShellを開き、次を実行します。

```
pip install -r requirements.txt
```

### 1-3. .env ファイルの作成

`.env.example` をコピーして `.env` という名前のファイルを作り、Webhook URLを入力します
（作り方は次の章）。

```
copy .env.example .env
```

### 1-4. 動作確認（投稿せずに内容だけ確認する）

```
python main.py --digest --dry-run
python main.py --alert --dry-run
python main.py --weekly --dry-run
```

コンソールに投稿予定の内容が表示されれば正常です。`--dry-run` を付けている間は
Discordには一切投稿されません。

### 1-5. 各チャンネルへのテスト投稿

**`テスト投稿.bat` をダブルクリック**するか、次のコマンドを実行します。

```
python main.py --test-webhooks
```

`.env` に設定した各チャンネルに実際に1件ずつテストメッセージが届きます。

---

## 2. Discord Webhookの作り方

1. Discordサーバーの対象チャンネル（例: #arb-news）を開く
2. チャンネル名の右にある歯車アイコン（チャンネルの編集）→「連携サービス」→「ウェブフック」
3. 「新しいウェブフック」を作成し、名前を分かりやすく変更（例: ARB News）
4. 「ウェブフックURLをコピー」でURLを取得する
5. これを ARB・ETH・SOL・WLD の4チャンネル分（必要な銘柄だけでよい）繰り返す

---

## 3. .env の書き方

`.env` に、銘柄ごとのWebhook URLを1行ずつ書きます。

```
WEBHOOK_ARB=https://discord.com/api/webhooks/xxxxxxxx/xxxxxxxx
WEBHOOK_ETH=https://discord.com/api/webhooks/xxxxxxxx/xxxxxxxx
WEBHOOK_SOL=https://discord.com/api/webhooks/xxxxxxxx/xxxxxxxx
WEBHOOK_WLD=https://discord.com/api/webhooks/xxxxxxxx/xxxxxxxx
```

- 通知が不要な銘柄は空欄のままでOKです（起動時にログへ警告が出て、その銘柄はスキップされます）
- 値を入れ忘れた場合もエラーで止まらず、他の銘柄の処理は続行されます

---

## 4. タスクスケジューラへの登録

Windowsの「タスクスケジューラ」を開き、「基本タスクの作成」から以下を3つ登録します。
「プログラムの開始」の画面で入力する内容は次の通りです（`<フォルダのパス>` は実際にこの
フォルダを置いた場所に置き換えてください。例: `C:\Users\あなたの名前\OneDrive\Desktop\crypto-news-bot`）。

いずれも「開始（作業）フォルダ」欄に `<フォルダのパス>` を指定してください（相対パスで
config.yaml や .env を読み込むため必須です）。

### 日次まとめ（毎日 8:00）

- プログラム/スクリプト: `python`
- 引数の追加: `main.py --digest`
- 開始（作業）フォルダ: `<フォルダのパス>`
- トリガー: 毎日 8:00

### 速報（30分ごと）

- プログラム/スクリプト: `python`
- 引数の追加: `main.py --alert`
- 開始（作業）フォルダ: `<フォルダのパス>`
- トリガー: 毎日、開始時刻 0:00、「タスクの繰り返し間隔」を30分・継続時間を「1日」に設定

### 週次振り返り（毎週月曜 8:00）

- プログラム/スクリプト: `python`
- 引数の追加: `main.py --weekly`
- 開始（作業）フォルダ: `<フォルダのパス>`
- トリガー: 毎週、月曜日、8:00

コマンドラインから `schtasks` で登録する場合は次のようになります（管理者権限は不要です。
`python` がフルパスで見つからない環境では `python` の部分を `python.exe` のフルパスに
置き換えてください）。

```
schtasks /create /tn "crypto-news-bot digest" /tr "python \"<フォルダのパス>\main.py\" --digest" /sc daily /st 08:00
schtasks /create /tn "crypto-news-bot alert" /tr "python \"<フォルダのパス>\main.py\" --alert" /sc minute /mo 30
schtasks /create /tn "crypto-news-bot weekly" /tr "python \"<フォルダのパス>\main.py\" --weekly" /sc weekly /d MON /st 08:00
```

（`schtasks` はタスク実行時の作業フォルダを明示的に指定できないため、`main.py` は自分の
置かれている場所を基準にファイルを読み書きするように作ってあります。フルパスで
`main.py` を指定していれば作業フォルダに依存せず正しく動作します。）

---

## 5. config.yaml の編集方法

`config.yaml` に、通知の中身に関わる設定をすべてまとめています。編集後はPythonの
再起動（次回のタスク実行）で反映されます。

### 銘柄の追加・変更

`coins:` の下に銘柄を追加し、`.env` に対応する `WEBHOOK_XXX` を追加するだけで通知対象を
増やせます。`coingecko_id` は [CoinGecko](https://www.coingecko.com/) でそのコインの
ページURLの末尾にあるIDを実際に確認してから設定してください（本ツールの開発時、
`worldcoin` というIDは別の古いトークンを指しており、正しいWLDのIDは `worldcoin-wld` でした。
似た名前の別トークンに注意してください）。

### キーワード（判定ロジック）

`keywords.good` / `keywords.bad` に良い/悪いキーワードを追加・削除できます。
`critical_keywords` は速報の候補になる重大ワードです。絵文字は `emojis` で変更できます。

絵文字にはサーバー独自の絵文字（`<:name:id>`、アニメーション絵文字は `<a:name:id>`）も
指定できます。ただしDiscordの仕様上、埋め込み（embed）のタイトル部分では独自絵文字が
描画されず文字列のまま表示されてしまうため、独自絵文字を設定した場合は自動的に本文の
先頭へ移動して表示します（通常のUnicode絵文字を設定した場合はタイトルに表示されます）。

否定表現の誤判定を抑えるための設定:

- `idiom_neutralizers`: 「falls short」のように、それ自体では判定に使わない言い回しを
  スコア計算前に取り除きます
- `negation_words_en` / `negation_suffixes_ja`: 「no hack」「下落せず」のように、
  キーワードの前後に否定語があれば、そのキーワードを判定から除外します

### しきい値・速報の条件

`alert_thresholds` にまとめてあります。

- `arb_wld_price_change_pct` / `eth_sol_price_change_pct`: 24時間の値動きで速報を出す%
- `eth_sol_critical_media_count` / `eth_sol_critical_window_hours`: ETH・SOLで重大ワード
  記事が何媒体・何時間以内で報じられたら速報にするか
- `price_alert_cooldown_hours`: 価格・データ系の速報を出したあと、同じ条件で次に出すまでの
  クールダウン時間
- `max_alerts_per_channel_per_run`: 1回の実行でチャンネルごとに出す速報の上限
- `news_alert_max_age_hours`: 公開からこの時間を超えた記事は速報にしない
  （Google Newsの検索結果には数ヶ月前の記事も混ざることがあるため、今日はじめてDBに
  保存された古い記事を「速報」として誤って流さないための安全装置です）

銘柄ごとの重点ワードは `arb.priority_good_keywords` / `arb.priority_bad_keywords`、
`wld.priority_bad_keywords`、`wld.morpho.risk_keywords`、`sol.dex_names` で編集できます。

### 価格の節目

`arb.price_levels`・`wld.price_levels` に配列で設定します（例: `[0.14, 0.20]`）。
上抜け・下抜けを検知したときに速報を出し、`price_alert_cooldown_hours` の間は同じ節目での
連続投稿を防ぎます。

### アンロック予定

`arb.unlocks` / `wld.unlocks` に、日付を **必ずダブルクォートで囲んだ `YYYY-MM-DD` 形式**
で手入力します。日次まとめで、その3日前と前日に自動的に表示されます。

```yaml
arb:
  unlocks:
    - date: "2026-10-16"
      amount: "92,600,000 ARB"
      description: "投資家・チーム向け月次アンロック"
```

### マクロ経済イベント

`macro_events` に同様の形式で日付とイベント名を追加します。日次まとめで当日・翌日分が
表示されます。

```yaml
macro_events:
  - date: "2026-10-29"
    description: "FOMC（政策金利発表）"
```

### メンション設定

`mention` を `@here` / `@everyone` / `<@&ロールID>` / 空文字（メンションなし）から選べます。

---

## 6. 確認できなかった外部API（無効化している機能）

実装前に実際にAPIへリクエストして仕様を確認しましたが、以下は公開APIが見つからず
無効化した状態にしてあります。将来的に公開APIが見つかった場合は、対応するコードを
有効化してください。

- **World Networkの利用状況（World ID認証済み人数など）**
  `worldcoin.org` / `world.org` 配下でそれらしいAPIエンドポイントを探しましたが、
  公開APIを確認できませんでした。`src/onchain.py` の `fetch_world_network_stats()` は
  常に `None` を返す無効化状態です。日次まとめには表示されません。

なお、以下のAPIは実装前に実際にリクエストして仕様を確認済みで、有効に動作します。

- CoinGecko 無料API（`worldcoin-wld` がWLDの正しいID、`usd1-wlfi` がUSD1のID）
- DefiLlama 手数料API（`/overview/fees/{chain}`）… Robinhood Chain・Arbitrum Oneの日次収益
- DefiLlama DEX出来高API（`/overview/dexs/{chain}`）… Robinhood ChainのDEX出来高
- DefiLlama トレジャリーAPI（`/treasury/arbitrum-dao`）… Arbitrum DAOトレジャリー残高
  （`chainTvls` の "Arbitrum" "Arbitrum Nova" "Ethereum" "OwnTokens" を合算した概算値です。
  DefiLlamaが単一の合計値を明示的に提供していないための近似値である点に注意してください）
- Arbitrum DAO公式フォーラムのRSS（`forum.arbitrum.foundation/latest.rss`）
- Morpho GraphQL API（`blue-api.morpho.org/graphql`）… WLD関連マーケットの利用率・APY・預け入れ額
- Google News RSS（英語版・日本語版）、CoinDesk・Cointelegraph・The BlockのRSS

---

## 7. その他の注意点

- DefiLlamaの日次データはUTC基準で区切られているため、「前日」は日本時間の暦日と
  厳密には一致しません（最大で数時間のズレが生じます）。無料APIの制約による近似です。
- 初回の `--alert` 実行時は、記事・価格・データの保存のみ行い、速報は出しません
  （何が「新着」かをまだ判定できないためです）。
- 週次振り返りは、`--alert` が30分ごとに記録した価格スナップショットをもとに
  作成します。運用を開始した直後の1回目・2回目の週次振り返りは、まだ1週間分の
  価格データが蓄積されていないため、変化率が正しく出ないことがあります
  （データ不足の場合はその旨を表示します）。運用を続けるほど内容が充実します。
- ログは `logs/bot.log` に出力され、2MBを超えると自動でローテーションします
  （`logs/bot.log.1` などに退避され、最大5世代保持）。
- データは `data/bot.db`（SQLite）に保存されます。バックアップしたい場合はこのファイルを
  コピーしてください。

---

## 8. テストの実行

```
pip install pytest
pytest tests/ -v
```

判定ロジック（否定形を含む）、速報条件の銘柄ごとの違い、価格節目の連続投稿防止、
記事の重複判定、銘柄への振り分け、BTCとの比較、Robinhood Chain収益変化の検知などを
自動テストしています。外部APIへの実際のアクセスは行いません（モック不要な純粋関数、
またはインメモリSQLiteのみを使ってテストしています）。
