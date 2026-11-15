#!/usr/bin/env python3
"""Set a Muse SDK token locally, without terminal echo or secret log files."""
import argparse
import getpass
import json
import re
import subprocess
import sys
import termios
import time

import serial
from serial.tools import list_ports


class SetupError(Exception):
    pass


def validate_token(text):
    token = text.strip()
    if token.startswith('>sdk_token='):
        token = token[len('>sdk_token='):].strip()
    if len(token) != 48:
        raise SetupError(f'入力は{len(token)}文字でした。SDKトークンはmgst_で始まる48文字です。値は送信していません。')
    if not token.startswith('mgst_'):
        raise SetupError('SDKトークンはmgst_で始まります。発行画面のSDKトークンをコピーしてください。値は送信していません。')
    if not re.fullmatch(r'mgst_[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]', token):
        raise SetupError('文字数と接頭辞は正しいものの、文字種または末尾がSDKの形式と一致しません。値は送信していません。')
    return token


def input_token(dialog):
    if not dialog:
        return validate_token(getpass.getpass('Muse SDK token（入力は非表示）: '))
    # Only this child process and Python receive the value. Never print its stdout.
    script = '''
    activate
    set entry to display dialog "C152に設定するMuse SDKトークンを貼り付けてください。\\n値はチャットやファイルへ保存せず、本体へ送信します。" with title "C152 / Muse SDKトークン" default answer "" with hidden answer buttons {"キャンセル", "設定"} default button "設定" cancel button "キャンセル"
    return text returned of entry
    '''
    result = subprocess.run(['/usr/bin/osascript', '-e', script], capture_output=True, text=True)
    if result.returncode:
        raise SetupError('入力を中止しました。本体のトークンは変更していません。')
    return validate_token(result.stdout)


def connect(port):
    device = serial.Serial(port=None, baudrate=115200, timeout=0.2,
                           write_timeout=3, exclusive=True)
    device.dtr = False
    device.rts = False
    device.port = port
    device.open()
    attrs = termios.tcgetattr(device.fileno())
    attrs[2] &= ~termios.HUPCL
    termios.tcsetattr(device.fileno(), termios.TCSANOW, attrs)
    return device


def wait_line(device, prefix, timeout):
    end = time.monotonic() + timeout
    pending = bytearray()
    while time.monotonic() < end:
        pending.extend(device.read(4096))
        while b'\n' in pending:
            line, _, pending = pending.partition(b'\n')
            text = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', line.decode('utf-8', 'replace')).strip()
            if text.startswith(prefix):
                return text
    raise SetupError('本体からの応答がありません。USB接続とscreenの終了を確認してください。')


def check_board(device):
    device.write(b'\n>status\n')
    device.flush()
    response = wait_line(device, '@status ', 10)
    try:
        status = json.loads(response[len('@status '):])
    except ValueError:
        raise SetupError('本体の状態を確認できませんでした。') from None
    if status.get('board') != 'M5Stack StopWatch':
        raise SetupError('接続先がC152ではないため、トークンを送信しませんでした。')
    return status.get('device', {})


def send_token(device, token):
    token = validate_token(token)
    check_board(device)
    # The firmware discards CR and completes commands only on LF.
    device.write(('>sdk_token=' + token + '\n').encode('ascii'))
    device.flush()
    response = wait_line(device, '@sdk_token ', 10)
    if response != '@sdk_token ok':
        raise SetupError('本体がトークンを保存できませんでした。形式と本体の状態を確認してください。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', help='C152 serial port; auto-selects a single Espressif USB device')
    parser.add_argument('--dialog', action='store_true', help='Use a macOS hidden-input dialog')
    args = parser.parse_args()
    try:
        ports = [p for p in list_ports.comports() if p.vid == 0x303a and p.pid == 0x1001]
        if args.port:
            ports = [p for p in ports if p.device == args.port]
        if len(ports) != 1:
            raise SetupError('C152のUSB接続を1台に絞れません。接続を確認し、--portで指定してください。')
        selected = ports[0]
        with connect(selected.device) as device:
            check_board(device)
        token = input_token(args.dialog)
        # The selected USB identity must still be present after the user finishes input.
        current = [p for p in list_ports.comports() if p.device == selected.device
                   and p.serial_number == selected.serial_number]
        if len(current) != 1:
            raise SetupError('入力中にUSB接続が変わりました。送信せず終了します。')
        with connect(selected.device) as device:
            send_token(device, token)
        print('SDKトークンを本体へ保存しました。本体が再起動します。')
        print('Museアプリの Settings → Devices → Add Device から登録してください。')
        return 0
    except (SetupError, serial.SerialException, OSError) as error:
        # Never include captured serial output, AppleScript output, or token text.
        print(str(error) if isinstance(error, SetupError) else 'USB接続または入力画面を開けませんでした。screenを終了して再実行してください。', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\n入力を中止しました。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
