"""Exercise the real amp gate and PCM writer, including queued-tail drain."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from test_controls import SDK


def function(source, signature):
    start = source.index(signature)
    return source[start:source.index('\n}\n', start) + 3]


class AudioGate(unittest.TestCase):
    def test_idle_mute_reopen_tail_and_switch_failure(self):
        source = (SDK / 'components/muse/muse_audio.c').read_text()
        functions = '\n'.join(function(source, name) for name in [
            'static esp_err_t speaker_power(', 'void muse_audio_quiet(',
            'static void apply_volume(', 'static esp_err_t open_codecs(',
            'esp_err_t muse_audio_write('])
        harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
typedef int esp_err_t;
#define ESP_OK 0
#define ESP_FAIL -1
#define ESP_CODEC_DEV_OK 0
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
#define ESP_RETURN_ON_FALSE(a,b,...) do { if(!(a)) return b; } while(0)
#define MUSE_AUDIO_RATE 16000
#define MUSE_AUDIO_CHUNK 320
#define CHANNELS 2
#define pdMS_TO_TICKS(x) (x)
typedef struct { int sample_rate,channel,bits_per_sample; } esp_codec_dev_sample_info_t;
static bool s_open, s_amp_on, hardware_on, fail_switch, audible=true;
static int s_spk=1, s_mic=2, vol=90, locks, switches, writes, mic_opens;
static int16_t s_out_stereo[MUSE_AUDIO_CHUNK*2];
static size_t drain_bytes;
static void lock(void){assert(!locks++);}
static void unlock(void){assert(locks--==1);}
static int gate(bool on){assert(locks);++switches;if(fail_switch)return ESP_FAIL;hardware_on=on;return ESP_OK;}
static struct { int (*mic_init)(void); int (*speaker_power)(bool); } board={0,gate};
static const typeof(board) *muse_board=&board;
static int esp_codec_dev_open(int dev,esp_codec_dev_sample_info_t*p){assert(p->sample_rate==16000);if(dev==s_spk)hardware_on=true;else ++mic_opens;return 0;}
static int esp_codec_dev_close(int dev){(void)dev;return 0;}
static bool codecs_reachable(void){return s_open;}
static void esp_codec_dev_set_out_vol(int dev,int v){assert(dev==s_spk);vol=v;}
static int esp_codec_dev_write(int dev,const void*raw,size_t n){
 assert(dev==s_spk);++writes;const int16_t*p=raw;bool zero=true;
 for(size_t i=0;i<n/2;++i){zero &= p[i]==0;if(i%2)assert(p[i]==p[i-1]);}
 if(locks){assert(hardware_on && zero);drain_bytes+=n;}
 return 0;
}
static bool muse_settings_speaker_on(void){return audible;}
static int muse_settings_volume(void){return vol;}
static void vTaskDelay(int ms){(void)ms;}
'''
        main = r'''
int main(void){
 lock();assert(open_codecs()==0);unlock();
 assert(s_open && mic_opens==1 && !hardware_on && !s_amp_on);
 int16_t pcm[640];for(int i=0;i<640;i++)pcm[i]=(i%2)?1234:-1234;
 assert(muse_audio_write(pcm,640)==0 && hardware_on && writes==2);
 int before=switches;muse_audio_quiet();
 assert(!hardware_on && s_open && switches==before+1 && drain_bytes==1920*4);
 before=writes;muse_audio_quiet();assert(writes==before);
 assert(muse_audio_write(pcm,320)==0 && hardware_on);
 lock();apply_volume(0);unlock();assert(!hardware_on);
 assert(muse_audio_write(pcm,320)==0 && !hardware_on);
 vol=90;audible=false;assert(muse_audio_write(pcm,320)==0 && !hardware_on);
 audible=true;fail_switch=true;before=writes;
 assert(muse_audio_write(pcm,320)==ESP_FAIL && writes==before && !s_amp_on);
 fail_switch=false;assert(muse_audio_write(pcm,320)==0 && hardware_on);
 muse_audio_quiet();lock();assert(open_codecs()==0);unlock();assert(!hardware_on && mic_opens==2);
 board.speaker_power=0;assert(muse_audio_write(pcm,320)==0);before=writes;muse_audio_quiet();assert(writes==before);
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)
            (path / 'test.c').write_text(harness + functions + main)
            run = subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                                  '-fsanitize=address,undefined', str(path / 'test.c'), '-o', str(path / 'run')],
                                 capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            run = subprocess.run([str(path / 'run')], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
