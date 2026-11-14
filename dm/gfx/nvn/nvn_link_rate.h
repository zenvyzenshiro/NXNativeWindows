// Display-link callback cadence observer. This is deliberately independent of the
// requested CAMetalDisplayLink range: only callback timestamps can establish 120 Hz.
#pragma once
#include <stddef.h>

#define NVN_LINK_RATE_WINDOW 8u

// TOTK's 3-tick hold is 40 fps in its 120 Hz game-timebase. Request the
// 120 Hz display-link mode both for the serious-thermal 40 fps cap and when
// the adaptive controller independently selects that hold. Explicit DM_FPS
// pins and every non-TOTK profile retain their existing link mode.
static inline int nvn_link_rate_gate_adaptive(int is_totk, int cap60,
                                               int fps_pinned, int ready120,
                                               int *pending, int hold) {
    if (!pending) return hold;
    if (!is_totk || !cap60) {
        *pending = 0;
        return hold;
    }
    if (fps_pinned) {
        *pending = 0;
        return hold == 3 ? 4 : hold;  // keep the pinned link mode; never run unverified 40 fps
    }
    // If the adaptive controller asks for a 3-tick hold before callback proof,
    // run a safe 4-tick (30 fps) step while 120 Hz is requested and measured.
    // Keep the request latched until the controller returns to its 2-tick floor.
    if (hold == 3 && !ready120) *pending = 1;
    if (hold <= 2) *pending = 0;
    if (*pending && !ready120 && hold == 3) return 4;
    return hold;
}

static inline int nvn_link_rate_wants_120(int is_totk, int has_fps_state,
                                          int fps_pinned, int requested_cap,
                                          int adaptive_hold, int adaptive_pending) {
    return is_totk && has_fps_state && !fps_pinned &&
           (requested_cap == 40 || (requested_cap == 60 &&
            (adaptive_hold == 3 || adaptive_pending)));
}

typedef struct {
    double previous;
    double interval[NVN_LINK_RATE_WINDOW];
    unsigned count, next;
    int started, observed_hz, ready120;
} nvn_link_rate_state;

static inline void nvn_link_rate_reset(nvn_link_rate_state *s) {
    *s = (nvn_link_rate_state){0};
}

static inline double nvn_link_rate_median(const double *v, unsigned n) {
    double a[NVN_LINK_RATE_WINDOW];
    for (unsigned i = 0; i < n; i++) {
        double x = v[i];
        unsigned j = i;
        while (j && a[j - 1] > x) { a[j] = a[j - 1]; j--; }
        a[j] = x;
    }
    if (!n) return 0;
    return n & 1u ? a[n / 2] : (a[n / 2 - 1] + a[n / 2]) * 0.5;
}

// Feed one monotonic callback timestamp in seconds. A 120 Hz result requires
// at least seven of the last eight intervals to be 6.5–11 ms, a median of
// 8.1–8.6 ms, and a rounded median rate of 119–121 Hz. One isolated missed
// callback therefore does not revoke a stable 120 Hz link; slower stable rates
// and sustained 60/90/100/110/117/118 Hz cadences cannot pass.
static inline void nvn_link_rate_sample(nvn_link_rate_state *s, double now) {
    if (!s->started) {
        s->previous = now;
        s->started = 1;
        return;
    }

    double dt = now - s->previous;
    s->previous = now;
    if (!(dt > 0.0 && dt <= 0.25)) {
        s->count = s->next = 0;
        s->observed_hz = s->ready120 = 0;
        return;
    }

    s->interval[s->next] = dt;
    s->next = (s->next + 1u) % NVN_LINK_RATE_WINDOW;
    if (s->count < NVN_LINK_RATE_WINDOW) s->count++;

    double median = nvn_link_rate_median(s->interval, s->count);
    s->observed_hz = median > 0.0 ? (int)(1.0 / median + 0.5) : 0;

    unsigned near120 = 0;
    for (unsigned i = 0; i < s->count; i++)
        if (s->interval[i] >= 0.0065 && s->interval[i] <= 0.0110) near120++;

    s->ready120 = s->count == NVN_LINK_RATE_WINDOW &&
                  near120 >= NVN_LINK_RATE_WINDOW - 1u &&
                  s->observed_hz >= 119 && s->observed_hz <= 121 &&
                  median >= 0.0081 && median <= 0.0086;
}
