/* sid_wrap.cpp — pure C API around libsidplayfp, for use from Python via ctypes.
 *
 * Mirrors the approach used for Furnace (_furnace_wrapper_src): libsidplayfp is
 * C++, so this exposes a flat C surface that ctypes can call.
 *
 * Provides: metadata (title/author/released/subsong count), per-subsong length
 * from an HVSC Songlengths database when one is supplied, and rendering a
 * subsong to interleaved stereo 16-bit PCM.
 */
#include <sidplayfp/sidplayfp.h>
#include <sidplayfp/SidTune.h>
#include <sidplayfp/SidInfo.h>
#include <sidplayfp/SidTuneInfo.h>
#include <sidplayfp/SidDatabase.h>
#include <sidplayfp/builders/residfp.h>

#include <cstring>
#include <cstdint>
#include <string>
#include <new>
#include <vector>

extern "C" {

struct SidMeta {
    char title[64];
    char author[64];
    char released[64];
    int  subsongs;
    int  start_song;     /* 1-based */
    int  is_rsid;
};

/* ---- metadata ---------------------------------------------------------- */
int sw_read_meta(const char* path, SidMeta* out)
{
    if (!path || !out) return -1;
    SidTune tune(path);
    if (!tune.getStatus()) return -1;
    const SidTuneInfo* i = tune.getInfo();
    if (!i) return -1;

    std::memset(out, 0, sizeof(*out));
    auto copy = [](char* dst, const char* src, size_t n) {
        if (!src) return;
        std::strncpy(dst, src, n - 1);
    };
    if (i->numberOfInfoStrings() > 0) copy(out->title,    i->infoString(0), sizeof(out->title));
    if (i->numberOfInfoStrings() > 1) copy(out->author,   i->infoString(1), sizeof(out->author));
    if (i->numberOfInfoStrings() > 2) copy(out->released, i->infoString(2), sizeof(out->released));
    out->subsongs   = (int)i->songs();
    out->start_song = (int)i->startSong();
    out->is_rsid    = (i->compatibility() == SidTuneInfo::COMPATIBILITY_R64) ? 1 : 0;
    return 0;
}

/* ---- HVSC songlength database ------------------------------------------ */
/* Returns length in milliseconds for subsong (1-based), or -1 if unknown. */
int sw_db_length(const char* db_path, const char* sid_path, int subsong)
{
    if (!db_path || !sid_path) return -1;
    SidDatabase db;
    if (db.open(db_path) < 0) return -1;
    SidTune tune(sid_path);
    if (!tune.getStatus()) return -1;
    tune.selectSong((unsigned int)(subsong < 1 ? 1 : subsong));
    const int_least32_t ms = db.lengthMs(tune);
    return ms > 0 ? (int)ms : -1;
}

/* ---- rendering --------------------------------------------------------- */
/* Render `subsong` (1-based) for up to max_ms into caller-allocated `out`
 * (interleaved stereo int16). Returns frames written, or -1 on error. */
int sw_render(const char* path, int subsong, int rate, int max_ms,
              int16_t* out, int out_frames)
{
    if (!path || !out || out_frames <= 0) return -1;

    SidTune tune(path);
    if (!tune.getStatus()) return -1;
    tune.selectSong((unsigned int)(subsong < 1 ? 1 : subsong));

    sidplayfp engine;
    ReSIDfpBuilder builder("residfp");

    SidConfig cfg = engine.config();
    cfg.frequency    = (uint_least32_t)rate;
    cfg.sidEmulation = &builder;
    /* Deterministic power-on state. Left at the default the engine randomises
     * it, so the same subsong rendered twice differs — which made automatic
     * length detection give different answers run to run. */
    cfg.powerOnDelay = 2500;
    if (!engine.config(cfg)) return -1;
    if (!engine.load(&tune)) return -1;

    /* libsidplayfp 3.x: run the CPU for a slice, then mix the produced
     * samples into our buffer. play() returns sample count, not frames. */
    engine.initMixer(true);   /* stereo */

    const int want = (max_ms > 0)
        ? (int)((int64_t)max_ms * rate / 1000)
        : out_frames;
    const int limit = want < out_frames ? want : out_frames;

    const unsigned int CYCLES = 5000;   /* ~5 ms of C64 time per slice */
    int frames = 0;
    while (frames < limit) {
        const int samples = engine.play(CYCLES);
        if (samples < 0) break;             /* emulation error */
        if (samples == 0) continue;
        /* In stereo, mix() writes 2 shorts per input sample — so the number of
         * samples we may pass equals the number of frames still free. */
        const int room = limit - frames;
        const unsigned int n = engine.mix(out + (size_t)frames * 2,
                                          (unsigned int)(samples < room ? samples : room));
        if (n == 0) break;
        frames += (int)(n / 2);
    }
    return frames;
}

/* ---- streaming render ---------------------------------------------------
 * sw_render() has to finish the whole tune before anything can play. Emulation
 * is strictly sequential (there is no seek — reaching 2:00 means emulating
 * everything before it), so the way to start fast is not to skip ahead but to
 * hand back the beginning immediately and keep going underneath.
 *
 * A stream keeps the engine, builder and tune alive between calls so the caller
 * can pull the tune a chunk at a time.
 */
struct SidStream {
    sidplayfp        engine;
    ReSIDfpBuilder   builder;
    SidTune          tune;
    bool             ok;

    SidStream(const char* path, int subsong, int rate)
        : builder("residfp"), tune(path), ok(false)
    {
        if (!tune.getStatus()) return;
        tune.selectSong((unsigned int)(subsong < 1 ? 1 : subsong));
        SidConfig cfg = engine.config();
        cfg.frequency    = (uint_least32_t)rate;
        cfg.sidEmulation = &builder;
        cfg.powerOnDelay = 2500;          /* deterministic, see sw_render */
        if (!engine.config(cfg)) return;
        if (!engine.load(&tune)) return;
        engine.initMixer(true);           /* stereo */
        ok = true;
    }
};

/* Open a streaming render. Returns NULL on failure. */
void* sw_stream_open(const char* path, int subsong, int rate)
{
    if (!path) return nullptr;
    SidStream* s = new (std::nothrow) SidStream(path, subsong, rate);
    if (!s || !s->ok) { delete s; return nullptr; }
    return s;
}

/* Pull up to `frames` stereo frames. Returns frames written (0 = nothing more
 * this call; the caller decides when it has enough). */
int sw_stream_read(void* handle, int16_t* out, int frames)
{
    SidStream* s = static_cast<SidStream*>(handle);
    if (!s || !out || frames <= 0) return -1;
    const unsigned int CYCLES = 5000;
    int done = 0;
    while (done < frames) {
        const int samples = s->engine.play(CYCLES);
        if (samples < 0) break;
        if (samples == 0) continue;
        const int room = frames - done;
        const unsigned int n = s->engine.mix(out + (size_t)done * 2,
                                             (unsigned int)(samples < room ? samples : room));
        if (n == 0) break;
        done += (int)(n / 2);
    }
    return done;
}

void sw_stream_close(void* handle)
{
    delete static_cast<SidStream*>(handle);
}

} /* extern "C" */
