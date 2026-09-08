/* furnace_wrap.cpp — thin C shim over DivEngine */
#include "furnace_wrap.h"

#include <engine/engine.h>
#include <engine/dispatch.h>

#include <algorithm>
#include <atomic>
#include <cstring>
#include <cstdio>
#include <cmath>
#include <memory>
#include <mutex>
#include <string>

void reportError(String what) {
    fprintf(stderr, "furnace: %s\n", what.c_str());
}

static constexpr int kChunk = 512;

struct FurnaceCtx {
    DivEngine engine;
    bool      loaded      = false;
    bool      dispatched  = false;
    bool      inited      = false;
    int       vuCount     = 0;
    std::unique_ptr<std::atomic<float>[]> vu;

    /* Scratch buffers for nextBuf */
    float outL[kChunk];
    float outR[kChunk];
    float limiterGain = 1.0f;

    FurnaceCtx() {
        engine.prePreInit();
        /* same tracker-config quarantine as _get_meta_engine() */
        engine.setConf("renderPoolThreads", 0);
        engine.setConf("midiInDevice", "");
        engine.setConf("midiOutDevice", "");
        engine.preInit(true);
    }
    ~FurnaceCtx() {
        if (dispatched) {
            engine.stop();
            engine.quitDispatch();
        }
    }
};

/* ── create / destroy ──────────────────────────────────────────────────────── */

FurnaceHandle fw_create(void) {
    return new FurnaceCtx;
}

void fw_destroy(FurnaceHandle h) {
    delete static_cast<FurnaceCtx*>(h);
}

/* ── helpers ───────────────────────────────────────────────────────────────── */

static bool _read_file(const char* path, unsigned char** out_buf, long* out_sz) {
    FILE* f = fopen(path, "rb");
    if (!f) return false;
    fseek(f, 0, SEEK_END);
    long sz = ftell(f);
    rewind(f);
    if (sz <= 0) { fclose(f); return false; }
    *out_buf = new unsigned char[sz];
    if (fread(*out_buf, 1, sz, f) != (size_t)sz) {
        delete[] *out_buf; fclose(f); return false;
    }
    fclose(f);
    *out_sz = sz;
    return true;
}

static void _fill_info(FurnaceCtx* ctx, FurnaceInfo* info, const char* path) {
    memset(info, 0, sizeof(*info));
    const DivSong& song = ctx->engine.song;
    const std::string& name = song.name;
    strncpy(info->title, name.empty() ? "" : name.c_str(), 255);
    strncpy(info->author, song.author.c_str(), 255);
    std::string chips;
    for (int i = 0; i < song.systemLen; i++) {
        if (i) chips += ", ";
        const char* n = ctx->engine.getSystemName(song.system[i]);
        chips += (n ? n : "?");
    }
    strncpy(info->system, chips.c_str(), 255);
    info->channel_count = ctx->engine.getTotalChannelCount();
    info->subsong_count = (int)song.subsong.size();
    DivSubSong* ss = (ctx->engine.curSubSong != nullptr)
                     ? ctx->engine.curSubSong
                     : (!song.subsong.empty() ? song.subsong[0] : nullptr);
    if (ss) {
        info->order_len   = ss->ordersLen;
        info->pattern_len = ss->patLen;
        info->hz          = ss->hz;
        const TimeMicros& dur = ss->ts.totalTime;
        info->duration_us = (int64_t)dur.seconds * 1000000LL + dur.micros;
    }
}

/* ── global singleton for thread-safe metadata reads ───────────────────────── */

static std::mutex   g_meta_mutex;
static DivEngine*   g_meta_engine  = nullptr;

static bool g_meta_inited = false;

/* True while fw_stream_* owns the singleton engine. Anything else that would
   load a different module into it must refuse rather than corrupt playback. */
static bool g_stream_active = false;

static DivEngine* _get_meta_engine() {
    if (!g_meta_engine) {
        g_meta_engine = new DivEngine;
        g_meta_engine->prePreInit();
        /* prePreInit() loads the USER'S Furnace tracker config
         * (~/.config/furnace/furnace.cfg). On machines where the real tracker
         * is installed that can enable renderPoolThreads=N — internal worker
         * threads that keep running between wrapper calls. Cancelling/reloading
         * then races those workers and dies with "free(): invalid pointer".
         * This is an offline renderer: force single-threaded rendering and no
         * MIDI listener, regardless of what the tracker config says. */
        g_meta_engine->setConf("renderPoolThreads", 0);
        g_meta_engine->setConf("midiInDevice", "");
        g_meta_engine->setConf("midiOutDevice", "");
        g_meta_engine->preInit(true);
    }
    return g_meta_engine;
}

