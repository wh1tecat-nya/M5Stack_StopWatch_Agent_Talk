# 固定WORLD予測器・波形合成のC++版

大幅な聴感改善を確認した包絡補正版を、量子化せずC++へ移植したものです。通常の読み上げにはまだ組み込んでいません。明示的に選択した診断ビルドだけで、固定入力の性能を測定します。モデルの重み、教師データ、音声合成方式は変更していません。

対象は `sumi_world_envelope_fp32_v1` の固定保存点です。

- 保存点SHA-256: `1fd7d966f410f7e29aee6e1ee2f8ad64bfd87cbd8847057be93931d2384616d1`
- 重みSHA-256: `75c2b800975f31b5204b926df5f1cef6eaabcd6ee5f684d8a66c9a19d194382b`
- 重み: 114,636パラメータ＋190個の正規化値、459,304 bytes

`world_envelope.cpp` と `.h` が推論本体です。外部の行列ライブラリや動的メモリー確保を使用しません。CLIとテストはホスト検証用です。モデル構造と重みの並び順は固定で、任意の `model.json` を読み込む実装ではありません。`bind` 前に呼び出し元で対象の形式、並び順、サイズ、SHA-256を照合してください。今回のPython監査では、固定した書き出しメタデータ全体との一致を確認しています。

## 入出力とメモリー

入力は、5ms間隔へ補間した32チャンネルのIrodori codec潜在表現です。生の25fps配列や文章は入力できません。配列はチャンネル優先の連続FP32で、正規化は内部で行います。

`predict` は64チャンネルの正規化予測を返します。`to_physical` は同じ領域を62個の符号化音響特徴、有声F0、声の有無の確率へ変換します。声の有無はPython版と同じ確率0.5の境界で判断します。重みは利用中に変更・解放せず、入出力、作業領域、重みの領域は互いに重ねないでください。

分割する場合は、必要な出力区間の前後に最大50フレームを付け、中央の出力だけを取り出します。文章の先頭と末尾では実在するフレームだけを渡します。入力をゼロで追加して長さを揃える方法では、層ごとの端処理が変わります。64出力フレームを一単位にする場合、内部の最大窓は164フレームです。

| 項目 | 64フレーム分割時 |
| --- | ---: |
| 作業領域 | 230,912 bytes（225.5KiB） |
| 窓の入力・出力 | 62,976 bytes（61.5KiB） |
| 重み・正規化値 | 459,304 bytes（約448.5KiB） |

モデル構造体、スタック、WORLD合成、文章処理、音声バッファ、OSは表に含みません。構造体はMacで936 bytesです。これらを合計しても本体全体の必要RAMとはなりません。先読みを含む方式であり、実機の遅延は未測定です。

## 確認結果

比較10文について、Python版とC++版の正規化予測の最大絶対差は約4.77e-6、音程差は最大約0.00101 cents、声の有無のラベルは完全一致しました。C++の全文推論と64フレーム分割推論は全10文で完全一致しました。

C++が出した特徴を従来のPython版WORLDへ渡した波形の相対L2差は最大約0.000405、RMSの相対差は最大約8.34e-7です。波形のビット一致や新たな聴感合格は主張しません。WORLD波形合成自体のC++移植は未実施です。

1、49、51、65、129フレームの短い入力を7フレームずつ処理し、境界と端数も照合しました。AddressSanitizerとUndefinedBehaviorSanitizerを有効にし、実モデルによる境界検証と、領域外書き込み・領域の重なり・不足バッファ・不正な正規化値・非有限入力などの検査が通過しました。

Apple clang 17 / arm64のMacでは、3回中最速の推論時間は音声1秒あたり全文約0.016秒、分割約0.040〜0.042秒でした。ファイル入出力、モデル読み込み、WORLD合成、文章処理は含みません。C152の速度や消費電力を示す値ではありません。

ESP32-S3用コンパイラーで、例外とRTTIを無効にした推論本体のオブジェクト生成も成功しました。オブジェクトのtext領域は3,675 bytes、data/bssは0です。重み、数学ライブラリ、アプリ本体は別です。リンク・実機実行・ファームウェア書き込みは行っていません。

## 再検証

プロジェクトのルートで、別の空の出力先を用意してください。

```sh
c++ -std=c++17 -O2 -ffp-contract=off -Wall -Wextra -Werror \
  muse/voice_training/native/world_envelope.cpp \
  muse/voice_training/native/world_envelope_cli.cpp \
  -o /tmp/world_envelope_cli

c++ -std=c++17 -O1 -g -ffp-contract=off \
  -fsanitize=address,undefined -fno-omit-frame-pointer -Wall -Wextra -Werror \
  muse/voice_training/native/world_envelope.cpp \
  muse/voice_training/native/world_envelope_cli.cpp \
  -o /tmp/world_envelope_cli_sanitized

c++ -std=c++17 -O1 -g -ffp-contract=off \
  -fsanitize=address,undefined -fno-omit-frame-pointer -Wall -Wextra -Werror \
  muse/voice_training/native/world_envelope.cpp \
  muse/voice_training/native/test_world_envelope.cpp \
  -o /tmp/test_world_envelope

muse/.cache/decoder-test-venv/bin/python muse/voice_training/audit_world_native.py \
  --baseline muse/.cache/irodori-world-envelope-baseline-20261113 \
  --data muse/.cache/irodori-world-envelope-input-20261113/data \
  --executable /tmp/world_envelope_cli \
  --sanitized-executable /tmp/world_envelope_cli_sanitized \
  --invariant-test /tmp/test_world_envelope \
  --output /tmp/world_native_audit
```

2026-11-13時点の保存結果は `muse/.cache/irodori-world-native-20261113/analysis/report.json`、作業状態は `world_native_status.json` です。2026-11-14にはWORLD波形合成と実機測定を追加し、1×1畳み込みの読み出し順を改善しました。10文の物理特徴は変更前と完全一致しています。最新の手順は [WORLD_SYNTHESIS.md](WORLD_SYNTHESIS.md)、状態は `world_synthesis_status.json` を参照してください。音素の時間対応と文章からcodec潜在表現を作るモデル、語尾「かな」の再合成由来の違和感は未解決です。
