"""Exercise actual diagnostic state transitions with failed rail/codec recovery."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class NoiseTest(unittest.TestCase):
    def test_timeout_cancel_readback_and_restore_failures(self):
        harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include "sumi_noise_test.h"
static int64_t now;
static bool audio=true, rail=true, fail_open, fail_read, fail_off;
static int opens, closes;
int64_t esp_timer_get_time(void){return now;}
void muse_audio_quiet(void){}
void muse_audio_power(bool on){
 if(on){++opens;if(!fail_open)audio=rail=true;}
 else{++closes;audio=false;if(!fail_off)rail=false;}
}
bool muse_audio_is_on(void){return audio;}
int sumi_audio_rail_enabled(void){return fail_read?-1:rail;}
int main(void){
 assert(sumi_noise_test_request());assert(!sumi_noise_test_request());
 assert(sumi_noise_test_step(false)&&!audio&&!rail);
 now=19999999;assert(sumi_noise_test_step(false)&&opens==0);
 now++;assert(!sumi_noise_test_step(false)&&audio&&rail&&opens==1);
 assert(sumi_noise_test_state()==SUMI_NOISE_DONE);
 assert(sumi_noise_test_request());sumi_noise_test_cancel();
 assert(!sumi_noise_test_step(false)&&closes==1);
 assert(sumi_noise_test_request());assert(sumi_noise_test_step(false));
 now+=1000000;sumi_noise_test_cancel();assert(!sumi_noise_test_step(false)&&audio);
 fail_off=true;assert(sumi_noise_test_request());
 assert(!sumi_noise_test_step(false)&&audio&&sumi_noise_test_state()==SUMI_NOISE_FAILED);
 fail_off=false;fail_read=true;assert(sumi_noise_test_request());
 assert(sumi_noise_test_step(false)&&audio&&sumi_noise_test_state()==SUMI_NOISE_RETRY);
 fail_read=false;now+=1000000;assert(!sumi_noise_test_step(false));
 assert(sumi_noise_test_state()==SUMI_NOISE_FAILED);
 assert(sumi_noise_test_request());assert(sumi_noise_test_step(false));
 fail_open=true;now+=20000000;assert(sumi_noise_test_step(false)&&!audio);
 int before=opens;now+=999999;assert(sumi_noise_test_step(false)&&opens==before);
 now++;assert(sumi_noise_test_step(false)&&opens==before+1);
 fail_open=false;now+=1000000;assert(!sumi_noise_test_step(false)&&audio);
 assert(sumi_noise_test_state()==SUMI_NOISE_DONE);
 assert(sumi_noise_test_request());assert(sumi_noise_test_step(false));
 fail_open=true;assert(!sumi_noise_test_step(true));
 assert(sumi_noise_test_state()==SUMI_NOISE_FAILED&&!sumi_noise_test_active());
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / 'run.c').write_text(harness)
            (p / 'muse_audio.h').write_text('#include <stdbool.h>\nvoid muse_audio_quiet(void);\nvoid muse_audio_power(bool);\nbool muse_audio_is_on(void);\n')
            (p / 'esp_timer.h').write_text('#include <stdint.h>\nint64_t esp_timer_get_time(void);\n')
            (p / 'esp_log.h').write_text('#define ESP_LOGI(...) ((void)0)\n#define ESP_LOGW(...) ((void)0)\n')
            result = subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                '-fsanitize=address,undefined', '-I', str(p), '-I', str(ROOT / 'overlay'),
                str(p / 'run.c'), str(ROOT / 'overlay/sumi_noise_test.c'), '-o', str(p / 'run')],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([str(p / 'run')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
