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
    uint8_t  can_max_payload, clock_flags;
    uint16_t max_notify_bytes;
} vtp_info_t;

int vtp_decode_info(const uint8_t *buf, size_t len,
                    vtp_info_t *out, const char **err);

/* ---- Link parameters ------------------------------------------------- */

/* SPEC.md §9.1 — the detail of a GET_LINK_PARAMS response. Reporting only:
 * nothing here is negotiated through VTP/1.
 *
 * Every field is gated by a validity bit, because a controller that cannot
 * report a parameter must say so rather than guess. Note that the phy enum has
 * no zero member, so a zeroed phy_tx can never pass for LE 1M. */
typedef struct {
    uint16_t validity;
    uint16_t att_mtu;
    uint16_t ll_max_tx_octets, ll_max_rx_octets;
    uint16_t conn_interval;        /* 1.25 ms units */
    uint16_t peripheral_latency;
    uint16_t supervision_timeout;  /* 10 ms units */
    uint8_t  phy_tx, phy_rx;
} vtp_link_params_t;

static inline int vtp_link_valid(const vtp_link_params_t *l, uint16_t bit) {
    return (l->validity & bit) != 0;
}

/* Non-zero when the PHY value is one this build recognises. A false result
 * means UNKNOWN and MUST NOT be treated as any particular PHY. */
int vtp_phy_known(uint8_t phy);

int vtp_decode_link_params(const uint8_t *buf, size_t len,
                           vtp_link_params_t *out, const char **err);

#ifdef __cplusplus
}
#endif
#endif /* VTP1_H */
