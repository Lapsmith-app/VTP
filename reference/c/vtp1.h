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

/* ---- CAN subscription table ------------------------------------------ */

/* SPEC.md §9.5 — one page of the installed table, as CAN_LIST returns it. */
typedef struct {
    uint16_t total;     /* subscriptions installed, across all pages */
    uint16_t index;     /* table index of the first entry in this page */
    uint8_t  count;
    uint8_t  reserved;
} vtp_can_list_page_t;

typedef struct {
    uint16_t handle;
    uint32_t id;
    uint32_t mask;      /* a set bit is a bit of id that must match */
    uint8_t  mode;
    uint16_t arg;
} vtp_can_subscription_t;

/* Non-zero when the mode is one this build recognises. False means UNKNOWN and
 * MUST NOT be read as every_frame. */
int vtp_sub_mode_known(uint8_t mode);

/* Validates the whole page before yielding anything, as the CAN batch decoder
 * does, so a truncated trailing entry rejects the response rather than
 * half-decoding it. */
int vtp_decode_can_list(const uint8_t *buf, size_t len,
                        vtp_can_list_page_t *page, const char **err);
/* Caller supplies index < page->count; the page must already be validated. */
void vtp_can_subscription_at(const uint8_t *buf, uint8_t index,
                             vtp_can_subscription_t *out);

/* ---- Monitor ---------------------------------------------------------- */

/* SPEC.md §13 — the one role that runs client-to-device: the client supplies
 * values the device cannot compute, so a device with a display can show them.
 * Channels are enumerated rather than computed, so nothing here parses an
 * expression. */

typedef struct {
    uint16_t total, index;
    uint8_t  count, reserved;
} vtp_monitor_page_t;

typedef struct {
    uint8_t  slot;
    uint16_t channel;
    uint8_t  reserved;
} vtp_monitor_channel_t;

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
                            vtp_monitor_page_t *page, const char **err);
void vtp_monitor_channel_at(const uint8_t *buf, uint8_t index,
                            vtp_monitor_channel_t *out);

int vtp_decode_monitor_update(const uint8_t *buf, size_t len,
                              vtp_monitor_header_t *hdr, const char **err);
void vtp_monitor_value_at(const uint8_t *buf, uint8_t index,
                          vtp_monitor_value_t *out);

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

#ifdef __cplusplus
}
#endif
#endif /* VTP1_H */
