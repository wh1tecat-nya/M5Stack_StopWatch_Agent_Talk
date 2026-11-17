"""Exercise the patched input C, using the SDK's hardware/RTOS fakes."""
from pathlib import Path
import os
import platform
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SDK = ROOT / '.cache/muse-gadget-sdk-463bd2a05380fea4211b0099ad0f70933c108b20/esp32'

class Controls(unittest.TestCase):
    def test_wake_record_settings_and_pairing(self):
        with tempfile.TemporaryDirectory() as td:
            stage = Path(td)
            for p in (SDK / 'components/muse').glob('*.h'): shutil.copy2(p, stage / p.name)
            shutil.copy2(SDK / 'components/muse/muse_input.c', stage / 'muse_input.c')
            shutil.copy2(SDK / 'tests/muse_ptt_ui_fakes.h', stage / 'muse_ptt_ui_fakes.h')
            headers = ['esp_attr.h','esp_err.h','esp_heap_caps.h','esp_log.h','esp_timer.h',
                'esp_codec_dev.h','lvgl.h','driver/gpio.h','nvs.h','nvs_flash.h',
                'freertos/FreeRTOS.h','freertos/task.h','freertos/queue.h','freertos/idf_additions.h','freertos/semphr.h']
            for name in headers:
                p = stage / name; p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text('#include "muse_ptt_ui_fakes.h"\n')
            (stage / 'sdkconfig.h').write_text('#pragma once\n#define CONFIG_MUSE_BOARD_M5STACK_STOPWATCH 1\n'
                '#define CONFIG_MUSE_REALTIME_VOICE 1\n#define CONFIG_MUSE_HATCH 1\n#define CONFIG_SPIRAM 1\n')
            upstream = (SDK / 'tests/muse_ptt_input_harness.c').read_text().split('int main(')[0]
            (stage / 'run.c').write_text(upstream + r'''
static int wakes, settings_toggles;
int64_t esp_timer_get_time(void) { return clock_us; }
void sumi_ui_wake(void) { ++wakes; }
void sumi_ui_toggle_settings(void) { ++settings_toggles; }
bool sumi_ui_settings_open(void) { return menu; }
bool sumi_wifi_active(void) { return false; }
bool muse_link_hatch_linked(void) { return true; }
int main(void) {
    s_queue=&queue; muse_board=&stopwatch_board;
    asleep=true;
    talk_button(MUSE_BTN_TALK_PRESS);
    assert(!asleep && wakes==1 && queue.n==0);
    talk_button(0); talk_button(0);
    talk_button(MUSE_BTN_TALK_RELEASE); assert(queue.n==0);
    talk_button(MUSE_BTN_TALK_PRESS); talk_button(MUSE_BTN_TALK_RELEASE);
    assert(queue.n==2 && queue.items[0].type==MUSE_PTT_DOWN && queue.items[1].type==MUSE_PTT_UP);
    int before=queue.n; call=true; blue();
    assert(settings_toggles==1 && queue.n==before && !speaker_toggles);
    menu=true; talk_button(MUSE_BTN_TALK_PRESS | MUSE_BTN_TALK_RELEASE); assert(queue.n==before);
    pairing=true; talk_button(MUSE_BTN_TALK_PRESS | MUSE_BTN_TALK_RELEASE); assert(queue.n==before);
    pairing=false; menu=false; call=false;
    asleep=true; blue(); assert(!asleep && settings_toggles==1 && wakes==2);
    dedicated_buttons(MUSE_BTN_POWER_PRESS | MUSE_BTN_POWER_RELEASE); assert(asleep);
    dedicated_buttons(MUSE_BTN_POWER_PRESS | MUSE_BTN_POWER_RELEASE); assert(!asleep && wakes==3);
    sumi_input_end_call(); assert(atomic_exchange(&s_end_requested,false));
    queue_full=true; unsigned epoch=muse_input_call_epoch();
    talk_button(MUSE_BTN_TALK_PRESS); assert(muse_input_call_epoch()!=epoch);
    return 0;
}
''')
            linker = '-Wl,-dead_strip' if platform.system() == 'Darwin' else '-Wl,--gc-sections'
            command = [os.environ.get('CC', 'cc'), '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra',
                '-Wno-unused-parameter','-Wno-unused-function','-Wno-unused-variable',
                '-ffunction-sections','-fdata-sections',linker,'-fsanitize=address,undefined',
                '-include',str(SDK / 'tests/host_compat.h'),'-I',str(stage),str(stage / 'run.c'),'-o',str(stage / 'run')]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([str(stage / 'run')], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

if __name__ == '__main__': unittest.main()
