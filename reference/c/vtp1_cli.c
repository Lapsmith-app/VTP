/* Conformance harness adapter for the C reference implementation.
 *
 * Implements the runner contract in conformance/README.md:
 *   stdin  — one case per line: <record><TAB><hex>
 *   stdout — one JSON object per line, in the same order
 *
 * Any implementation in any language that speaks this contract can be tested
 * by conformance/run.py. That is the point: the suite is not C-specific.
 *
 * Every successfully decoded case is also re-encoded and reported as
 * `roundtrip_hex`. The runner requires that to equal the input byte for byte,
 * which checks two things one decode cannot: that the encoder and decoder agree
 * about the layout, and that the encoder emits the canonical form rather than
 * merely a form that happens to decode back.
 */
#include "vtp1.h"
#include "vtp1_encode.h"
#include <ctype.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAX_PAYLOAD 4096
#define MAX_CAN_FRAMES 128
#define MAX_IMU_SAMPLES 128

static int unhex(const char *s, uint8_t *out, size_t cap, size_t *len) {
    size_t n = 0;
    while (s[0] && s[1] && !isspace((unsigned char)s[0])) {
        if (n >= cap) return -1;
        char b[3] = {s[0], s[1], 0};
        char *end;
        long v = strtol(b, &end, 16);
        if (*end) return -1;
        out[n++] = (uint8_t)v;
        s += 2;
    }
    *len = n;
    return 0;
}

static void reject(const char *why) { printf("{\"ok\":false,\"reason\":\"%s\"}\n", why); }

/* Which validity bit gates which field. The decoder deliberately carries raw
 * values and leaves gating to the caller, so this is where that caller lives:
 * the harness reports what an application built on this decoder would be
 * required to treat as absent. */
static const struct { const char *name; uint32_t bit; } GATED[] = {
    {"t_utc",         VTP_GPS_VALIDITY_T_UTC},
    {"lat",           VTP_GPS_VALIDITY_POSITION},
    {"lon",           VTP_GPS_VALIDITY_POSITION},
    {"alt_msl",       VTP_GPS_VALIDITY_ALT_MSL},
    {"alt_ellipsoid", VTP_GPS_VALIDITY_ALT_ELLIPSOID},
    {"vel_n",         VTP_GPS_VALIDITY_VELOCITY},
    {"vel_e",         VTP_GPS_VALIDITY_VELOCITY},
    {"vel_d",         VTP_GPS_VALIDITY_VELOCITY},
    {"head_mot",      VTP_GPS_VALIDITY_HEAD_MOT},
    {"h_acc",         VTP_GPS_VALIDITY_H_ACC},
    {"v_acc",         VTP_GPS_VALIDITY_V_ACC},
    {"s_acc",         VTP_GPS_VALIDITY_S_ACC},
    {"p_dop",         VTP_GPS_VALIDITY_P_DOP},
    {"num_sv",        VTP_GPS_VALIDITY_NUM_SV},
};

static void put_absent(const vtp_gps_fix_t *f) {
    printf(",\"absent\":[");
    int first = 1;
    for (size_t i = 0; i < sizeof GATED / sizeof GATED[0]; i++) {
        if (vtp_gps_valid(f, GATED[i].bit)) continue;
        printf("%s\"%s\"", first ? "" : ",", GATED[i].name);
        first = 0;
    }
    printf("]");
}

/* The same idea for link_params: which validity bit gates which field. */
static const struct { const char *name; uint16_t bit; } LINK_GATED[] = {
    {"att_mtu",             VTP_LINK_VALIDITY_ATT_MTU},
    {"ll_max_tx_octets",    VTP_LINK_VALIDITY_LL_DATA_LENGTH},
    {"ll_max_rx_octets",    VTP_LINK_VALIDITY_LL_DATA_LENGTH},
    {"conn_interval",       VTP_LINK_VALIDITY_CONN_PARAMS},
    {"peripheral_latency",  VTP_LINK_VALIDITY_CONN_PARAMS},
    {"supervision_timeout", VTP_LINK_VALIDITY_CONN_PARAMS},
    {"phy_tx",              VTP_LINK_VALIDITY_PHY},
    {"phy_rx",              VTP_LINK_VALIDITY_PHY},
};

static void put_link_absent(const vtp_link_params_t *l) {
    printf(",\"absent\":[");
    int first = 1;
    for (size_t i = 0; i < sizeof LINK_GATED / sizeof LINK_GATED[0]; i++) {
        if (vtp_link_valid(l, LINK_GATED[i].bit)) continue;
        printf("%s\"%s\"", first ? "" : ",", LINK_GATED[i].name);
        first = 0;
    }
    printf("]");
}

