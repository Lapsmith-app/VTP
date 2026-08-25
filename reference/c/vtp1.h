/* VTP/1 reference decoder — C99, no dependencies.
 *
 * Companion to SPEC.md. Where this code and the specification disagree, the
 * specification wins and this is a bug.
 *
 * Three rules from SPEC.md §1.1 are load-bearing here and are asserted by the
 * conformance vectors:
 *   - A malformed payload is rejected whole. Never decode a prefix.
 *   - A field whose validity bit is clear is ABSENT, not zero.
 *   - An unknown enum value stays unknown. Never coerce to a default.
 */
#ifndef VTP1_H
#define VTP1_H

#include <stddef.h>
#include <stdint.h>
#include "vtp1_generated.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ---- GPS ------------------------------------------------------------- */

typedef struct {
    uint16_t seq, dropped;
    uint32_t validity;
    uint64_t t_device;
    int64_t  t_utc;
    int32_t  lat, lon, alt_msl, alt_ellipsoid;
    int32_t  vel_n, vel_e, vel_d, head_mot;
    uint32_t h_acc, v_acc, s_acc;
    uint16_t p_dop;
    uint8_t  fix_type, num_sv, fix_flags, ext_count;
    /* Offset of the first extension record, and total extension bytes. */
    size_t   ext_offset, ext_bytes;
} vtp_gps_fix_t;

/* Non-zero when the named validity bit is set, e.g.
 * vtp_gps_valid(&fix, VTP_GPS_VALIDITY_POSITION). */
static inline int vtp_gps_valid(const vtp_gps_fix_t *f, uint32_t bit) {
    return (f->validity & bit) != 0;
}

/* Non-zero when fix_type is a value this build recognises. A false result
 * means UNKNOWN and MUST NOT be treated as any particular fix type. */
int vtp_fix_type_known(uint8_t fix_type);

/* Returns 0 on success, -1 on reject with *err set to a stable reason code. */
int vtp_decode_gps_fix(const uint8_t *buf, size_t len,
                       vtp_gps_fix_t *out, const char **err);

/* ---- CAN ------------------------------------------------------------- */

typedef struct {
    uint16_t seq, dropped;
    uint64_t t_base;
    uint8_t  count, flags;
    uint16_t reserved;
} vtp_can_header_t;

typedef struct {
    uint16_t dt;
    uint32_t id;          /* arbitration id, flag bits already stripped */
    int      extended, fd, rtr;
    uint8_t  len;
    const uint8_t *payload;
    uint64_t t_device;    /* t_base + dt * 10 us */
} vtp_can_frame_t;

typedef struct {
    const uint8_t *p;
    size_t remaining;
    uint8_t left;
    uint64_t t_base;
} vtp_can_iter_t;

/* Validates the whole batch length before yielding anything, so a truncated
 * trailing record rejects the notification rather than half-decoding it. */
int vtp_can_batch_begin(const uint8_t *buf, size_t len,
                        vtp_can_header_t *hdr, vtp_can_iter_t *it,
                        const char **err);
int vtp_can_iter_next(vtp_can_iter_t *it, vtp_can_frame_t *out);

/* ---- IMU ------------------------------------------------------------- */

typedef struct {
    uint16_t seq, dropped;
    uint64_t t_base;
    uint32_t period;      /* microseconds; u32 so sub-15.26 Hz rates fit */
    uint8_t  count, flags;
    uint16_t reserved;
} vtp_imu_header_t;

#define VTP_IMU_HAS_ACCEL 0x01
#define VTP_IMU_HAS_GYRO  0x02

typedef struct {
    int16_t ax, ay, az;   /* milli-g; absent unless flags & VTP_IMU_HAS_ACCEL */
    int16_t gx, gy, gz;   /* 0.05 deg/s; absent unless flags & VTP_IMU_HAS_GYRO */
    uint64_t t_device;
} vtp_imu_sample_t;

int vtp_decode_imu_batch(const uint8_t *buf, size_t len,
                         vtp_imu_header_t *hdr, const char **err);