static void _ensure_meta_dispatched(DivEngine* eng) {
    if (!g_meta_inited) {
        eng->setAudio(DIV_AUDIO_DUMMY);
        eng->init();
        g_meta_inited = true;
    }
}

int fw_read_meta_path(const char* path, FurnaceInfo* out_info,
                      char* ch_names_out, int ch_names_stride, int ch_names_max) {
    std::lock_guard<std::mutex> lock(g_meta_mutex);
    /* Reading tags means loading that module into the engine — which would cut
       a stream that is currently playing out of it. Refuse instead; the caller
       caches tags and falls back to the filename. */
    if (g_stream_active) return -1;
    DivEngine* eng = _get_meta_engine();

    unsigned char* buf = nullptr;
    long sz = 0;
    if (!_read_file(path, &buf, &sz)) {
        fprintf(stderr, "fw_read_meta_path: cannot open %s\n", path);
        return -1;
    }

    if (!eng->load(buf, (size_t)sz, path)) {
        fprintf(stderr, "fw_read_meta_path: parse failed: %s\n", path);
        return -1;
    }

    eng->calcSongTimestamps();

    if (out_info) {
        memset(out_info, 0, sizeof(*out_info));
        const DivSong& song = eng->song;
        strncpy(out_info->title,  song.name.c_str(),   255);
        strncpy(out_info->author, song.author.c_str(), 255);
        std::string chips;
        for (int i = 0; i < song.systemLen; i++) {
            if (i) chips += ", ";
            const char* n = eng->getSystemName(song.system[i]);
            chips += (n ? n : "?");
        }
        strncpy(out_info->system, chips.c_str(), 255);
        out_info->channel_count  = eng->getTotalChannelCount();
        out_info->subsong_count  = (int)song.subsong.size();
        DivSubSong* ss = (eng->curSubSong != nullptr)
                         ? eng->curSubSong
                         : (!song.subsong.empty() ? song.subsong[0] : nullptr);
        if (ss) {
            out_info->order_len   = ss->ordersLen;
            out_info->pattern_len = ss->patLen;
            out_info->hz          = ss->hz;
            const TimeMicros& dur = ss->ts.totalTime;
            out_info->duration_us = (int64_t)dur.seconds * 1000000LL + dur.micros;
        }
    }

    if (ch_names_out && ch_names_stride > 0 && ch_names_max > 0) {
        int n = std::min(ch_names_max, eng->getTotalChannelCount());
        for (int i = 0; i < n; i++) {
            const char* cn = eng->getChannelName(i);
            char* slot = ch_names_out + i * ch_names_stride;
            strncpy(slot, (cn && cn[0]) ? cn : "?", (size_t)ch_names_stride - 1);
            slot[ch_names_stride - 1] = '\0';
        }
    }

    return 0;
}

/* ── render full song to WAV (global singleton engine) ─────────────────────── */

/* Minimal WAV header writer — 16-bit stereo PCM */
static void _write_wav_header(FILE* f, int sample_rate, int n_frames) {
    int byte_rate   = sample_rate * 2 * 2;
    int data_bytes  = n_frames * 4;            /* 2 channels × 2 bytes */
    int riff_size   = 36 + data_bytes;
    auto wr16 = [&](int v){ uint16_t u = (uint16_t)v; fwrite(&u,2,1,f); };
    auto wr32 = [&](int v){ uint32_t u = (uint32_t)v; fwrite(&u,4,1,f); };
    fwrite("RIFF",4,1,f); wr32(riff_size);
    fwrite("WAVE",4,1,f);
    fwrite("fmt ",4,1,f); wr32(16); wr16(1); wr16(2);
    wr32(sample_rate); wr32(byte_rate); wr16(4); wr16(16);
    fwrite("data",4,1,f); wr32(data_bytes);
}

