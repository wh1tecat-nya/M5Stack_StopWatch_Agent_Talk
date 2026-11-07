# すみとStopWatchの接続テスト

Cloudflare Workers上で固定文の疎通テストと、StopWatchの録音を文字起こししてすみに届ける機能を提供する。すみが返した短文はHTTPSでStopWatchに戻し、本体で読み上げる。固定文の疎通確認には音声APIキーは不要。

## ローカル検証

```sh
npm ci
npm test
```

OAuthでの接続、署名付き通知、返答の取得を、テスト用の通知先とローカルD1・KVで検証する。

## Cloudflareに配置

このディレクトリで実行する。

```sh
npx wrangler login
cp wrangler.jsonc wrangler.deploy.jsonc
npx wrangler d1 create sumi-stopwatch-text --no-update-config
npx wrangler kv namespace create OAUTH_KV --no-update-config
```

作成結果のD1とKVのIDを `wrangler.deploy.jsonc` の対応する仮IDに入れる。

```sh
npx wrangler d1 migrations apply DB --remote --config wrangler.deploy.jsonc
npx wrangler deploy --config wrangler.deploy.jsonc
npx wrangler secret put RELAY_ORIGIN --config wrangler.deploy.jsonc
npx wrangler secret put OWNER_KEY --config wrangler.deploy.jsonc
npx wrangler secret put DEVICE_TOKEN --config wrangler.deploy.jsonc
```

`RELAY_ORIGIN` はデプロイ時に表示されたHTTPS URLのオリジン。末尾のスラッシュは省く。残りの2つには、それぞれ別の32文字以上のランダムな値を入力する。`OWNER_KEY` はOAuth承認画面用、`DEVICE_TOKEN` は端末用。

## 既存のすみで疎通確認

1. ChatGPTでカスタムMCPアプリのURLにWorkerの `/mcp` を指定し、OAuthで接続する。中継の承認画面に `OWNER_KEY` を入力する。接続に使われるリダイレクトURLが既定の `https://chatgpt.com/connector_platform_oauth_redirect` と異なる場合は、ChatGPTが表示する正確なURLを `CHATGPT_REDIRECT_URI` に設定する。
2. 既存のすみの会話でアプリを選び、端末ID `stopwatch` のイベント `stopwatch.message` を購読するよう伝える。届いたら同じ `request_id` を付けて `submit_stopwatch_reply` で短く返してもらう。購読時に `unapproved_callback_host` が返ったら、実際のChatGPTから提示されたOpenAIのHTTPSホストを確認し、`wrangler secret put CALLBACK_HOST --config wrangler.deploy.jsonc` でそのホスト名を登録してから再試行する。
3. 端末用の `DEVICE_TOKEN` をBearer認証に使い、空の本文で `PUT /device/text-turns/{id}` を送る。`id` は `openssl rand -hex 16` で生成できる。HTTP 503の場合は同じIDで再試行する。
4. 同じ認証とIDで `GET /device/text-turns/{id}` を実行する。`status` が `ready` になり、`answer` にすみの返答が入れば疎通成功。リクエストの有効期限は10分。

[OpenAIのMCP Events仕様](https://developers.openai.com/plugins/build/mcp-events)

## StopWatchからの録音を受け付ける

音声認識に使うOpenAI APIキーをWorkerのシークレットに登録する。

```sh
npx wrangler secret put OPENAI_API_KEY --config wrangler.deploy.jsonc
```

既存のすみが開始したクラウド作業で `stopwatch.message` を購読する。イベントの `text` に対して日本語で短く返答し、同じ `request_id` と返答文を `submit_stopwatch_reply` に渡すよう設定する。返答は1〜2文、80文字程度を目安にする。録音は認証済みの `PUT /device/turns/{id}` で受け付け、16 kHz・モノラル・符号付き16ビットリトルエンディアンPCMに対応する。StopWatchは `GET /device/replies/next` で返答テキストを受け取り、本体で音声化する。再生後の受領通知で処理用の録音とテキストを消去する。

## 会話履歴

`/history` を開き、既存の `OWNER_KEY` でログインする。ログインは7日間有効で、ページからログアウトできる。文字起こしと返答は、`0003_conversation_history.sql` で追加する履歴テーブルに期限なしで保存する。保存は会話の更新と同じトランザクションで行い、再送による重複を防ぐ。処理用データの受領通知・期限切れ削除後も履歴を保持する。保存開始前に消去済みの本文は復元できない。
