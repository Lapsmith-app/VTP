/* The C encoder's API contract, checked where no wire vector can reach.
 *
 * conformance/produce.py tests the PROTOCOL contract: which inputs an encoder
 * must refuse. Its cases travel as JSON, so every array it describes exists.
 * Two of this header's promises are therefore untestable from there, and both
 * were broken:
 *
 *   1. "`hdr->count` frames are read from `frames`" — a count with no array
 *      behind it. vtp_encode_can_batch read frames[0].dt before checking,
 *      vtp_encode_monitor_list and vtp_encode_monitor_update ran their
 *      duplicate-slot sweeps before checking, and vtp_encode_imu_batch
 *      dereferenced a sample only when a presence flag was set — so the same
 *      malformed call crashed or silently emitted zeroed samples depending on
 *      one bit of the header.
 *
 *   2. "Nothing is written on -1." vtp_encode_can_batch validated the
 *      arbitration identifier inside its write loop, after the header had gone
 *      into the buffer, so a refused batch left the caller's buffer modified
 *      and every byte of the previous notification still readable behind it.
 *
 * Run under ASan and UBSan (`make san`): a null dereference that happens not
 * to fault is not a pass.
 */
#include "vtp1_encode.h"
#include <stdio.h>
#include <string.h>

static int failures = 0;

static void ok(int condition, const char *what) {
    if (condition) return;
    printf("    FAIL %s\n", what);
    failures++;
}

/* Every buffer starts filled with this. A function that returns -1 must leave
 * it untouched, so any other byte is a write that should not have happened. */
#define POISON 0xAB

static int untouched(const uint8_t *buf, size_t n) {
    for (size_t i = 0; i < n; i++)
        if (buf[i] != POISON) return 0;
    return 1;
}

#define SETUP uint8_t out[512]; memset(out, POISON, sizeof out)

/* ---- a count with no array behind it ---------------------------------- */

static void missing_arrays(void) {
    printf("  a count with no array behind it is refused, not dereferenced\n");
    {
        SETUP;
        vtp_can_header_t h;
        memset(&h, 0, sizeof h);
        h.count = 2;
        ok(vtp_encode_can_batch(&h, NULL, out, sizeof out) == -1,
           "can_batch(count=2, frames=NULL) refuses");
        ok(untouched(out, sizeof out), "can_batch(frames=NULL) wrote nothing");
    }
    {
        /* Both presence flags, because the write loop only reached a sample
         * through a set flag: with flags=0 the identical malformed call used
         * to return a perfectly well-formed batch of zeroed samples. */
        for (uint8_t flags = 0; flags <= 3; flags++) {
            SETUP;
            vtp_imu_header_t h;
            memset(&h, 0, sizeof h);
            h.count = 2;
            h.period = 1000;
            h.flags = flags;
            ok(vtp_encode_imu_batch(&h, NULL, out, sizeof out) == -1,
               "imu_batch(count=2, samples=NULL) refuses whatever flags say");
            ok(untouched(out, sizeof out), "imu_batch(samples=NULL) wrote nothing");
        }
    }
    {
        SETUP;
        vtp_monitor_declaration_t p;
        memset(&p, 0, sizeof p);
        p.count = 2;
        ok(vtp_encode_monitor_list(&p, NULL, out, sizeof out) == -1,
           "monitor_list(count=2, entries=NULL) refuses");
        ok(untouched(out, sizeof out), "monitor_list(entries=NULL) wrote nothing");
    }
    {
        SETUP;
        vtp_monitor_header_t h;
        memset(&h, 0, sizeof h);
        h.count = 2;
        ok(vtp_encode_monitor_update(&h, NULL, out, sizeof out) == -1,
           "monitor_update(count=2, values=NULL) refuses");
        ok(untouched(out, sizeof out), "monitor_update(values=NULL) wrote nothing");
    }
    {
        SETUP;
        vtp_gps_fix_t f;
        memset(&f, 0, sizeof f);
        ok(vtp_encode_gps_fix(&f, NULL, 4, out, sizeof out) == -1,
           "gps_fix(ext_len=4, ext=NULL) refuses");
        ok(untouched(out, sizeof out), "gps_fix(ext=NULL) wrote nothing");
    }
    {
        SETUP;
        vtp_can_header_t h;
        vtp_can_frame_t f;
        memset(&h, 0, sizeof h);
        memset(&f, 0, sizeof f);
        h.count = 1;
        f.len = 4;              /* a length with no payload behind it */
        f.payload = NULL;
        ok(vtp_encode_can_batch(&h, &f, out, sizeof out) == -1,
           "can_batch(len=4, payload=NULL) refuses");
        ok(untouched(out, sizeof out), "can_batch(payload=NULL) wrote nothing");
    }
    {
        SETUP;
        vtp_control_response_t r;
        memset(&r, 0, sizeof r);
        r.detail_len = 2;
        r.detail = NULL;
        ok(vtp_encode_control_response(&r, out, sizeof out) == -1,
           "control_response(detail_len=2, detail=NULL) refuses");
        ok(untouched(out, sizeof out), "control_response(detail=NULL) wrote nothing");
    }
    {
        SETUP;
        vtp_obd_probe_t pr;
        memset(&pr, 0, sizeof pr);
        pr.validity = VTP_OBD_VALIDITY_RESPONDED;
        pr.request_id = 0x7DF;
        pr.count = 2;
        ok(vtp_encode_obd_info(&pr, NULL, out, sizeof out) == -1,
           "obd_info(count=2, ecus=NULL) refuses");
        ok(untouched(out, sizeof out), "obd_info(ecus=NULL) wrote nothing");
    }
    /* A count of zero has nothing to read, so a null array is not malformed
     * there: SPEC.md 13.5 lets a device ask for no channels at all, and that
     * declaration is exactly a header with no entries behind it. */
    {
        SETUP;
        vtp_monitor_declaration_t p;
        memset(&p, 0, sizeof p);
        ok(vtp_encode_monitor_list(&p, NULL, out, sizeof out)
               == VTP_MONITOR_DECLARATION_SIZE,
           "monitor_list(count=0, entries=NULL) is a legal empty declaration");
    }
    /* Same rule for the silent-car probe (SPEC.md 15.2): `responded` clear,
     * count 0, no array -- a record a device must be able to produce. */
    {
        SETUP;
        vtp_obd_probe_t pr;
        memset(&pr, 0, sizeof pr);
        ok(vtp_encode_obd_info(&pr, NULL, out, sizeof out)
               == VTP_OBD_PROBE_SIZE,
           "obd_info(count=0, ecus=NULL) is a legal silent-car probe");
    }
}