/* ── streaming render on the singleton engine ──────────────────────────────
 * fw_render_wav_path has to finish the whole module before a note can play,
 * which is seconds for a long tune. These pull it a chunk at a time instead,
 * so playback can start on the first chunk and render the rest underneath.
 *
 * They deliberately reuse the SAME singleton engine as the metadata/WAV paths.
 * DivEngine keeps its chip-definition table in a static array but registers it
 * per instance (and memsets it in the constructor), so a second DivEngine
 * anywhere in the process corrupts the first and crashes with
 * "free(): invalid pointer". One engine, always.
 *
 * The caller must hold a lock across load/read/stop (Python does, via
 * native_lock.RENDER_LOCK) — g_meta_mutex only guards individual calls.
 */
int fw_stream_load(const char* path, int rate, int loops, int64_t* out_duration_us) {
    std::lock_guard<std::mutex> lock(g_meta_mutex);
    DivEngine* eng = _get_meta_engine();

    if (g_stream_active) {          /* retire a previous stream first */
        eng->stop();
        eng->quitDispatch();
        g_stream_active = false;
    }

    unsigned char* buf = nullptr;
    long sz = 0;
    if (!_read_file(path, &buf, &sz)) return -1;
    if (!eng->load(buf, (size_t)sz, path)) return -1;   /* load() owns buf */

    if (rate >= 8000 && rate <= 384000 && !g_meta_inited) {
        eng->setConf("audioRate", rate);
    }
    _ensure_meta_dispatched(eng);
    eng->initDispatch();
    eng->renderSamples();
    eng->calcSongTimestamps();

    if (out_duration_us) {
        const DivSong& song = eng->song;
        DivSubSong* ss = (eng->curSubSong != nullptr)
                         ? eng->curSubSong
                         : (!song.subsong.empty() ? song.subsong[0] : nullptr);
        *out_duration_us = 0;
        if (ss) {
            const TimeMicros& dur = ss->ts.totalTime;
            *out_duration_us = (int64_t)dur.seconds * 1000000LL + dur.micros;
        }
    }
    eng->setLoops(loops);
    eng->play();
    g_stream_active = true;
    return 0;
}

/* Pull up to n_frames of interleaved stereo int16. Returns frames written,
 * 0 at end of song, -1 if no stream is open. */
int fw_stream_read(int16_t* out, int n_frames) {
    std::lock_guard<std::mutex> lock(g_meta_mutex);
    if (!g_stream_active || !out || n_frames <= 0) return -1;
    DivEngine* eng = _get_meta_engine();
    if (!eng->isPlaying()) return 0;

    static constexpr int kW = 1024;
    float outL[kW], outR[kW];
    float* bufs[2] = { outL, outR };

    int done = 0;
    while (done < n_frames && eng->isPlaying()) {
        int chunk = std::min(n_frames - done, kW);
        eng->nextBuf(nullptr, bufs, 0, 2, chunk);
        for (int i = 0; i < chunk; i++) {
            out[(done + i) * 2]     = (int16_t)std::clamp((int)(outL[i] * 32767), -32768, 32767);
            out[(done + i) * 2 + 1] = (int16_t)std::clamp((int)(outR[i] * 32767), -32768, 32767);
        }
        done += chunk;
    }
    return done;
}

void fw_stream_stop(void) {
    std::lock_guard<std::mutex> lock(g_meta_mutex);
    if (!g_stream_active) return;
    DivEngine* eng = _get_meta_engine();
    eng->stop();
    eng->quitDispatch();
    g_stream_active = false;
}

