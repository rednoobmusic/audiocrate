/* furnace_wrap.h — pure C API wrapping DivEngine for use via ctypes */
#pragma once
#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>

/* Opaque handle */
typedef void* FurnaceHandle;

/* Metadata filled by fw_load() */
typedef struct FurnaceInfo {
    char    title[256];
    char    author[256];
    char    system[256];       /* human-readable chip list, comma-separated */
    int     channel_count;
    int     order_len;
    int     pattern_len;
    double  hz;
    int64_t duration_us;       /* total song duration in microseconds (1 loop) */
    int     subsong_count;     /* number of sub-songs (usually 1) */
} FurnaceInfo;

/* Per-channel name (call fw_channel_name(h, i) for each i < info.channel_count) */
const char* fw_channel_name(FurnaceHandle h, int ch);

/* Create / destroy */
FurnaceHandle fw_create(void);
void          fw_destroy(FurnaceHandle h);

/* Read metadata using a global singleton engine (mutex-protected).
   Safe to call from multiple threads. No handle needed. Returns 0 on success. */
int  fw_read_meta_path(const char* path, FurnaceInfo* out_info,
                       char* ch_names_out, int ch_names_stride, int ch_names_max);

/* Render the full song (loops passes) to a WAV file using the same global
   singleton engine as fw_read_meta_path (mutex-protected, never concurrent
   with the play handle).  out_wav must be a writable path (e.g. temp file).
   Returns 0 on success, -1 on error.
   cancel_flag: if non-NULL, rendering stops when *cancel_flag becomes non-zero. */
/* Streaming render on the singleton engine — start playing on the first chunk
   instead of waiting for a whole-song render. Caller must serialise
   load/read/stop against each other and against fw_render_wav_path. */
int  fw_stream_load(const char* path, int rate, int loops, int64_t* out_duration_us);
int  fw_stream_read(int16_t* out, int n_frames);
void fw_stream_stop(void);

int  fw_render_wav_path(const char* path, const char* out_wav, int loops,
                        volatile int* cancel_flag);

/* Read metadata only (no dispatch/audio init — fast, safe to use from any thread).
   Returns 0 on success, -1 on error. */
int  fw_read_meta(FurnaceHandle h, const char* path, FurnaceInfo* out_info);

/* Load a .fur file fully (inits dispatch, needed before play/render).
   Returns 0 on success, -1 on error. */
int  fw_load(FurnaceHandle h, const char* path, FurnaceInfo* out_info);
/* Optional: set desired engine sample rate before first fw_load/init.
   Useful to match output device rate and avoid extra resampling. */
void fw_set_sample_rate(FurnaceHandle h, int sample_rate);

/* Playback (loops = number of times to play through; use 2 for "2 loops") */
void fw_play(FurnaceHandle h, int loops);
void fw_stop(FurnaceHandle h);
int  fw_is_playing(FurnaceHandle h);

/* Select a sub-song (0-based).  Must be called after fw_load, before fw_play.
   Re-fills *out_info if non-NULL. Returns 0 on success. */
int fw_select_subsong(FurnaceHandle h, int subsong, FurnaceInfo* out_info);

/* Seek to the start of a given pattern order (instant, row 0). */
void fw_seek_order(FurnaceHandle h, int order);

/* Seek to a specific order and row. */
void fw_seek_order_row(FurnaceHandle h, int order, int row);

/* Position query */
int fw_current_order(FurnaceHandle h);
int fw_current_row(FurnaceHandle h);

/* Convert order+row → microseconds into the song (uses calcSongTimestamps). */
int64_t fw_order_row_to_us(FurnaceHandle h, int order, int row);

/* Render n_frames stereo float samples into out[] (interleaved L,R).
   Returns frames actually written (may be less if song finished). */
int fw_render(FurnaceHandle h, float* out, int n_frames);

/* Per-channel VU (0..1 peak, with decay) */
float fw_channel_vu(FurnaceHandle h, int ch);
/* Channel visibility flags from module/subsong metadata.
   Returns 1 if visible in song, 0 if hidden/unavailable. */
int fw_channel_visible(FurnaceHandle h, int ch);

/* Per-channel oscilloscope sample window (real dispatch osc buffer).
   Returns number of float samples written to out[], 0 on unavailable/error.
   Samples are normalized to [-1, +1], newest sample last. */
int fw_channel_scope(FurnaceHandle h, int ch, float* out, int max_samples);

/* Actual sample rate the engine is rendering at (after fw_load).
   Use this to configure QAudioSink / WAV output. */
int fw_sample_rate(FurnaceHandle h);

#ifdef __cplusplus
}
#endif