/* Caller supplies index < hdr->count; batch must already have been validated. */
void vtp_imu_sample_at(const uint8_t *buf, const vtp_imu_header_t *hdr,
                       uint8_t index, vtp_imu_sample_t *out);

/* ---- Info ------------------------------------------------------------ */

typedef struct {
    uint8_t  protocol_major, protocol_minor;
    uint32_t capabilities;
    uint16_t gps_rate_hz, gps_max_rate_hz, can_subscription_slots;
    uint32_t can_max_frames_per_s;
    uint16_t imu_rate_hz, imu_max_rate_hz;
    uint8_t  obd_poll_slots;      /* SPEC.md 15.4; byte 20, assigned per 11.2 */
    uint8_t  clock_flags;
    /* Bytes 22-23 held obd_min_interval_ms and are reserved again:
       SPEC.md 15.4 is response-paced, so there is no declared rate. Still
       decoded, because SPEC.md 2 requires a receiver to carry reserved
       bytes through rather than assume them zero. */
    uint16_t reserved_22;
} vtp_info_t;

int vtp_decode_info(const uint8_t *buf, size_t len,
                    vtp_info_t *out, const char **err);

/* SPEC.md §4.1 -- non-zero when this capability word satisfies the profile
 * matrix: every implication met, and every capacity field zero behind a
 * cleared bit. Not called by the decoder: an Info that breaks the matrix
 * decodes, and this is what a CLIENT calls to surface the contradiction (and
 * what the encoder mirrors to refuse producing one). `info` may be NULL to
 * check only the implications. On a false result `why` (when not NULL) names
 * the bit or field that failed. */
int vtp_capabilities_coherent(uint32_t capabilities,
                              const uint8_t *info, size_t len,
                              const char **why);

/* ---- Monitor ---------------------------------------------------------- */

/* SPEC.md §13 — the one role that runs client-to-device: the client supplies
 * values the device cannot compute, so a device with a display can show them.
 * Channels are enumerated rather than computed, so nothing here parses an
 * expression. */

/* Not paged: MONITOR_LIST answers with the whole declaration (SPEC.md 13.3).
 * `total` and `index` are gone with the paging they described. */
typedef struct {
    uint8_t  count, reserved;
} vtp_monitor_declaration_t;

typedef struct {
    uint8_t  slot;
    uint16_t channel;
    uint8_t  max_age;   /* 100 ms units; never zero. SPEC.md §13.5 */
} vtp_monitor_channel_t;

/* SPEC.md §13.4 -- the most channels a device may ask for: as many values as
 * fit beside a monitor_header in one write at the §2 minimum ATT MTU, less the
 * 3-byte ATT write header. A complete write is only a workable rule if a
 * complete write always fits. */
#define VTP_MONITOR_MAX_CHANNELS \
    ((VTP_MIN_ATT_MTU - 3 - VTP_MONITOR_HEADER_SIZE) / VTP_MONITOR_VALUE_SIZE)

typedef struct {
    uint16_t seq;
    uint8_t  count, reserved;
} vtp_monitor_header_t;

typedef struct {
    uint8_t  slot;
    uint8_t  validity;
    int32_t  value;     /* absent unless validity & VTP_MONITOR_VALIDITY_PRESENT */
} vtp_monitor_value_t;

/* Non-zero when the channel is one this build recognises. False means UNKNOWN;
 * the client answers the slot absent rather than substituting another. */
int vtp_channel_known(uint16_t channel);

int vtp_decode_monitor_list(const uint8_t *buf, size_t len,
                            vtp_monitor_declaration_t *page, const char **err);
void vtp_monitor_channel_at(const uint8_t *buf, uint8_t index,
                            vtp_monitor_channel_t *out);

int vtp_decode_monitor_update(const uint8_t *buf, size_t len,
                              vtp_monitor_header_t *hdr, const char **err);
void vtp_monitor_value_at(const uint8_t *buf, uint8_t index,
                          vtp_monitor_value_t *out);