int fw_render_wav_path(const char* path, const char* out_wav, int loops,
                       volatile int* cancel_flag) {
    std::lock_guard<std::mutex> lock(g_meta_mutex);
    /* Same reason as fw_read_meta_path: this reloads the shared engine. */
    if (g_stream_active) return -1;
    DivEngine* eng = _get_meta_engine();

    unsigned char* buf = nullptr;
    long sz = 0;
    if (!_read_file(path, &buf, &sz)) {
        fprintf(stderr, "fw_render_wav_path: cannot open %s\n", path);
        return -1;
    }
    if (!eng->load(buf, (size_t)sz, path)) {
        fprintf(stderr, "fw_render_wav_path: parse failed: %s\n", path);
        return -1;
    }

    _ensure_meta_dispatched(eng);
    eng->initDispatch();
    eng->renderSamples();
    eng->calcSongTimestamps();

    const DivSong& song = eng->song;
    DivSubSong* ss = eng->curSubSong ? eng->curSubSong
                     : (!song.subsong.empty() ? song.subsong[0] : nullptr);
    if (!ss) { eng->quitDispatch(); return -1; }

    int rate = eng->getAudioDescGot().rate;
    if (rate <= 0) rate = 44100;
    const TimeMicros& dur = ss->ts.totalTime;
    int64_t dur_us = (int64_t)dur.seconds * 1000000LL + dur.micros;
    int total_frames = (int)(dur_us / 1000000.0 * rate) * loops;

    FILE* out = fopen(out_wav, "wb");
    if (!out) { eng->quitDispatch(); return -1; }

    /* Reserve space for header, fill later */
    _write_wav_header(out, rate, 0);

    static constexpr int kW = 1024;
    float outL[kW], outR[kW];
    float* bufs[2] = { outL, outR };
    int16_t pcm[kW * 2];

    eng->setLoops(loops);
    eng->play();

    int written = 0;
    int remaining = total_frames;
    while (remaining > 0 && eng->isPlaying()) {
        if (cancel_flag && *cancel_flag) break;
        int chunk = std::min(remaining, kW);
        eng->nextBuf(nullptr, bufs, 0, 2, chunk);
        for (int i = 0; i < chunk; i++) {
            pcm[i*2]   = (int16_t)std::clamp((int)(outL[i] * 32767), -32768, 32767);
            pcm[i*2+1] = (int16_t)std::clamp((int)(outR[i] * 32767), -32768, 32767);
        }
        fwrite(pcm, 4, chunk, out);
        written += chunk;
        remaining -= chunk;
    }

    eng->stop();
    eng->quitDispatch();

    /* Patch WAV header with real frame count */
    rewind(out);
    _write_wav_header(out, rate, written);
    fclose(out);
    return (cancel_flag && *cancel_flag) ? -1 : 0;
}

/* ── read meta (no dispatch) ───────────────────────────────────────────────── */

int fw_read_meta(FurnaceHandle h, const char* path, FurnaceInfo* info) {
    auto* ctx = static_cast<FurnaceCtx*>(h);

    /* Tear down any previous full load */
    if (ctx->dispatched) {
        ctx->engine.stop();
        ctx->engine.quitDispatch();
        ctx->dispatched = false;
    }
    ctx->loaded = false;

    unsigned char* buf = nullptr;
    long sz = 0;
    if (!_read_file(path, &buf, &sz)) {
        fprintf(stderr, "fw_read_meta: cannot open %s\n", path);
        return -1;
    }

    /* load() parses the song but doesn't touch audio */
    if (!ctx->engine.load(buf, (size_t)sz, path)) {
        fprintf(stderr, "fw_read_meta: parse failed: %s\n", path);
        return -1;
    }

    /* calcSongTimestamps uses only the subsong data — no dispatch needed */
    ctx->engine.calcSongTimestamps();

    if (info) _fill_info(ctx, info, path);
    return 0;
    /* Intentionally do NOT set ctx->inited or ctx->loaded —
       a subsequent fw_load() must still do the full init sequence. */
}

/* ── full load (dispatch + render) ─────────────────────────────────────────── */

int fw_load(FurnaceHandle h, const char* path, FurnaceInfo* info) {
    auto* ctx = static_cast<FurnaceCtx*>(h);

    if (ctx->dispatched) {
        ctx->engine.stop();
        ctx->engine.quitDispatch();
        ctx->dispatched = false;
    }
    ctx->loaded = false;

    unsigned char* buf = nullptr;
    long sz = 0;
    if (!_read_file(path, &buf, &sz)) {
        fprintf(stderr, "fw_load: cannot open %s\n", path);
        return -1;
    }

    if (!ctx->engine.load(buf, (size_t)sz, path)) {
        fprintf(stderr, "fw_load: parse failed: %s\n", path);
        return -1;
    }

    if (!ctx->inited) {
        // Force deterministic high-quality render settings for wrapper usage.
        // Do this before init(), since engine snapshots config there.
        ctx->engine.setConf("audioQuality", 0); // 0 = high quality
        ctx->engine.setConf("audioHiPass", 0);  // disable extra hi-pass coloration
        ctx->engine.setConf("audioChans", 2);
        // Use dummy audio backend — we pull samples manually via nextBuf()
        ctx->engine.setAudio(DIV_AUDIO_DUMMY);
        ctx->engine.init();
        ctx->inited = true;
    }
    ctx->engine.initDispatch();
    ctx->engine.renderSamples();
    ctx->dispatched = true;
    ctx->loaded     = true;
    ctx->limiterGain = 1.0f;

    ctx->engine.calcSongTimestamps();
    if (info) _fill_info(ctx, info, path);
    return 0;
}

