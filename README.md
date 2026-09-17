# crypto-news-bot

ARB・ETH・SOL・WLD のニュースと関連データを収集し、銘柄ごとに別々のDiscordチャンネルへ
Webhookで通知するツールです。有料API・LLM APIは一切使わず、判定はすべてキーワードと
ルールで行います。

**GitHub Actions（クラウド）で自動実行されるため、PCの電源は関係ありません。**

投稿は3種類あります。

| 種類 | タイミング | 内容 |
|---|---|---|
| 日次まとめ | 毎朝7:00（日本時間） | 前日のニュースと価格・オンチェーンデータを1チャンネル1投稿 |
| 速報 | 30分ごと | 重大ニュースや急な値動きがあるときだけ `@here` 付きで即時投稿 |
| 週次振り返り | 毎週月曜7:00 | 先週大きく動いた日と、その直前に出たニュース |

リポジトリ: https://github.com/sajicosaji/crypto-news-bot

---

## 1. ニュースの読みやすさについて

リンクを踏まなくても内容が分かるように、次の2つを各記事に付けています。

- **本文抜粋**: 媒体が配信している記事の書き出しをそのまま表示します
- **読む価値アドバイス**: 重大ワード・報道媒体数・同時に起きている値動きから
  「高 / 中 / 低」と理由を判定して表示します

表示例:

```
📉 Arbitrum bridge exploited for $24M
　ブリッジから2,400万ドル相当が流出。攻撃者は資金を分散送金している。
　👉 読む価値: 高 — 重大ワード「exploit」を含む、3媒体が報じている、
　　同時に24hで-11.2%の値動き。まず最初に目を通すのがおすすめ
```

### 値動きの背景ニュース

価格が動いたとき、その原因になりそうなニュースを出します。ただし
**BTCにつられただけの動きは、個別ニュースのせいにしません**。

BTCの24h変化率との差（超過変動）を見て、差が小さければ「相場全体につられた動き」と
表示するだけにし、差が大きいときだけ「銘柄固有の動き」として背景ニュースを探します。

```
値動きの背景
BTC +0.9% に対してこの銘柄は +11.2%。差し引き +10.3% の上振れで、
銘柄固有の動きが大きいです。

上昇の背景になりそうなニュース（原因と断定するものではありません）:
🚀 Arbitrum DAOが手数料収入によるARB買い戻しを可決
```

拾う条件は次の通りです。値動きの向きと合わない記事（上昇時の悪材料など）は出しません。

- 値動きの向きと記事の良い/悪いが一致している
- または、多くの媒体が報じた話題（見出しのキーワードだけでは強弱を拾いきれないため、
  5媒体以上が報じていれば中立判定でも候補にする）
- 重大ワードを含む、直近に出た記事ほど上位

該当が無ければ「見つかりませんでした」と正直に表示します。無理に理由を作りません。
しきい値は `move_context.coin_specific_pct`（既定3%）で変えられます。

### できないこと（正直な制約）

- **英語記事を日本語に翻訳することはできません。** 翻訳や本格的な要約にはLLMが必要で、
  「LLM APIを使わない」という方針のため実装していません。日本語メディア
  （CoinDesk Japan・あたらしい経済・BeInCrypto Japan）の記事は日本語で読めますが、
  英語メディアの抜粋は英語のままです。
- 抜粋は媒体が配信している長さ（通常1〜3文）が上限です。それ以上長くはできません。
- もし日本語の要約が欲しくなった場合は、LLM APIを使う方式に変更できます
  （Claude Haikuで月200〜500円程度の見込み）。必要になったら言ってください。

---

## 2. どうやって動いているか（自動実行の仕組み）

GitHubのスケジュール実行（cron）は**このアカウントでは発火しないことが多い**ため
（競馬BOT・蒲郡BOTでも同じ問題が起きており、今回も23:30 UTCの実行が発火しないことを実測）、
次の仕組みにしています。

