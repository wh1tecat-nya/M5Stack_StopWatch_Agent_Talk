# すみ × M5Stack StopWatch

StopWatch C152から、設定した2.4 GHz Wi-Fiを経由して、既存のChatGPT dot「すみ」に音声メッセージを送る試作です。運用時はStopWatchと中継サーバーを使います。

## 操作

- 待機中はSUMIバッジを低輝度で表示します。どちらかのボタンでアバターに切り替わります。
- アバター表示中に左ボタン（A）を押すと録音を開始します。再押下、発話後約0.9秒の無音、または15秒の上限で録音を終了します。発話を検出しなければ送信しません。
- 待機中もWi-Fiを維持し、約3秒ごとに中継に返信を確認します。返信を受け取るとアバターに切り替わり、本体スピーカーで合成音声を再生します。
- 右ボタンでのリアルタイム通話は今後の実装範囲です。

## 開発と接続

1. Arduino IDEで `SumiAvatar/SumiAvatar.ino` を開きます。ESP32 Arduino Core 3.3.7、M5Unified 0.3.0、M5GFX 0.3.0でビルドを確認しました。ボードは `ESP32-S3-Box`、フラッシュは16 MB、PSRAMはOPIです。
2. `SumiAvatar/stopwatch_config.example.h` を同じフォルダの `stopwatch_config.h` にコピーし、SSID、Wi-Fiパスワード、中継のHTTPS URL、端末トークン、TLSのルートCAを設定します。設定ファイルはGitの対象外です。設定なしでも待機画面とアバター表示をビルドできます。
3. 中継をPython 3.11以降で `python3 -m relay.server --database /path/to/sumi.sqlite3` として起動します。環境変数に `SUMI_DEVICE_TOKEN`、`SUMI_MCP_TOKEN`、`OPENAI_API_KEY`、`SUMI_CALLBACK_HOST` を設定します。トークンは別々の32文字以上のランダム値とし、許可するMCPコールバックのホスト名を指定します。中継は録音を `gpt-transcribe` で文字起こしし、返答を `gpt-4o-mini-tts` で音声化します。読み上げの声は `SUMI_TTS_VOICE` で指定できます。初期値は `marin` です。
4. 中継は `127.0.0.1:8765` だけで待ち受けます。運用時は中継と同じホストにHTTPSゲートウェイを置き、`/device/` では端末のBearerトークンを中継に渡します。`/mcp` ではゲートウェイがChatGPT用OAuthを検証し、許可したユーザーの要求だけを `SUMI_MCP_TOKEN` 付きで中継に渡します。ゲートウェイ、OAuth、クラウド配置は別途設定が必要です。
5. ChatGPTでMCPプラグインを接続し、既存のすみの会話でイベント `stopwatch.message` を `device_id: stopwatch` で購読します。発話の文字起こしに短く返答し、イベントに含まれる `request_id` と返答文を `submit_stopwatch_reply` に渡すよう指示します。

中継のローカルテストは `python3 -m unittest relay.test_relay -v` で実行できます。模擬の文字起こし・イベント受信・音声合成を使います。実機への書き込みと既存のすみへの実接続は別途検証します。

音声の実機確認には、Arduino CLIのビルドオプション
`--build-property compiler.cpp.extra_flags=-DSUMI_AUDIO_BENCH` を追加できます。
この検証ビルドはWi-Fiを起動せず、左ボタンで録音した音声を本体で再生します。
録音終了の15秒後にバッジに戻り、その1秒後にアバターが再表示されて録音を再生します。
