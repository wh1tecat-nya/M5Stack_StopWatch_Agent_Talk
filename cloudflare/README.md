# すみとStopWatchの接続テスト

固定文を既存のすみの会話に通知し、すみが返答ツールで返した短文を端末用のHTTPS APIから受け取る。最初の疎通ではPCのターミナルをテスト端末として使う。中継はCloudflare Workers上で動く。音声APIキーは不要。

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