```
Claudeのクラウドルーティン（1日4回: 7:00 / 13:00 / 19:00 / 1:00 JST）
        ↓ ワークフローを起動
GitHub Actions の「自走ジョブ」（1回あたり約5.5時間動き続ける）
        ↓ ジョブの中で自分の時計を見て判断
  ・30分ごと      → 速報チェック
  ・7:00 JST      → 日次まとめ（その日まだ投稿していなければ）
  ・月曜 7:00 JST → 週次振り返り（その週まだ投稿していなければ）
```

1日4回の起動で24時間をカバーします。GitHub側のスケジュール実行も保険として残して
ありますが、発火しなくても問題ありません。

日次まとめと週次振り返りは「その日/その週にまだ投稿していない場合だけ」投稿する作りなので、
ジョブが重なっても二重投稿は起きません。逆に朝の実行が失敗しても、13時までに復旧すれば
遅れて投稿されます（`schedule.digest_window_hours` で調整可）。

外部トリガー: `trig_01JDfJhtQ9EWXsBBm8KfsBAS`
（claude.ai の設定画面、またはこのチャットで「トリガーを止めて」と言えば停止できます）

---

## 3. 普段の運用

**基本的に何もする必要はありません。**

### 動いているか確認したいとき

https://github.com/sajicosaji/crypto-news-bot/actions

緑のチェックが並んでいれば正常です。赤い×があれば、それをクリックするとエラー内容が見られます。

### 今すぐ手動で動かしたいとき

上記のActionsページ →「crypto-news-bot」→「Run workflow」→ モードを選んで実行。
コマンドからは次のようにします。

```
gh workflow run crypto-news-bot.yml --repo sajicosaji/crypto-news-bot -f mode=digest
```

`mode` は `serve`（自走）/ `alert` / `digest` / `weekly` / `test-webhooks` から選べます。
手動で1回だけ試したいときは `alert` や `digest` を選んでください。

### 設定を変えたいとき

`config.yaml` を編集して、GitHubにpushすれば次回の実行から反映されます。

```
git add config.yaml
git commit -m "設定を変更"
git push
```

---

## 4. Discord Webhookの設定

Webhook URLは**GitHubのSecretsに登録済み**です（コードには入っていません）。
チャンネルを作り直すなどでURLが変わった場合は、次のように更新します。

```
gh secret set WEBHOOK_ARB --repo sajicosaji/crypto-news-bot
```

（実行するとURLの入力を求められます。`WEBHOOK_ETH` / `WEBHOOK_SOL` / `WEBHOOK_WLD` も同様）

Webhookの作り方: Discordの対象チャンネル → 歯車（チャンネルの編集）→「連携サービス」
→「ウェブフック」→「新しいウェブフック」→「ウェブフックURLをコピー」

登録していない銘柄はスキップされ、ログに警告が出るだけでエラーにはなりません。

---

## 5. config.yaml の編集方法

### 銘柄の追加・変更

