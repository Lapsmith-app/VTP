/* VTP/1 reference encoder — C99, no dependencies.
 *
 * The device-side half. Firmware needs this and an app does not, so it is a
 * separate translation unit: a decoder-only client links vtp1.c alone, and a
 * device links this alone if it never parses.
 *
 * Every function returns the number of bytes written, or -1 when the output
 * buffer is too small. Nothing is written on -1.
 *
 * The encoder ENFORCES the specification rather than trusting its caller:
 * a field whose validity bit is clear is written as zero regardless of what
 * the struct holds (SPEC.md §5.1). Firmware that computes a stale altitude
 * and then clears the bit therefore cannot leak the stale value onto the wire.
 */
#ifndef VTP1_ENCODE_H
#define VTP1_ENCODE_H

#include "vtp1.h"

#ifdef __cplusplus
extern "C" {
#endif

/* `ext` may be NULL with ext_len 0. `fix->ext_count` must agree with the
 * records actually present in `ext`; it is written through unchanged. */
int vtp_encode_gps_fix(const vtp_gps_fix_t *fix,
                       const uint8_t *ext, size_t ext_len,
                       uint8_t *out, size_t cap);

/* `hdr->count` frames are read from `frames`. */
int vtp_encode_can_batch(const vtp_can_header_t *hdr,
                         const vtp_can_frame_t *frames,
                         uint8_t *out, size_t cap);

/* `hdr->count` samples are read from `samples`. Absent sensor groups are
 * written as zero, matching the presence flags in `hdr->flags`. */
int vtp_encode_imu_batch(const vtp_imu_header_t *hdr,
                         const vtp_imu_sample_t *samples,
                         uint8_t *out, size_t cap);

int vtp_encode_info(const vtp_info_t *info, uint8_t *out, size_t cap);

int vtp_encode_time_sync(const vtp_time_sync_t *t, uint8_t *out, size_t cap);
int vtp_encode_control_response(const vtp_control_response_t *r,
                                uint8_t *out, size_t cap);

/* `page->count` entries are read from `entries`. */
int vtp_encode_monitor_list(const vtp_monitor_declaration_t *page,
                            const vtp_monitor_channel_t *entries,
                            uint8_t *out, size_t cap);

/* `hdr->count` values are read from `values`. A value whose present bit is
 * clear is written as zero whatever the caller supplied (SPEC.md §13.4). */
int vtp_encode_monitor_update(const vtp_monitor_header_t *hdr,
                              const vtp_monitor_value_t *values,
                              uint8_t *out, size_t cap);

#ifdef __cplusplus
}
#endif
#endif /* VTP1_ENCODE_H */
