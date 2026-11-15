#!/usr/bin/env python3
"""Save an OpenAI API key to C152 through a hidden local input dialog."""
import argparse
import getpass
import re
import subprocess
import sys
import serial
from serial.tools import list_ports
from set_sdk_token import SetupError, check_board, connect, wait_line


def validate_key(text):
    key = text.strip()
    if not 20 <= len(key) < 512 or not re.fullmatch(r'sk-[A-Za-z0-9_-]+', key):
        raise SetupError('OpenAI APIキーの形式を確認してください。値は送信していません。')
    return key


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dialog', action='store_true')
    args = parser.parse_args()
    try:
        ports = [p for p in list_ports.comports() if p.vid == 0x303a and p.pid == 0x1001
                 and (p.serial_number or '').upper() == 'C4:9E:7E:12:A3:90']
        if len(ports) != 1:
            raise SetupError('対象のC152をUSBで接続してください。')
        selected = ports[0]
        with connect(selected.device) as device:
            check_board(device)
        if args.dialog:
            script = '''
            activate
            set entry to display dialog "音声認識に使うOpenAI APIキーを入力してください。\\n本体へ保存してOpenAI認識に切り替えます。音声はOpenAI APIへ送信され、APIの利用料が発生します。\\n値はチャットやMacのファイルへ保存しません。" with title "C152 / OpenAI音声認識" default answer "" with hidden answer buttons {"キャンセル", "設定"} default button "設定" cancel button "キャンセル"
            return text returned of entry
            '''
            result = subprocess.run(['/usr/bin/osascript', '-e', script], capture_output=True, text=True)
            if result.returncode:
                raise SetupError('入力を中止しました。本体の設定は変更していません。')
            key = validate_key(result.stdout)
            result = None
        else:
            key = validate_key(getpass.getpass('OpenAI API key（入力は非表示）: '))
        current = [p for p in list_ports.comports() if p.device == selected.device
                   and p.serial_number == selected.serial_number]
        if len(current) != 1:
            raise SetupError('入力中にUSB接続が変わりました。送信せず終了します。')
        with connect(selected.device) as device:
            check_board(device)
            device.write(('>openai_key=' + key + '\n').encode('ascii')); device.flush()
            key = None
            if wait_line(device, '@openai_key ', 10) != '@openai_key ok':
                raise SetupError('キーを保存できませんでした。会話を終了して再実行してください。')
            device.write(b'>asr=1\n'); device.flush()
            if wait_line(device, '@asr ', 10) != '@asr ok':
                raise SetupError('キーは保存済みです。本体の設定「音声認識」でOpenAIを選んでください。')
        print('APIキーを保存し、音声認識をOpenAIへ切り替えました。再起動不要です。')
        return 0
    except (SetupError, serial.SerialException, OSError) as error:
        print(str(error) if isinstance(error, SetupError) else 'USB接続または入力画面を開けませんでした。', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('入力を中止しました。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