/* SPEC.md §7.2 -- imu_header.flags bit 2: at least one sample in this batch
 * was at or beyond the range of the sensor that produced it. The reading is a
 * lower bound on the magnitude, not a measurement. */
#define VTP_IMU_FLAG_SATURATED 0x04

/* ---- Aiding ----------------------------------------------------------- */

/* SPEC.md §14.2 -- what aiding a device accepts, and what it already holds.
 *
 * `format` names the receiver's format, not this protocol's: the bytes of a
 * transfer are opaque here and a client MUST NOT send a format the device did
 * not declare. `held_until` is gated, because "holds nothing" and "holds a
 * window ending at the Unix epoch" are different answers. */
typedef struct {
    uint8_t  validity;
    uint8_t  format;
    uint16_t reserved_2;   /* Appendix A holds it; MUST be ignored on receive */
    uint32_t max_bytes;
    int64_t  held_until;   /* ms, Unix epoch -- same as gps_fix.t_utc */
} vtp_gnss_aid_caps_t;

static inline int vtp_aid_valid(const vtp_gnss_aid_caps_t *c, uint8_t bit) {
    return (c->validity & bit) != 0;
}

/* SPEC.md §14.3 -- the detail of GNSS_AID_BEGIN. `chunk_bytes` is fixed for
 * the whole transfer so that index-to-offset is arithmetic; without that a
 * device could not place a resent chunk. `token` names the transfer: EATT
 * lets a client hold several ATT bearers, ordered only within each, so the
 * token is what keeps a stale chunk out of the transfer that superseded it. */
typedef struct {
    uint8_t  token;
    uint16_t chunk_bytes;
    uint8_t  reserved_3;   /* Appendix A holds it; MUST be ignored on receive */
} vtp_aid_begin_result_t;

/* SPEC.md §14.4 -- the detail of GNSS_AID_COMMIT. `first_missing` is gated:
 * chunk 0 is a real index, so a cleared bit is the only way to say "nothing is
 * missing" without it reading as "chunk 0 is". */
typedef struct {
    uint8_t  validity;
    uint8_t  result;
    uint16_t first_missing;
} vtp_aid_commit_result_t;

static inline int vtp_commit_valid(const vtp_aid_commit_result_t *c, uint8_t bit) {
    return (c->validity & bit) != 0;
}

/* Non-zero when the value is one this build recognises. A false result means
 * UNKNOWN and MUST NOT be treated as any particular member (SPEC.md §11.4). */
int vtp_aid_format_known(uint8_t format);
int vtp_aid_result_known(uint8_t result);

int vtp_decode_gnss_aid_caps(const uint8_t *buf, size_t len,
                             vtp_gnss_aid_caps_t *out, const char **err);
int vtp_decode_aid_begin_result(const uint8_t *buf, size_t len,
                                vtp_aid_begin_result_t *out, const char **err);
int vtp_decode_aid_commit_result(const uint8_t *buf, size_t len,
                                 vtp_aid_commit_result_t *out, const char **err);

/* SPEC.md §9 -- the envelope of every Control response. `detail` points into
 * the caller's buffer and is non-NULL only when status is ok; its shape is
 * decided by the opcode, and §11.3 lets a minor version add opcodes carrying
 * anything, so this decoder carries the bytes rather than parsing them. */
typedef struct {
    uint8_t opcode;
    uint8_t tag;
    uint8_t status;
    const uint8_t *detail;
    size_t detail_len;
} vtp_control_response_t;

/* Non-zero when the status value is one this build recognises. A false result
 * means UNKNOWN and MUST NOT be treated as a failure or as success. */
int vtp_status_known(uint8_t status);

int vtp_decode_control_response(const uint8_t *buf, size_t len,
                                vtp_control_response_t *out, const char **err);

/* ---- Power ------------------------------------------------------------ */

/* SPEC.md §9.7 -- the detail of a GET_POWER response. Measured when the
 * request arrives, so it carries no timestamp of its own.
 *
 * The two fields are gated separately because a device knows them separately:
 * one on the car's ignition feed knows it is on external power and has no
 * charge to report, and one whose gauge has failed knows the opposite. As
 * everywhere else, a cleared bit means absent -- and the source enum has no
 * zero member, so a zeroed byte can never pass for mains. */