/* ---- nothing is written on failure ------------------------------------ */

static void atomicity(void) {
    printf("  a refusal leaves the caller's buffer exactly as it found it\n");
    {
        /* The regression this test was written for. The identifier check used
         * to live in the write loop, so seq, dropped and t_base were already
         * in the buffer when the call returned -1. */
        SETUP;
        vtp_can_header_t h;
        vtp_can_frame_t f;
        memset(&h, 0, sizeof h);
        memset(&f, 0, sizeof f);
        h.seq = 0x1234;
        h.count = 1;
        f.extended = 1;
        f.id = 0x3FFFFFFFu;         /* outside the 29-bit arbitration field */
        ok(vtp_encode_can_batch(&h, &f, out, sizeof out) == -1,
           "can_batch refuses an identifier outside the arbitration field");
        ok(untouched(out, sizeof out),
           "can_batch wrote nothing when it refused the identifier");
    }
    {
        SETUP;
        vtp_can_header_t h;
        vtp_can_frame_t f;
        memset(&h, 0, sizeof h);
        memset(&f, 0, sizeof f);
        h.seq = 0x1234;
        h.count = 1;
        f.id = 0x7FF;
        f.fd = 1;
        f.len = 9;                  /* no CAN FD DLC expresses nine */
        ok(vtp_encode_can_batch(&h, &f, out, sizeof out) == -1,
           "can_batch refuses a length off the CAN FD ladder");
        ok(untouched(out, sizeof out), "can_batch wrote nothing when it refused");
    }
    {
        SETUP;
        vtp_monitor_header_t h;
        vtp_monitor_value_t v[2];
        memset(&h, 0, sizeof h);
        memset(v, 0, sizeof v);
        h.count = 2;
        v[0].slot = v[1].slot = 3;  /* the same slot twice */
        ok(vtp_encode_monitor_update(&h, v, out, sizeof out) == -1,
           "monitor_update refuses a repeated slot");
        ok(untouched(out, sizeof out),
           "monitor_update wrote nothing when it refused");
    }
    {
        SETUP;
        vtp_time_sync_t t;
        memset(&t, 0, sizeof t);
        t.t_device_rx = 9000000;
        t.t_device_tx = 8999000;    /* answered before it was asked */
        ok(vtp_encode_time_sync(&t, out, sizeof out) == -1,
           "time_sync refuses a negative round trip");
        ok(untouched(out, sizeof out), "time_sync wrote nothing when it refused");
    }
    {
        SETUP;
        vtp_power_state_t p;
        memset(&p, 0, sizeof p);
        p.validity = VTP_POWER_VALIDITY_PERCENT;
        p.percent  = 200;           /* SPEC.md 9.7 -- the field is 0..100 */
        ok(vtp_encode_power_state(&p, out, sizeof out) == -1,
           "power_state refuses a percent above 100");
        ok(untouched(out, sizeof out),
           "power_state wrote nothing when it refused");
    }
    {
        /* The refusal is found at entry 1, after entry 0 already validated:
         * exactly the shape that leaks a half-written record if the checks
         * live in the write loop. SPEC.md 15.2. */
        SETUP;
        vtp_obd_probe_t pr;
        vtp_obd_ecu_t e[2];
        memset(&pr, 0, sizeof pr);
        memset(e, 0, sizeof e);
        pr.validity = VTP_OBD_VALIDITY_RESPONDED;
        pr.request_id = 0x7DF;
        pr.count = 2;
        e[0].id = 0x7E9;
        e[1].id = 0x7E8;            /* not strictly ascending */
        ok(vtp_encode_obd_info(&pr, e, out, sizeof out) == -1,
           "obd_info refuses entries out of order");
        ok(untouched(out, sizeof out),
           "obd_info wrote nothing when it refused");
    }
}