void fw_set_sample_rate(FurnaceHandle h, int sample_rate) {
    auto* ctx = static_cast<FurnaceCtx*>(h);
    if (!ctx) return;
    if (sample_rate < 8000 || sample_rate > 384000) return;
    // Must be configured before init() to take effect cleanly.
    if (ctx->inited) return;
    ctx->engine.setConf("audioRate", sample_rate);
}

/* ── subsong select ────────────────────────────────────────────────────────── */

int fw_select_subsong(FurnaceHandle h, int subsong, FurnaceInfo* out_info) {
    auto* ctx = static_cast<FurnaceCtx*>(h);
    if (!ctx->loaded) return -1;
    const DivSong& song = ctx->engine.song;
    if (subsong < 0 || subsong >= (int)song.subsong.size()) return -1;
    ctx->engine.curSubSong = song.subsong[subsong];
    ctx->engine.calcSongTimestamps();
    if (out_info) _fill_info(ctx, out_info, "");
    return 0;
}

/* ── channel name ──────────────────────────────────────────────────────────── */

const char* fw_channel_name(FurnaceHandle h, int ch) {
    auto* ctx = static_cast<FurnaceCtx*>(h);
    const char* n = ctx->engine.getChannelName(ch);
    return (n && n[0]) ? n : "?";
}

/* ── playback ──────────────────────────────────────────────────────────────── */

void fw_play(FurnaceHandle h, int loops) {
    auto* ctx = static_cast<FurnaceCtx*>(h);
    if (!ctx->loaded) return;

    ctx->vuCount = ctx->engine.getTotalChannelCount();
    ctx->vu = std::make_unique<std::atomic<float>[]>(ctx->vuCount);
    for (int i = 0; i < ctx->vuCount; i++) ctx->vu[i].store(0.0f);
    ctx->limiterGain = 1.0f;

    ctx->engine.setLoops(loops);
    ctx->engine.play();
}

void fw_stop(FurnaceHandle h) {
    auto* ctx = static_cast<FurnaceCtx*>(h);
    if (!ctx->dispatched) return;
    ctx->engine.stop();
}

int fw_is_playing(FurnaceHandle h) {
    auto* ctx = static_cast<FurnaceCtx*>(h);
    if (!ctx->dispatched) return 0;
    return ctx->engine.isPlaying() ? 1 : 0;
}

/* ── seek ──────────────────────────────────────────────────────────────────── */

void fw_seek_order(FurnaceHandle h, int order) {
    auto* ctx = static_cast<FurnaceCtx*>(h);
    if (!ctx->loaded) return;
    ctx->engine.setOrder((unsigned char)order);
}

void fw_seek_order_row(FurnaceHandle h, int order, int row) {
    auto* ctx = static_cast<FurnaceCtx*>(h);
    if (!ctx->loaded) return;
    ctx->engine.setOrder((unsigned char)order);
    (void)row;
}

/* ── position ──────────────────────────────────────────────────────────────── */

int fw_current_order(FurnaceHandle h) {
    auto* ctx = static_cast<FurnaceCtx*>(h);
    return (int)ctx->engine.getOrder();
}

int fw_current_row(FurnaceHandle h) {
    auto* ctx = static_cast<FurnaceCtx*>(h);
    return ctx->engine.getRow();
}

int64_t fw_order_row_to_us(FurnaceHandle h, int order, int row) {
    auto* ctx = static_cast<FurnaceCtx*>(h);
    if (!ctx->loaded) return 0;
    const DivSong& song = ctx->engine.song;
    if (song.subsong.empty() || !song.subsong[0]) return 0;
    TimeMicros t = song.subsong[0]->ts.getTimes(order, row);
    if (t.seconds < 0) return 0;
    return (int64_t)t.seconds * 1000000LL + t.micros;
}

/* ── render ────────────────────────────────────────────────────────────────── */

