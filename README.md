# すみ × M5Stack StopWatch

StopWatch C152から、設定した2.4 GHz Wi-Fiを経由して、既存のChatGPT dot「すみ」に音声メッセージを送る試作です。運用時はStopWatchと中継サーバーを使います。

## 操作

- 待機中はSUMIバッジを低輝度で表示します。右ボタンでアバターに切り替わります。
- 左ボタン（A）を押している間は録音します。録音中は赤い表示と入力レベルが出ます。ボタンを離すと送信します。15秒に達すると録音を止め、ボタンを離すまで送信を待ちます。
- 待機中もWi-Fiを維持し、約3秒ごとに中継に返信を確認します。送信直後の90秒間は約0.75秒間隔で確認します。返信を受け取るとアバターに切り替わり、本体スピーカーで合成音声を再生します。
- 右ボタンでのリアルタイム通話は今後の実装範囲です。

## 開発と接続

Cloudflare Worker構成では、録音を `gpt-transcribe` で文字起こしし、すみの返答テキストをStopWatchに戻します。StopWatchはsanoTTS-jpで音声を生成します。[Workerの設定](cloudflare/README.md)

このワークスペースでWorkerのURLと端末トークンを `cloudflare/.env.text-probe.json` に用意済みの場合は、`python3 tools/configure_stopwatch.py /path/to/root-ca.pem` でWi-Fi設定を対話入力できます。設定は端末ファームウェアをビルドする前に行います。

本体で読み上げるビルドには、[sanoTTS-jp v1.2.0](https://github.com/ayutaz/sanoTTS-jp/releases/tag/v1.2.0) のArduinoライブラリと `sanoTTS-jp-voice-tsukuyomi-v4.zip` を使います。ビルド時はC++に `-DSUMI_SANOTTS -DSANOTTS_ARENA_HEAP=1`、Cに `-DSANOTTS_ARENA_HEAP=1` を指定します。`SumiAvatar/partitions.csv` の `dict` は既定のFFat領域を使用するため、書き込み前に実機のフラッシュをバックアップしてください。パーティション表とアプリを書き込んだ後、公式リリースの `k1-dict-213000-8mb-m5.bin` を辞書領域 `0x610000` に書き込みます。音声モデルと生成音声には[モデルライセンス](https://github.com/ayutaz/sanoTTS-jp/blob/v1.2.0/LICENSE-MODEL.md)が適用されます。

### Python版でのローカル検証

1. Arduino IDEで `SumiAvatar/SumiAvatar.ino` を開きます。ESP32 Arduino Core 3.3.7、M5Unified 0.3.0、M5GFX 0.3.0でビルドを確認しました。ボードは `ESP32-S3-Box`、フラッシュは16 MB、PSRAMはOPIです。
2. `SumiAvatar/stopwatch_config.example.h` を同じフォルダの `stopwatch_config.h` にコピーし、SSID、Wi-Fiパスワード、中継のHTTPS URL、端末トークン、TLSのルートCAを設定します。設定ファイルはGitの対象外です。設定なしでも待機画面とアバター表示をビルドできます。
3. 中継をPython 3.11以降で `python3 -m relay.server --database /path/to/sumi.sqlite3` として起動します。環境変数に `SUMI_DEVICE_TOKEN`、`SUMI_MCP_TOKEN`、`OPENAI_API_KEY`、`SUMI_CALLBACK_HOST` を設定します。トークンは別々の32文字以上のランダム値とし、許可するMCPコールバックのホスト名を指定します。中継は録音を `gpt-transcribe` で文字起こしし、返答を `gpt-4o-mini-tts` で音声化します。読み上げの声は `SUMI_TTS_VOICE` で指定できます。初期値は `marin` です。
4. 中継は `127.0.0.1:8765` だけで待ち受けます。運用時は中継と同じホストにHTTPSゲートウェイを置き、`/device/` では端末のBearerトークンを中継に渡します。`/mcp` ではゲートウェイがChatGPT用OAuthを検証し、許可したユーザーの要求だけを `SUMI_MCP_TOKEN` 付きで中継に渡します。ゲートウェイ、OAuth、クラウド配置は別途設定が必要です。
5. ChatGPTでMCPプラグインを接続し、既存のすみの会話でイベント `stopwatch.message` を `device_id: stopwatch` で購読します。発話の文字起こしに短く返答し、イベントに含まれる `request_id` と返答文を `submit_stopwatch_reply` に渡すよう指示します。

中継のローカルテストは `python3 -m unittest relay.test_relay -v` で実行できます。模擬の文字起こし・イベント受信・音声合成を使います。実機への書き込みと既存のすみへの実接続は別途検証します。

音声の実機確認には、Arduino CLIのビルドオプション
`--build-property compiler.cpp.extra_flags=-DSUMI_AUDIO_BENCH` を追加できます。
この検証ビルドはWi-Fiを起動せず、左ボタンを押している間の録音を本体で再生します。
録音終了の15秒後にバッジに戻り、その1秒後にアバターが再表示されて録音を再生します。
