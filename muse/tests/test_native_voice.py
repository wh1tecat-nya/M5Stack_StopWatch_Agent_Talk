"""Run the production voice call against the SDK's synthetic transport."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from test_controls import SDK


class NativeVoice(unittest.TestCase):
    def test_side_chat_admission_mismatch_and_404_fallback(self):
        test = SDK / 'tests'
        muse = SDK / 'components/muse'
        fakes = test / 'voice_call_fakes'
        source = (test / 'muse_voice_call_harness.cpp').read_text().replace('int main(', 'int upstream_main(')
        source += r'''
#include "sumi_chat.h"
#include "sumi_voice_options.h"
int main(int argc, char **argv) {
    assert(argc==2 && muse_voice_call_init() && muse_voice_call_ready());
    reset_world();sumi_chat_select(1);
    int which=atoi(argv[1]);
    if(which==0) {
        SESSION_EVENT="{\"type\":\"hatch.voice.session\",\"call_id\":\"%s\",\"session_id\":\"voice-session\",\"thread_id\":\"3622d9af-4010-4247-ac57-cd702e8bc9d3\"}";
        int64_t id=live_call();
        bool scoped=false;
        for(const auto &op:s_ops) if(op.op=="media") scoped=op.body.find("\"thread_id\":\"3622d9af-4010-4247-ac57-cd702e8bc9d3\"")!=std::string::npos;
        assert(scoped);muse_voice_call_end();serve_ending(id);expect_event(MUSE_VOICE_CALL_EV_ENDED);
    } else if(which==1) {
        muse_voice_call_start();poll();int64_t id=media_id();
        stream_end(id,false,404);poll();int64_t end=end_call_id();assert(end);
        stream_end(end,false,404);poll_expect_drop();
        char text[96];expect_event(MUSE_VOICE_CALL_EV_ENDED,text);
        assert(strstr(text,"404") && sumi_native_voice_unavailable());
        sumi_native_voice_retry();assert(!sumi_native_voice_unavailable());
        sumi_native_voice_rejected(403);assert(!sumi_native_voice_unavailable());
    } else {
        muse_voice_call_start();poll();int64_t id=media_id();head(id,200);
        char event[512];snprintf(event,sizeof(event),SESSION_EVENT,call_id_of(id).c_str());
        line(id,event);poll();serve_ending(id);
        char text[96];expect_event(MUSE_VOICE_CALL_EV_ENDED,text);
        assert(strstr(text,"一致しません") && !read_all());
    }
}
'''
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / 'harness.cpp').write_text(source)
            inc = ['-I', str(fakes), '-I', str(test), '-I', str(muse)]
            flags = ['-O1','-g','-fsanitize=address,undefined','-fno-omit-frame-pointer']
            files = [('cc','-std=c11',muse/'muse_voice_call_core.c'),
                     ('cc','-std=c11',muse/'sumi_chat.c'),
                     ('cc','-std=c11',muse/'sumi_voice_options.c'),
                     ('c++','-std=gnu++17',muse/'muse_voice_call.cpp'),
                     ('c++','-std=gnu++17',out/'harness.cpp')]
            objects = []
            for i,(compiler,std,path) in enumerate(files):
                obj = out / f'{i}.o'; objects.append(str(obj))
                extra = ['-include',str(test/'host_compat.h'),'-include',str(fakes/'wall_clock.h')] if path.name=='muse_voice_call.cpp' else []
                run = subprocess.run([compiler,std,*flags,*inc,*extra,'-c',str(path),'-o',str(obj)],capture_output=True,text=True)
                self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            subprocess.run(['c++',*flags,*objects,'-lm','-o',str(out/'run')],check=True)
            for case in range(3):
                run = subprocess.run([str(out/'run'),str(case)],capture_output=True,text=True,timeout=10,
                    env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0:abort_on_error=1'})
                self.assertEqual(run.returncode,0,f'case {case}: '+run.stdout+run.stderr)