/* SPEC.md §7 — a sensor group whose presence flag is clear is ABSENT, not a
 * measurement of zero. The decoder carries raw values and leaves this to the
 * caller, exactly as it does for gps_fix, so the harness reports what an
 * application built on it would be required to treat as absent. */
static void put_imu_absent(uint8_t flags) {
    static const char *ACCEL[] = {"ax", "ay", "az"};
    static const char *GYRO[] = {"gx", "gy", "gz"};
    printf(",\"absent\":[");
    int first = 1;
    /* Emitted in sorted order so it compares equal to the vector's list. */
    for (size_t i = 0; i < 3; i++) {
        if (flags & VTP_IMU_HAS_ACCEL) break;
        printf("%s\"%s\"", first ? "" : ",", ACCEL[i]);
        first = 0;
    }
    for (size_t i = 0; i < 3; i++) {
        if (flags & VTP_IMU_HAS_GYRO) break;
        printf("%s\"%s\"", first ? "" : ",", GYRO[i]);
        first = 0;
    }
    printf("]");
}

static void put_hex(const uint8_t *p, size_t n) {
    for (size_t i = 0; i < n; i++) printf("%02x", p[i]);
}

/* Closes the JSON object with the re-encoded bytes, or with an explicit
 * failure the runner will surface rather than silently skip. */
static void finish(const uint8_t *encoded, int n) {
    if (n < 0) {
        printf(",\"roundtrip_error\":\"encode-failed\"}\n");
        return;
    }
    printf(",\"roundtrip_hex\":\"");
    put_hex(encoded, (size_t)n);
    printf("\"}\n");
}

