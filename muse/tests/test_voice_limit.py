"""Held-only audio: run the production voice loop with a scripted clock/mic."""
from pathlib import Path
import os
import platform
import shutil
import subprocess
import tempfile
import unittest
from test_controls import SDK

class VoiceLimit(unittest.TestCase):
    def test_capture_limit_and_retry_memory_lifetime(self):
        with tempfile.TemporaryDirectory() as td:
            stage = Path(td)
            for p in (SDK/'components/muse').glob('*.h'): shutil.copy2(p,stage/p.name)
            for name in ['muse_input.c','muse_voice.c']: shutil.copy2(SDK/'components/muse'/name,stage/name)
            shutil.copy2(SDK/'tests/muse_ptt_ui_fakes.h',stage/'muse_ptt_ui_fakes.h')
            for name in ['esp_attr.h','esp_err.h','esp_heap_caps.h','esp_log.h','esp_timer.h','esp_codec_dev.h',
                'lvgl.h','driver/gpio.h','nvs.h','nvs_flash.h','freertos/FreeRTOS.h','freertos/task.h',
                'freertos/queue.h','freertos/idf_additions.h','freertos/semphr.h']:
                p=stage/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('#include "muse_ptt_ui_fakes.h"\n')
            (stage/'sdkconfig.h').write_text('#pragma once\n#define CONFIG_MUSE_BOARD_M5STACK_STOPWATCH 1\n'
                '#define CONFIG_MUSE_REALTIME_VOICE 1\n#define CONFIG_MUSE_HATCH 1\n#define CONFIG_SPIRAM 1\n')
            source=(SDK/'tests/muse_ptt_voice_harness.c').read_text().split('int main(')[0]
            source=source.replace('frame>=(scenario==1?22:16)','frame>=(scenario==100?900:scenario==1?22:16)')
            source=source.replace('++notes; if(scenario>=20) longjmp(finish,2); assert(!"PTT opened a note");','++notes;')
            source=source.replace('    muse_hatch_ev_t ev=hatch_end;', r'''
    if(scenario>=103) {
        static int events;
        *p=0;
        if(events++==0) {
            if(scenario==104)return MUSE_HATCH_EV_ERROR;
            if(scenario==105)return MUSE_HATCH_EV_REPLY;
            if(scenario==106)return MUSE_HATCH_EV_DONE;
            return MUSE_HATCH_EV_SENT;
        }
        /* The retry copy is already gone before reply playback or failure. */
        assert(scenario==107 ? s_held[0].pcm==NULL : s_rec==NULL);
        return scenario==106 ? MUSE_HATCH_EV_NONE : MUSE_HATCH_EV_ERROR;
    }
    muse_hatch_ev_t ev=hatch_end;''')
            source=source.replace('    if(scenario==41 && frame==2)',r'''
    if(scenario==100) {
        if(frame==800) { assert(delivered>0 && accepted_at[delivered-1]<15000000); push(MUSE_PTT_UP); }
        if(frame==802) push(MUSE_PTT_DOWN);
        if(frame==860) push(MUSE_PTT_UP);
    }
    if(scenario==101 && frame==800) push(MUSE_PTT_UP);
    if((scenario==102 || scenario==108 || scenario==109) && frame==10) push(MUSE_PTT_UP);
    if(scenario==110 && frame==20) push(MUSE_PTT_UP);
    if(scenario==41 && frame==2)''')
            source += r'''
void sumi_ui_wake(void) {}
void muse_audio_quiet(void) {}
bool sumi_asr_enabled(void) { return scenario==108; }
bool sumi_phone_current(uint32_t turn) { return turn == 1; }
static size_t streamed_frames;
void sumi_phone_note_progress(uint32_t turn, const int16_t *pcm, size_t frames) {
    assert(turn==1 && pcm==s_rec && frames==s_rec_n && frames>streamed_frames);
    streamed_frames=frames;
}
void sumi_ui_toggle_settings(void) {}
bool sumi_ui_settings_open(void) { return false; }
bool sumi_wifi_active(void) { return false; }
bool muse_link_hatch_linked(void) { return true; }
bool sumi_native_voice_unavailable(void) { return false; }
int main(int argc,char**argv){
    assert(argc==2); scenario=atoi(argv[1]);s_queue=input_queue=&queue;ready=true;
    s_pre=calloc(PRE_CHUNKS,sizeof(*s_pre));
    if(scenario>=103 && scenario<=107){
        int16_t **retry=scenario==107 ? &s_held[0].pcm : &s_rec;
        *retry=malloc(MAX_FRAMES*sizeof(int16_t));assert(*retry);
        bool received=false;
        assert(!hatch_reply(&received,retry));
        assert(received==(scenario!=104));
        assert((*retry==NULL)==received);
        free(*retry);*retry=NULL;
    } else if(scenario==100){
        run_call(true,0);
        assert(starts==1 && ended_event && !held);
        bool second=false;
        for(int i=0;i<delivered;++i){
            assert(accepted_at[i]<15000000 || (accepted_at[i]>=32000000 && accepted_at[i]<34400000));
            second |= accepted_at[i]>=32000000;
        }
        assert(second);
    } else {
        s_rec=calloc(MAX_FRAMES,sizeof(int16_t));s_pre_fill=3;
        s_phone_turn=scenario==109 || scenario==110 ? 1 : 0;
        size_t count=0;char why[96];assert(record(false,&count,why,sizeof(why)));
        assert(s_pre_fill==0 && s_rec[0]==101);
        if(scenario==101)assert(count==MAX_FRAMES && s_rec_n==MAX_FRAMES && frame==800);
        else if(scenario==110)assert(count==20*MUSE_AUDIO_CHUNK && frame==26 && streamed_frames==s_rec_n);
        else assert(count==10*MUSE_AUDIO_CHUNK && frame==16 && s_rec_n==16*MUSE_AUDIO_CHUNK);
        if(scenario!=110)assert(streamed_frames==0);
        if(scenario>=108)assert(notes==0 && !s_live && !s_tried);
        free(s_rec);
    }
    free(s_pre);return 0;
}
'''
            (stage/'run.c').write_text(source)
            linker='-Wl,-dead_strip' if platform.system()=='Darwin' else '-Wl,--gc-sections'
            result=subprocess.run([os.environ.get('CC','cc'),'-std=gnu11','-O1','-g','-ffunction-sections',
                '-fdata-sections',linker,'-fsanitize=address,undefined','-include',str(SDK/'tests/host_compat.h'),
                '-I',str(stage),str(stage/'run.c'),str(SDK/'components/muse/muse_voice_call_core.c'),'-lm','-o',str(stage/'run')],
                capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            for case in range(100,111):
                result=subprocess.run([str(stage/'run'),str(case)],capture_output=True,text=True,timeout=10)
                self.assertEqual(result.returncode,0,f'case {case}: '+result.stdout+result.stderr)

if __name__=='__main__':unittest.main()