`coins:` に銘柄を追加し、GitHub Secretsに対応する `WEBHOOK_XXX` を追加すれば通知対象が増えます。
`coingecko_id` は [CoinGecko](https://www.coingecko.com/) の該当コインのページURLで実際に
確認してください（開発時、`worldcoin` は別の古いトークンを指しており、正しいWLDのIDは
`worldcoin-wld` でした。似た名前の別トークンに注意）。

### ニュースの取得元

`sources.feeds` に「名前」と「RSSのURL」を並べるだけで取得元を増やせます。
日本語メディアを増やすと、日本語で読める記事が増えます。

```yaml
sources:
  feeds:
    - name: "媒体名"
      url: "https://example.com/feed"
```

**Google Newsについて**: `google_news_en` / `google_news_ja` は既定で `false` です。
Google NewsはGitHub Actionsのようなデータセンター経由のアクセスを503で拒否するため、
クラウドでは取得できません（実際に確認済み）。またGoogle Newsのリンクは記事本文に
到達できないため、本文抜粋も取れません。ローカルPCで動かす場合のみ `true` が使えます。

### キーワード（判定ロジック）

`keywords.good` / `keywords.bad` に良い/悪いキーワードを、`critical_keywords` に
速報の候補になる重大ワードを設定します。絵文字は `emojis` で変更できます。

絵文字にはサーバー独自の絵文字（`<:name:id>`、アニメーションは `<a:name:id>`）も
指定できます。Discordの仕様上、埋め込みのタイトルでは独自絵文字が描画されないため、
その場合は自動的に本文の先頭に移動して表示します。

否定表現の誤判定を抑える設定:

- `idiom_neutralizers`: 「falls short」のような紛らわしい言い回しを判定前に取り除く
- `negation_words_en` / `negation_suffixes_ja`: 「no hack」「下落せず」のように、
  キーワードの前後に否定語があればそのキーワードを判定から除外する

### 読む価値アドバイス

`reading_advice.price_reaction_pct`: この%以上の値動きが同時に起きていれば優先度を上げます。
`reading_advice.enabled: false` にすればアドバイス行を消せます。

### 抜粋の長さ・件数

- `excerpt.max_length`: 1記事あたりの抜粋の最大文字数（既定300）
- `digest.detailed_items`: 抜粋＋アドバイス付きで詳しく載せる件数（既定5）
- `digest.max_news_items`: 日次まとめに載せる記事数の上限（既定10）

Discordの埋め込みには文字数の上限があるため、長くする場合は件数を減らしてください
（上限を超える分は自動的に打ち切られます）。

### 同じニュースのまとめ方

媒体ごとに見出しが書き換えられるため、完全一致だけでは同じ話題が何件も並びます。
`dedup.min_shared_terms`（既定3）は、見出しの中で手がかりになる語がいくつ一致したら
同じ話題とみなすかの設定です。

- 数を**増やす**とまとめる条件が厳しくなり、同じ話題が複数並びやすくなります
- 数を**減らすと**まとめすぎて、別のニュースが隠れてしまうことがあります（2は実測で
  まとめすぎだったため3にしています）

### しきい値・速報の条件

`alert_thresholds` にまとめてあります。

- `arb_wld_price_change_pct` / `eth_sol_price_change_pct`: 24時間の値動きで速報を出す%
- `eth_sol_critical_media_count` / `eth_sol_critical_window_hours`: ETH・SOLで重大ワード
  記事が何媒体・何時間以内に報じられたら速報にするか
- `price_alert_cooldown_hours`: 価格・データ系の速報を出したあとのクールダウン時間
- `max_alerts_per_channel_per_run`: 1回の実行でチャンネルごとに出す速報の上限
- `news_alert_max_age_hours`: 公開からこの時間を超えた記事は速報にしない

銘柄ごとの重点ワードは `arb.priority_good_keywords` / `arb.priority_bad_keywords`、
`wld.priority_bad_keywords`、`wld.morpho.risk_keywords`、`sol.dex_names` で編集できます。

### 価格の節目

`arb.price_levels`・`wld.price_levels` に配列で設定します（例: `[0.14, 0.20]`）。
上抜け・下抜けで速報を出し、`price_alert_cooldown_hours` の間は同じ節目での連続投稿を防ぎます。

### アンロック予定・マクロ予定

日付は**必ずダブルクォートで囲んだ `YYYY-MM-DD` 形式**で手入力します。
アンロックは3日前と前日に、マクロ予定は当日と翌日に、日次まとめへ自動表示されます。

```yaml
arb:
  unlocks:
    - date: "2026-10-16"
      amount: "92,600,000 ARB"
      description: "投資家・チーム向け月次アンロック"

macro_events:
  - date: "2026-10-29"
    description: "FOMC（政策金利発表）"
```

### 配信時刻の変更

`schedule.digest_hour_jst`（既定7）で日次まとめの時刻を変えられます。週次振り返りも
同じ時刻になります。**時刻を変えるときは、外部トリガーの起動時刻も合わせて動かす必要が
あります**（配信時刻に自走ジョブが動いていないと、次の起動まで投稿が遅れるため）。
トリガー側の変更はこのチャットで「◯時に変えて」と言えば対応します。

### メンション設定

`mention` を `@here` / `@everyone` / `<@&ロールID>` / 空文字（メンションなし）から選べます。

---

## 6. 確認できなかった外部API（無効化している機能）

実装前に実際にAPIへリクエストして確認しましたが、以下は公開APIが見つからず無効化しています。

- **World Networkの利用状況（World ID認証済み人数など）**
  `worldcoin.org` / `world.org` 配下を調べましたが公開APIを確認できませんでした。
  `src/onchain.py` の `fetch_world_network_stats()` は常に `None` を返します。

確認済みで動作しているAPI:

- CoinGecko 無料API（WLD = `worldcoin-wld`、USD1 = `usd1-wlfi`）
- DefiLlama 手数料API（Robinhood Chain・Arbitrum Oneの日次収益）
- DefiLlama DEX出来高API（Robinhood ChainのDEX出来高）
- DefiLlama トレジャリーAPI（`treasury/arbitrum-dao`）
  ※ `chainTvls` の "Arbitrum" "Arbitrum Nova" "Ethereum" "OwnTokens" を合算した**概算値**です
- Arbitrum DAO公式フォーラムRSS（`forum.arbitrum.foundation/latest.rss`）
- Morpho GraphQL API（WLD関連マーケットの利用率・APY・預け入れ額）
- 各媒体の直接RSS（config.yaml の `sources.feeds` 参照）

---

## 7. 仕組み上の注意点

- DefiLlamaの日次データはUTC基準で区切られているため、「前日」は日本時間の暦日と
  厳密には一致しません（最大で数時間のズレ）。無料APIの制約による近似です。
- 初回の `--alert` 実行時は保存のみ行い、速報は出しません（何が新着かまだ判定できないため）。
- 週次振り返りは、30分ごとの実行で記録した価格をもとに作ります。運用開始直後は
  1週間分のデータが無いため変化率が正しく出ません（データ不足の場合はその旨を表示します）。
- 記事・価格・速報履歴は30日で自動削除されます（`data_retention_days` で変更可）。
- データベースはGitHub Actionsのキャッシュで実行間を引き継いでいます。キャッシュが
  消えた場合は初回扱いになり、速報を出さずに保存からやり直すため誤爆はしません。
- 速報は「自走ジョブ」が30分ごとにチェックします。ジョブの切り替わり（1日4回）の
  タイミングでは数分の空白が生じます。
- クラウドルーティンが停止すると自動実行も止まります。Actionsページに実行が
  並んでいない場合はトリガーを確認してください。

---

## 8. ローカルPCで動かす場合（任意）

クラウドで動いているので通常は不要ですが、手元で試したいときは次の通りです。

```
セットアップ.bat をダブルクリック（ライブラリのインストール）
.env.example を .env にコピーしてWebhook URLを記入
python main.py --digest --dry-run     # 投稿せず内容だけ確認
python main.py --alert --dry-run
python main.py --weekly --dry-run
テスト投稿.bat をダブルクリック（各チャンネルへテスト投稿）
```

`--dry-run` を付けている間はDiscordには一切投稿されません。

---

## 9. テストの実行

```
pytest tests/ -v
```

判定ロジック（否定形を含む）、速報条件の銘柄ごとの違い、価格節目の連続投稿防止、
記事の重複判定と言い換え記事のまとめ、銘柄への振り分け、BTCとの比較、
Robinhood Chain収益変化の検知、抜粋の整形、読む価値アドバイスを自動テストしています。
外部APIへの実際のアクセスは行いません。