int main(void) {
    char line[8192];
    while (fgets(line, sizeof line, stdin)) {
        char *tab = strchr(line, '\t');
        if (!tab) continue;
        *tab = 0;
        const char *record = line;
        const char *hex = tab + 1;

        uint8_t buf[MAX_PAYLOAD];
        uint8_t enc[MAX_PAYLOAD];
        size_t len = 0;
        if (unhex(hex, buf, sizeof buf, &len) != 0) { reject("bad-hex"); continue; }

        const char *err = "unknown";

        if (!strcmp(record, "gps_fix")) {
            vtp_gps_fix_t f;
            if (vtp_decode_gps_fix(buf, len, &f, &err)) { reject(err); continue; }
            printf("{\"ok\":true,\"seq\":%u,\"dropped\":%u,\"validity\":%u,"
                   "\"t_device\":%" PRIu64 ",\"t_utc\":%" PRId64 ","
                   "\"lat\":%d,\"lon\":%d,\"alt_msl\":%d,\"alt_ellipsoid\":%d,"
                   "\"vel_n\":%d,\"vel_e\":%d,\"vel_d\":%d,\"head_mot\":%d,"
                   "\"h_acc\":%u,\"v_acc\":%u,\"s_acc\":%u,\"p_dop\":%u,"
                   "\"fix_type\":%u,\"num_sv\":%u,\"fix_flags\":%u,\"ext_count\":%u,"
                   "\"fix_type_known\":%s",
                   f.seq, f.dropped, f.validity, f.t_device, f.t_utc,
                   f.lat, f.lon, f.alt_msl, f.alt_ellipsoid,
                   f.vel_n, f.vel_e, f.vel_d, f.head_mot,
                   f.h_acc, f.v_acc, f.s_acc, f.p_dop,
                   f.fix_type, f.num_sv, f.fix_flags, f.ext_count,
                   vtp_fix_type_known(f.fix_type) ? "true" : "false");
            put_absent(&f);
            finish(enc, vtp_encode_gps_fix(&f, buf + f.ext_offset, f.ext_bytes,
                                           enc, sizeof enc));

        } else if (!strcmp(record, "can_batch")) {
            vtp_can_header_t h;
            vtp_can_iter_t it;
            if (vtp_can_batch_begin(buf, len, &h, &it, &err)) { reject(err); continue; }
            if (h.count > MAX_CAN_FRAMES) { reject("too-many-frames"); continue; }

            vtp_can_frame_t frames[MAX_CAN_FRAMES];
            uint8_t n = 0;
            while (vtp_can_iter_next(&it, &frames[n])) n++;

            printf("{\"ok\":true,\"header\":{\"seq\":%u,\"dropped\":%u,"
                   "\"t_base\":%" PRIu64 ",\"count\":%u,\"flags\":%u,\"reserved\":%u},"
                   "\"records\":[",
                   h.seq, h.dropped, h.t_base, h.count, h.flags, h.reserved);
            for (uint8_t i = 0; i < n; i++) {
                const vtp_can_frame_t *fr = &frames[i];
                printf("%s{\"dt\":%u,\"id\":%u,\"extended\":%s,\"fd\":%s,\"rtr\":%s,"
                       "\"len\":%u,\"payload\":\"",
                       i ? "," : "", fr->dt, fr->id,
                       fr->extended ? "true" : "false", fr->fd ? "true" : "false",
                       fr->rtr ? "true" : "false", fr->len);
                put_hex(fr->payload, fr->len);
                printf("\",\"t_device_us\":%" PRIu64 "}", fr->t_device);
            }
            printf("]");
            finish(enc, vtp_encode_can_batch(&h, frames, enc, sizeof enc));

        } else if (!strcmp(record, "imu_batch")) {
            vtp_imu_header_t h;
            if (vtp_decode_imu_batch(buf, len, &h, &err)) { reject(err); continue; }
            if (h.count > MAX_IMU_SAMPLES) { reject("too-many-samples"); continue; }

            vtp_imu_sample_t samples[MAX_IMU_SAMPLES];
            for (uint8_t i = 0; i < h.count; i++) {
                vtp_imu_sample_at(buf, &h, i, &samples[i]);
            }

            printf("{\"ok\":true,\"header\":{\"seq\":%u,\"dropped\":%u,"
                   "\"t_base\":%" PRIu64 ",\"period\":%u,\"count\":%u,"
                   "\"flags\":%u,\"reserved\":%u,\"saturated\":%s},"
                   "\"samples\":[",
                   h.seq, h.dropped, h.t_base, h.period, h.count, h.flags,
                   h.reserved,
                   (h.flags & VTP_IMU_FLAG_SATURATED) ? "true" : "false");
            for (uint8_t i = 0; i < h.count; i++) {
                const vtp_imu_sample_t *s = &samples[i];
                printf("%s{\"ax\":%d,\"ay\":%d,\"az\":%d,\"gx\":%d,\"gy\":%d,\"gz\":%d,"
                       "\"t_device_us\":%" PRIu64,
                       i ? "," : "", s->ax, s->ay, s->az, s->gx, s->gy, s->gz,
                       s->t_device);
                put_imu_absent(h.flags);
                printf("}");
            }
            printf("]");
            finish(enc, vtp_encode_imu_batch(&h, samples, enc, sizeof enc));

        } else if (!strcmp(record, "info")) {
            vtp_info_t v;
            if (vtp_decode_info(buf, len, &v, &err)) { reject(err); continue; }
            printf("{\"ok\":true,\"protocol_major\":%u,\"protocol_minor\":%u,"
                   "\"capabilities\":%u,\"gps_rate_hz\":%u,\"gps_max_rate_hz\":%u,"
                   "\"can_subscription_slots\":%u,\"can_max_frames_per_s\":%u,"
                   "\"imu_rate_hz\":%u,\"imu_max_rate_hz\":%u,\"can_max_payload\":%u,"
                   "\"clock_flags\":%u,\"max_notify_bytes\":%u",
                   v.protocol_major, v.protocol_minor, v.capabilities,
                   v.gps_rate_hz, v.gps_max_rate_hz, v.can_subscription_slots,
                   v.can_max_frames_per_s, v.imu_rate_hz, v.imu_max_rate_hz,
                   v.can_max_payload, v.clock_flags, v.max_notify_bytes);
            finish(enc, vtp_encode_info(&v, enc, sizeof enc));

        } else if (!strcmp(record, "can_list")) {
            vtp_can_list_page_t pg;
            if (vtp_decode_can_list(buf, len, &pg, &err)) { reject(err); continue; }
            vtp_can_subscription_t subs[256];
            for (uint8_t i = 0; i < pg.count; i++)
                vtp_can_subscription_at(buf, i, &subs[i]);

            printf("{\"ok\":true,\"page\":{\"total\":%u,\"index\":%u,"
                   "\"count\":%u,\"reserved\":%u},\"entries\":[",
                   pg.total, pg.index, pg.count, pg.reserved);
            for (uint8_t i = 0; i < pg.count; i++) {
                const vtp_can_subscription_t *s = &subs[i];
                printf("%s{\"handle\":%u,\"id\":%u,\"mask\":%u,\"mode\":%u,"
                       "\"arg\":%u,\"mode_known\":%s}",
                       i ? "," : "", s->handle, s->id, s->mask, s->mode, s->arg,
                       vtp_sub_mode_known(s->mode) ? "true" : "false");
            }
            printf("]");
            finish(enc, vtp_encode_can_list(&pg, subs, enc, sizeof enc));

        } else if (!strcmp(record, "monitor_list")) {
            vtp_monitor_declaration_t pg;
            if (vtp_decode_monitor_list(buf, len, &pg, &err)) { reject(err); continue; }
            vtp_monitor_channel_t chans[256];
            for (uint8_t i = 0; i < pg.count; i++)
                vtp_monitor_channel_at(buf, i, &chans[i]);
            printf("{\"ok\":true,\"declaration\":{"
                   "\"count\":%u,\"reserved\":%u},\"entries\":[",
                   pg.count, pg.reserved);
            for (uint8_t i = 0; i < pg.count; i++) {
                printf("%s{\"slot\":%u,\"channel\":%u,\"max_age\":%u,"
                       "\"channel_known\":%s}",
                       i ? "," : "", chans[i].slot, chans[i].channel,
                       chans[i].max_age,
                       vtp_channel_known(chans[i].channel) ? "true" : "false");
            }
            printf("]");
            finish(enc, vtp_encode_monitor_list(&pg, chans, enc, sizeof enc));

        } else if (!strcmp(record, "monitor_update")) {
            vtp_monitor_header_t mh;
            if (vtp_decode_monitor_update(buf, len, &mh, &err)) { reject(err); continue; }
            vtp_monitor_value_t vals[256];
            for (uint8_t i = 0; i < mh.count; i++)
                vtp_monitor_value_at(buf, i, &vals[i]);
            printf("{\"ok\":true,\"header\":{\"seq\":%u,\"count\":%u,"
                   "\"reserved\":%u},\"values\":[",
                   mh.seq, mh.count, mh.reserved);
            for (uint8_t i = 0; i < mh.count; i++) {
                const int present =
                    (vals[i].validity & VTP_MONITOR_VALIDITY_PRESENT) != 0;
                printf("%s{\"slot\":%u,\"validity\":%u,\"value\":%d,"
                       "\"absent\":[%s]}",
                       i ? "," : "", vals[i].slot, vals[i].validity,
                       vals[i].value, present ? "" : "\"value\"");
            }
            printf("]");
            finish(enc, vtp_encode_monitor_update(&mh, vals, enc, sizeof enc));

        } else if (!strcmp(record, "link_params")) {
            vtp_link_params_t l;
            if (vtp_decode_link_params(buf, len, &l, &err)) { reject(err); continue; }
            printf("{\"ok\":true,\"validity\":%u,\"att_mtu\":%u,"
                   "\"ll_max_tx_octets\":%u,\"ll_max_rx_octets\":%u,"
                   "\"conn_interval\":%u,\"peripheral_latency\":%u,"
                   "\"supervision_timeout\":%u,\"phy_tx\":%u,\"phy_rx\":%u,"
                   "\"phy_tx_known\":%s,\"phy_rx_known\":%s",
                   l.validity, l.att_mtu, l.ll_max_tx_octets, l.ll_max_rx_octets,
                   l.conn_interval, l.peripheral_latency, l.supervision_timeout,
                   l.phy_tx, l.phy_rx,
                   vtp_phy_known(l.phy_tx) ? "true" : "false",
                   vtp_phy_known(l.phy_rx) ? "true" : "false");
            put_link_absent(&l);
            finish(enc, vtp_encode_link_params(&l, enc, sizeof enc));

        } else if (!strcmp(record, "control_response")) {
            vtp_control_response_t r;
            if (vtp_decode_control_response(buf, len, &r, &err)) { reject(err); continue; }
            printf("{\"ok\":true,\"opcode\":%u,\"tag\":%u,\"status\":%u,"
                   "\"status_known\":%s,\"detail_hex\":\"",
                   r.opcode, r.tag, r.status,
                   vtp_status_known(r.status) ? "true" : "false");
            put_hex(r.detail, r.detail_len);
            printf("\"");
            finish(enc, vtp_encode_control_response(&r, enc, sizeof enc));

        } else if (!strcmp(record, "time_sync")) {
            vtp_time_sync_t t;
            if (vtp_decode_time_sync(buf, len, &t, &err)) { reject(err); continue; }
            printf("{\"ok\":true,\"t_device_rx\":%" PRIu64 ","
                   "\"t_device_tx\":%" PRIu64 ",\"processing_us\":%" PRIu64,
                   t.t_device_rx, t.t_device_tx,
                   t.t_device_tx - t.t_device_rx);
            finish(enc, vtp_encode_time_sync(&t, enc, sizeof enc));

        } else {
            reject("unknown-record");
        }
        fflush(stdout);
    }
    return 0;
}