/* ---- a buffer too small to hold the answer ----------------------------- */

static void short_buffers(void) {
    printf("  a buffer one byte short is a refusal, not a partial record\n");
    {
        SETUP;
        vtp_info_t v;
        memset(&v, 0, sizeof v);
        ok(vtp_encode_info(&v, out, VTP_INFO_SIZE - 1) == -1,
           "info refuses a buffer one byte short");
        ok(untouched(out, sizeof out), "info wrote nothing into a short buffer");
    }
    {
        SETUP;
        vtp_gps_fix_t f;
        memset(&f, 0, sizeof f);
        ok(vtp_encode_gps_fix(&f, NULL, 0, out, VTP_GPS_FIX_SIZE - 1) == -1,
           "gps_fix refuses a buffer one byte short");
        ok(untouched(out, sizeof out), "gps_fix wrote nothing into a short buffer");
    }
    {
        SETUP;
        vtp_time_sync_t t;
        memset(&t, 0, sizeof t);
        ok(vtp_encode_time_sync(&t, out, VTP_TIME_SYNC_SIZE - 1) == -1,
           "time_sync refuses a buffer one byte short");
        ok(untouched(out, sizeof out),
           "time_sync wrote nothing into a short buffer");
    }
    {
        SETUP;
        vtp_power_state_t p;
        memset(&p, 0, sizeof p);
        ok(vtp_encode_power_state(&p, out, VTP_POWER_STATE_SIZE - 1) == -1,
           "power_state refuses a buffer one byte short");
        ok(untouched(out, sizeof out),
           "power_state wrote nothing into a short buffer");
    }
    {
        SETUP;
        vtp_imu_header_t h;
        vtp_imu_sample_t s;
        memset(&h, 0, sizeof h);
        memset(&s, 0, sizeof s);
        h.count = 1;
        h.period = 1000;
        ok(vtp_encode_imu_batch(&h, &s, out,
                                VTP_IMU_HEADER_SIZE + VTP_IMU_SAMPLE_SIZE - 1) == -1,
           "imu_batch refuses a buffer one byte short");
        ok(untouched(out, sizeof out),
           "imu_batch wrote nothing into a short buffer");
    }
    {
        SETUP;
        vtp_obd_probe_t pr;
        vtp_obd_ecu_t e;
        memset(&pr, 0, sizeof pr);
        memset(&e, 0, sizeof e);
        pr.validity = VTP_OBD_VALIDITY_RESPONDED;
        pr.request_id = 0x7DF;
        pr.count = 1;
        e.id = 0x7E8;
        ok(vtp_encode_obd_info(&pr, &e, out,
                               VTP_OBD_PROBE_SIZE + VTP_OBD_ECU_SIZE - 1) == -1,
           "obd_info refuses a buffer one byte short");
        ok(untouched(out, sizeof out),
           "obd_info wrote nothing into a short buffer");
    }
}

int main(void) {
    printf("vtp1_encode.c — C API contract\n");
    missing_arrays();
    atomicity();
    short_buffers();
    if (failures) {
        printf("\n%d check(s) failed.\n", failures);
        return 1;
    }
    printf("\nall checks passed.\n");
    return 0;
}