typedef struct {
    uint8_t validity;
    uint8_t source;      /* enum power_source */
    uint8_t percent;     /* 0..100 */
} vtp_power_state_t;

static inline int vtp_power_valid(const vtp_power_state_t *p, uint8_t bit) {
    return (p->validity & bit) != 0;
}

/* Non-zero when the source value is one this build recognises. A false result
 * means UNKNOWN and MUST NOT be treated as any particular supply state. */
int vtp_power_source_known(uint8_t source);

int vtp_decode_power_state(const uint8_t *buf, size_t len,
                           vtp_power_state_t *out, const char **err);

/* ---- OBD (SPEC.md §15) ------------------------------------------------ */

/* SPEC.md §15.2 -- the detail of an OBD_INFO response: what the car in front
 * of the device answered, measured when asked. Followed by `count` obd_ecu
 * entries.
 *
 * One validity bit gates four fields, because they are one measurement: a
 * probe nothing answered has no request identifier that worked and no masks
 * that were read. `count` is NOT gated -- it is what the payload is walked
 * by, so it is layout -- and its agreement with `responded` is a content
 * rule the decoder deliberately does not reject (§1.1's split). */
typedef struct {
    uint8_t  validity;
    uint8_t  count;
    uint32_t request_id;       /* bits 0-28 arbitration, b29 extended */
    uint32_t supported_01_20;  /* bit n = PID 0x01+n, LSB first. SPEC.md 15.3 */
    uint32_t supported_21_40;  /* bit n = PID 0x21+n */
    uint32_t supported_41_60;  /* bit n = PID 0x41+n */
    uint16_t reserved_18;      /* Appendix A holds it; MUST be ignored on receive */
} vtp_obd_probe_t;

typedef struct {
    uint32_t id;               /* response identifier, same layout as request_id */
} vtp_obd_ecu_t;

static inline int vtp_obd_valid(const vtp_obd_probe_t *p, uint8_t bit) {
    return (p->validity & bit) != 0;
}

/* SPEC.md §15.2 -- identifier validity for request_id and obd_ecu.id: §6.4's
 * rule with bits 30-31 required zero, because these fields name identifiers
 * rather than how a frame travelled. One inline shared by the decoder and
 * the encoder, so the two translation units cannot drift on one rule --
 * §15.2 scopes WHERE it applies (entry ids always; request_id only when
 * `responded` is set), and each caller carries that scope. */
static inline int vtp_obd_identifier_ok(uint32_t raw) {
    if (raw & 0xC0000000u) return 0;
    if (!(raw & (1u << 29)) && (raw & 0x1FFFFFFFu) > 0x7FFu) return 0;
    return 1;
}

/* Validates length arithmetic and §15.2's identifier validity for the whole
 * response before returning. The content rules -- count agreeing with
 * `responded`, entries strictly ascending, at most eight -- are NOT rejected
 * here: the layout is sound, so the response decodes and the CLIENT flags
 * the contradiction. The encoder refuses all of them (vtp1_encode.c). */
int vtp_decode_obd_info(const uint8_t *buf, size_t len,
                        vtp_obd_probe_t *out, const char **err);
/* Caller supplies index < out->count; the buffer must already have been
 * validated by vtp_decode_obd_info. */
void vtp_obd_ecu_at(const uint8_t *buf, uint8_t index, vtp_obd_ecu_t *out);

/* SPEC.md §9.5 -- two readings of the device clock, so a client can take the
 * device's own processing time out of the round trip and bound its error. */
typedef struct {
    uint64_t t_device_rx;
    uint64_t t_device_tx;
} vtp_time_sync_t;

int vtp_decode_time_sync(const uint8_t *buf, size_t len,
                         vtp_time_sync_t *out, const char **err);

#ifdef __cplusplus
}
#endif
#endif /* VTP1_H */
