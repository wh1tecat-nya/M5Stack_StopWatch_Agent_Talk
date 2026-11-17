"""Exercise production subscription events, acknowledgements and note headers."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from test_controls import SDK


class ChatRouting(unittest.TestCase):
    def test_side_chat_rejects_other_context_and_mismatched_ack(self):
        src = (SDK / 'components/muse/muse_chat_session.cpp').read_text()
        constants = src[src.index('#define MIC_RATE'):src.index('/* ---- Voice task')]
        types = src[src.index('enum phase_t'):src.index('/* 10 KB')]
        handlers = src[src.index('static int find_msg('):src.index('/* ---- Turn: speech ---- */')]
        note = src[src.index('static bool open_note(void)\n{'):src.index('/* Moves the mic into the note request')]
        send = src[src.index('static void send_chat(const char *text,'):src.index('static void post_chat(const char *text)')]
        code = r'''
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include "host_compat.h"
#include "cJSON.h"
#include "minimp3.h"
#include "muse_chat_priv.h"
#include "sumi_chat.h"
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
''' + constants + types + r'''
static turn_t s_turn;
static char s_reply_shown[EV_TEXT];
static int64_t s_last_seq;
static int shown, failed, sent;
static int64_t now_us() { return 12345; }
enum mark_t { M_TEXT, M_DONE, M_ACK };
static void mark(mark_t) {}
static void emit(muse_hatch_ev_t ev, const char *) { if (ev==MUSE_HATCH_EV_SENT) ++sent; }
static void turn_fail(const char *) { ++failed; s_turn.phase=P_IDLE; }
void muse_hatch_console(const char *event, const char *, const char *, ...) { if (!strcmp(event,"text")) ++shown; }
void muse_hatch_tail_words(const char *s,char *d,size_t n){strlcpy(d,s,n);}
bool muse_hatch_caption_at(const char *s,size_t,char *d,size_t n){strlcpy(d,s,n);return *s;}
struct stream_t { char *line; size_t len; };
''' + handlers + r'''
enum kind_t { K_CHAT };
static char header[512];
static int64_t open_stream(kind_t,const char*,const char*,const char*,const char*,const char* body,bool){if(body)strlcpy(header,body,sizeof(header));return 3;}
static void disconnect(const char*) {}
static bool send_body(int64_t,const uint8_t *data,size_t n,bool){assert(n<sizeof(header));memcpy(header,data,n);header[n]=0;return true;}
void muse_hatch_wav_header(uint8_t *p,int){memset(p,0,44);}
''' + note + send + r'''
static void event(const char *session, int seq, const char *text) {
    cJSON *root=cJSON_CreateObject(), *payload=cJSON_CreateObject();
    cJSON_AddStringToObject(root,"type","event");cJSON_AddStringToObject(root,"event","delta.text_append");
    cJSON_AddNumberToObject(root,"seq",seq);cJSON_AddItemToObject(root,"payload",payload);
    if(session)cJSON_AddStringToObject(payload,"session_id",session);
    cJSON_AddStringToObject(payload,"message_id","reply");cJSON_AddStringToObject(payload,"text",text);
    on_event(root);cJSON_Delete(root);
}
static void ack(const char *session, bool thread) {
    char body[256];snprintf(body,sizeof(body),"{\"result\":{\"message_id\":\"note\",\"session_id\":\"%s\",\"is_thread\":%s}}",session,thread?"true":"false");
    stream_t stream{body,strlen(body)};on_chat_ack(&stream);
}
int main(){
    sumi_chat_select(1);s_turn.phase=P_WAIT_REPLY;s_turn.text=true;
    char side[SUMI_CHAT_ID_BYTES+1];sumi_chat_id(side);
    assert(!strcmp(side,SUMI_CHAT_LEGACY_ID));
    sumi_chat_id(s_turn.session_id);
    event("main-chat",500,"Wrong main context");event(nullptr,501,"Unscoped");
    assert(!shown && !s_turn.nmsgs && !s_last_seq);
    ack("main-chat",false);assert(failed==1 && !sent);
    s_turn.phase=P_WAIT_REPLY;ack(side,true);assert(sent==1 && s_turn.acked);
    event(side,1,"Correct side chat");assert(shown==1 && s_turn.nmsgs==1);
    event(side,1,"Duplicate");assert(shown==1);
    s_turn=turn_t{};s_turn.phase=P_WAIT_REPLY;s_turn.text=true;
    sumi_chat_id(s_turn.session_id);
    ack(side,true);event(side,2,"Second turn");assert(shown==2);
    cJSON *parent=cJSON_Parse("{\"reply_to_message_id\":\"self-reply\"}");
    assert(bind_msg("self-reply",parent)>=0);cJSON_Delete(parent);
    parent=cJSON_Parse("{\"reply_to_message_id\":\"unrelated\"}");
    assert(bind_msg("alien",parent)<0);cJSON_Delete(parent);
    uint8_t wav[44];s_turn.note=wav;assert(open_note());
    strcat(header,MUSE_HATCH_NOTE_TAIL);cJSON *body=cJSON_Parse(header);assert(body);
    assert(!strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(body,"session_id")),side));
    assert(!strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(body,"output_modality")),"text"));cJSON_Delete(body);
    for (int chat=0;chat<2;++chat) for(int wearable=0;wearable<2;++wearable) {
        sumi_chat_select(chat);sumi_chat_set_wearable(wearable);s_turn.wearable=sumi_chat_wearable();
        sumi_chat_id(s_turn.session_id);
        assert(open_note());strcat(header,MUSE_HATCH_NOTE_TAIL);body=cJSON_Parse(header);assert(body);
        assert((cJSON_GetObjectItem(body,"session_id")!=nullptr)==(chat!=0));
        assert((cJSON_GetObjectItem(body,"device")!=nullptr)==(wearable!=0));
        if(wearable)assert(!strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(body,"device")),"mcu-wearable"));
        cJSON_Delete(body);
        /* Settings cannot change a turn already in progress. */
        sumi_chat_set_wearable(!wearable);
        send_chat("M5Stackからの会話テスト", "text");body=cJSON_Parse(header);assert(body);
        assert((cJSON_GetObjectItem(body,"session_id")!=nullptr)==(chat!=0));
        assert((cJSON_GetObjectItem(body,"device")!=nullptr)==(wearable!=0));
        assert(!strcmp(cJSON_GetStringValue(cJSON_GetObjectItem(body,"message")),"M5Stackからの会話テスト"));
        cJSON_Delete(body);
    }
    // A refresh can reorder rows, while selection stays pinned to the UUID.
    const uint8_t catalog[] = "\1\2" "11111111-1111-4111-8111-111111111111" "\4Test"
                                      SUMI_CHAT_LEGACY_ID "\6Legacy";
    assert(sumi_chat_selected()==1);
    assert(sumi_chat_sync(catalog,sizeof(catalog)-1));
    assert(sumi_chat_count()==3 && sumi_chat_selected()==2);
    char id[SUMI_CHAT_ID_BYTES+1];
    sumi_chat_id(id);assert(!strcmp(id,SUMI_CHAT_LEGACY_ID));
    assert(!sumi_chat_sync(catalog,sizeof(catalog)-2) && sumi_chat_selected()==2);
    assert(sumi_chat_select(1));sumi_chat_id(id);
    assert(!strcmp(id,"11111111-1111-4111-8111-111111111111"));
    const uint8_t empty[]={1,0};
    assert(sumi_chat_sync(empty,sizeof(empty)));
    assert(sumi_chat_count()==1 && sumi_chat_selected()==0);
    sumi_chat_id(id);assert(!id[0]);
}
'''
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / 'run.cpp').write_text(code)
            json = SDK / 'managed_components/espressif__cjson/cJSON'
            obj = p / 'json.o'
            subprocess.run(['cc', '-c', str(json/'cJSON.c'), '-o', str(obj)], check=True)
            chat = p / 'chat.o'
            subprocess.run(['cc', '-std=c11', '-c', str(Path(__file__).resolve().parents[1]/'overlay/sumi_chat.c'), '-o', str(chat)], check=True)
            result = subprocess.run(['c++','-std=c++17','-fsanitize=address,undefined',
                '-I',str(SDK/'tests'),'-I',str(SDK/'components/muse'),'-I',str(json),
                '-I',str(SDK/'components/minimp3/include'), str(p/'run.cpp'),str(obj),str(chat),'-o',str(p/'run')], capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            result = subprocess.run([str(p/'run')],capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
