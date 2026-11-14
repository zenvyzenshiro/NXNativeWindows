#include "../nvn/nvn_link_rate.h"
#include <stdio.h>

static int checks, failed;
#define CHECK(label, ok) do { \
    int pass = !!(ok); checks++; failed += !pass; \
    printf("%s: %s\n", (label), pass ? "PASS" : "FAIL"); \
} while (0)

static void feed(nvn_link_rate_state *s, double *t, double period, int n) {
    for (int i = 0; i < n; i++) {
        *t += period;
        nvn_link_rate_sample(s, *t);
    }
}

int main(void) {
    nvn_link_rate_state s = {0};
    double t = 1.0;
    nvn_link_rate_sample(&s, t);
    feed(&s, &t, 1.0 / 120.0, 8);
    CHECK("eight consecutive 120 Hz intervals are recognized", s.ready120);
    CHECK("120 Hz cadence is measured from timestamps", s.observed_hz == 120);

    feed(&s, &t, 1.0 / 60.0, 1);
    CHECK("one isolated missed callback does not revoke stable 120 Hz", s.ready120);
    feed(&s, &t, 1.0 / 60.0, 1);
    CHECK("two slow callbacks revoke 120 Hz verification", !s.ready120);

    nvn_link_rate_reset(&s);
    t = 10.0;
    nvn_link_rate_sample(&s, t);
    feed(&s, &t, 1.0 / 60.0, 8);
    CHECK("a 60 Hz callback stream is measured as 60", s.observed_hz == 60);
    CHECK("a 60 Hz stream cannot pass the 120 Hz gate", !s.ready120);

    nvn_link_rate_reset(&s);
    t = 20.0;
    nvn_link_rate_sample(&s, t);
    feed(&s, &t, 0.010, 8);
    CHECK("a 100 Hz stream is measured correctly", s.observed_hz == 100);
    CHECK("a 100 Hz stream does not pass the 120 Hz gate", !s.ready120);

    nvn_link_rate_reset(&s);
    t = 30.0;
    nvn_link_rate_sample(&s, t);
    feed(&s, &t, 1.0 / 90.0, 8);
    CHECK("a 90 Hz stream is measured correctly", s.observed_hz == 90);
    CHECK("a 90 Hz stream does not pass the 120 Hz gate", !s.ready120);

    nvn_link_rate_reset(&s);
    t = 40.0;
    nvn_link_rate_sample(&s, t);
    feed(&s, &t, 1.0 / 110.0, 8);
    CHECK("a 110 Hz stream is not mistaken for nominal 120 Hz", s.observed_hz == 110 && !s.ready120);

    nvn_link_rate_reset(&s);
    t = 50.0;
    nvn_link_rate_sample(&s, t);
    feed(&s, &t, 1.0 / 118.0, 8);
    CHECK("a near-120 but non-120 cadence does not authorize 40 fps", s.observed_hz == 118 && !s.ready120);

    feed(&s, &t, 1.0, 1);
    CHECK("a long callback silence clears the stale rate estimate", s.observed_hz == 0 && !s.ready120);

    CHECK("serious-thermal 40 fps requests 120 Hz before callback proof",
          nvn_link_rate_wants_120(1, 1, 0, 40, 4, 0));
    CHECK("adaptive 3-tick hold requests 120 Hz at the normal 60 fps cap",
          nvn_link_rate_wants_120(1, 1, 0, 60, 3, 0));
    CHECK("normal 2-tick hold does not request 120 Hz",
          !nvn_link_rate_wants_120(1, 1, 0, 60, 2, 0));
    CHECK("adaptive 4-tick hold does not request 120 Hz",
          !nvn_link_rate_wants_120(1, 1, 0, 60, 4, 0));
    CHECK("a native 30 fps cap does not request the 120 Hz mode",
          !nvn_link_rate_wants_120(1, 1, 0, 30, 3, 0));
    CHECK("an inactive frame-step target does not request the 120 Hz mode",
          !nvn_link_rate_wants_120(1, 1, 0, 0, 3, 0));
    CHECK("non-TOTK profiles never receive the override",
          !nvn_link_rate_wants_120(0, 1, 0, 60, 3, 0));
    CHECK("profiles without frame-step state never receive the override",
          !nvn_link_rate_wants_120(1, 0, 0, 60, 3, 0));
    CHECK("an explicit DM_FPS pin disables the automatic override",
          !nvn_link_rate_wants_120(1, 1, 1, 60, 3, 0));

    int pending = 0;
    CHECK("an unverified adaptive 3-tick hold is clamped to safe 4-tick 30 fps",
          nvn_link_rate_gate_adaptive(1, 1, 0, 0, &pending, 3) == 4 && pending);
    CHECK("pending adaptive fallback keeps requesting 120 Hz at hold 4",
          nvn_link_rate_wants_120(1, 1, 0, 60, 4, pending));
    CHECK("unverified callbacks keep the adaptive fallback at 30 fps",
          nvn_link_rate_gate_adaptive(1, 1, 0, 0, &pending, 4) == 4 && pending);
    CHECK("verified 120 Hz allows the adaptive controller to try 40 fps",
          nvn_link_rate_gate_adaptive(1, 1, 0, 1, &pending, 3) == 3 && pending);
    CHECK("the pending link request remains while adaptive hold is above 2",
          nvn_link_rate_wants_120(1, 1, 0, 60, 3, pending));
    CHECK("returning to the 2-tick 60 fps floor releases the pending link request",
          nvn_link_rate_gate_adaptive(1, 1, 0, 1, &pending, 2) == 2 && !pending);
    CHECK("a 40 fps decision made with verified 120 Hz needs no pending fallback",
          nvn_link_rate_gate_adaptive(1, 1, 0, 1, &pending, 3) == 3 && !pending);
    pending = 1;
    CHECK("non-TOTK profiles clear and bypass the adaptive pending gate",
          nvn_link_rate_gate_adaptive(0, 1, 0, 0, &pending, 3) == 3 && !pending);
    pending = 1;
    CHECK("an explicit DM_FPS pin keeps its link mode but clamps unverified 40 fps to 30",
          nvn_link_rate_gate_adaptive(1, 1, 1, 0, &pending, 3) == 4 && !pending);
    pending = 1;
    CHECK("a native 30 fps cap clears the adaptive pending request",
          nvn_link_rate_gate_adaptive(1, 0, 0, 0, &pending, 4) == 4 && !pending);

    printf("t_link_rate: %d checks, %d failed\n", checks, failed);
    return failed != 0;
}