int fw_render(FurnaceHandle h, float* out, int n_frames) {
    auto* ctx = static_cast<FurnaceCtx*>(h);
    if (!ctx->engine.isPlaying()) return 0;

    float* bufs[2] = { ctx->outL, ctx->outR };
    int done = 0;
    while (done < n_frames) {
        int chunk = std::min(n_frames - done, kChunk);
        ctx->engine.nextBuf(nullptr, bufs, 0, 2, chunk);

        /* Update VU */
        for (int ch = 0; ch < ctx->vuCount; ch++) {
            DivDispatchOscBuffer* osc = ctx->engine.getOscBuffer(ch);
            float peak = 0.0f;
            if (osc) {
                const unsigned short pos = (unsigned short)(osc->needle >> OSCBUF_PREC);
                for (int i = 0; i < std::min(512, chunk); i++) {
                    float s = osc->data[(unsigned short)(pos - i)] / 32768.0f;
                    peak = std::max(peak, std::abs(s));
                }
            }
            float prev = ctx->vu[ch].load();
            ctx->vu[ch].store(std::max(peak, prev * 0.80f));
        }

        // Transparent auto-limiter to avoid hard clipping on hot modules.
        float chunkPeak = 0.0f;
        for (int i = 0; i < chunk; i++) {
            chunkPeak = std::max(chunkPeak, std::abs(ctx->outL[i]));
            chunkPeak = std::max(chunkPeak, std::abs(ctx->outR[i]));
        }
        float targetGain = 1.0f;
        const float ceiling = 0.985f;
        if (chunkPeak > ceiling) {
            targetGain = ceiling / chunkPeak;
        }
        // Fast attack, slow release.
        if (targetGain < ctx->limiterGain) {
            ctx->limiterGain = targetGain;
        } else {
            ctx->limiterGain = ctx->limiterGain * 0.995f + targetGain * 0.005f;
            if (ctx->limiterGain > 1.0f) ctx->limiterGain = 1.0f;
        }

        /* Interleave into out[] */
        for (int i = 0; i < chunk; i++) {
            out[(done + i) * 2]     = std::clamp(ctx->outL[i] * ctx->limiterGain, -1.0f, 1.0f);
            out[(done + i) * 2 + 1] = std::clamp(ctx->outR[i] * ctx->limiterGain, -1.0f, 1.0f);
        }
        done += chunk;

        if (!ctx->engine.isPlaying()) break;
    }
    return done;
}

/* ── VU ────────────────────────────────────────────────────────────────────── */

float fw_channel_vu(FurnaceHandle h, int ch) {
    auto* ctx = static_cast<FurnaceCtx*>(h);
    if (!ctx->vu || ch < 0 || ch >= ctx->vuCount) return 0.0f;
    return ctx->vu[ch].load();
}

int fw_channel_visible(FurnaceHandle h, int ch) {
    auto* ctx = static_cast<FurnaceCtx*>(h);
    if (!ctx || !ctx->loaded || ch < 0 || ch >= ctx->engine.getTotalChannelCount()) return 0;
    if (!ctx->engine.curSubSong) return 1;
    return ctx->engine.curSubSong->chanShowChanOsc[ch] ? 1 : 0;
}

int fw_channel_scope(FurnaceHandle h, int ch, float* out, int max_samples) {
    auto* ctx = static_cast<FurnaceCtx*>(h);
    if (!ctx || !out || max_samples <= 0) return 0;
    if (!ctx->dispatched || ch < 0 || ch >= ctx->engine.getTotalChannelCount()) return 0;

    DivDispatchOscBuffer* osc = ctx->engine.getOscBuffer(ch);
    if (!osc) return 0;

    int count = max_samples;
    if (count > 32768) count = 32768;

    const unsigned short pos = (unsigned short)(osc->needle >> OSCBUF_PREC);
    // Prime with a nearby valid sample so leading invalid markers don't create
    // fake spikes/dropouts at window edges.
    short last = 0;
    for (int b = 0; b < 1024; b++) {
        const unsigned short bi = (unsigned short)(pos - b);
        short s = osc->data[bi];
        if (s != (short)-1) {
            last = s;
            break;
        }
    }
    for (int i = 0; i < count; i++) {
        const unsigned short idx = (unsigned short)(pos - (count - 1 - i));
        short s = osc->data[idx];
        // Furnace osc buffers can contain -1 marker entries; treat them as
        // "no new sample" and hold the last valid one to avoid gritty spikes.
        if (s == (short)-1) {
            s = last;
        } else {
            last = s;
        }
        out[i] = std::clamp((float)s / 32768.0f, -1.0f, 1.0f);
    }
    return count;
}

int fw_sample_rate(FurnaceHandle h) {
    auto* ctx = static_cast<FurnaceCtx*>(h);
    int r = ctx->engine.getAudioDescGot().rate;
    return (r > 0) ? r : 44100;
}
