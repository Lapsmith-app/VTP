#include "vtp1_encode.h"
#include <string.h>

/* Little-endian writers, byte at a time for the same reason the readers are:
 * correct on a big-endian host and on an unaligned buffer. */
static void wr16(uint8_t *p, uint16_t v) {
    p[0] = (uint8_t)(v & 0xFF);
    p[1] = (uint8_t)((v >> 8) & 0xFF);
}
static void wr32(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)(v & 0xFF);
    p[1] = (uint8_t)((v >> 8) & 0xFF);
    p[2] = (uint8_t)((v >> 16) & 0xFF);
    p[3] = (uint8_t)((v >> 24) & 0xFF);
}
static void wr64(uint8_t *p, uint64_t v) {
    wr32(p, (uint32_t)(v & 0xFFFFFFFFu));
    wr32(p + 4, (uint32_t)((v >> 32) & 0xFFFFFFFFu));
}

/* SPEC.md §5.1: a field whose validity bit is clear MUST be written as zero.
 * Applied here rather than trusted from the caller, so a device that clears a
 * bit cannot also ship the stale value it was hiding. */
static uint32_t gate32(uint32_t value, uint32_t validity, uint32_t bit) {
    return (validity & bit) ? value : 0u;
}
static uint64_t gate64(uint64_t value, uint32_t validity, uint32_t bit) {
    return (validity & bit) ? value : 0u;
}

int vtp_encode_gps_fix(const vtp_gps_fix_t *f,
                       const uint8_t *ext, size_t ext_len,
                       uint8_t *out, size_t cap) {
    if (cap < (size_t)VTP_GPS_FIX_SIZE + ext_len) return -1;
    memset(out, 0, VTP_GPS_FIX_SIZE);

    const uint32_t v = f->validity;

    wr16(out + VTP_GPS_FIX_OFF_SEQ, f->seq);
    wr16(out + VTP_GPS_FIX_OFF_DROPPED, f->dropped);
    wr32(out + VTP_GPS_FIX_OFF_VALIDITY, v);
    wr64(out + VTP_GPS_FIX_OFF_T_DEVICE, f->t_device);
    wr64(out + VTP_GPS_FIX_OFF_T_UTC,
         gate64((uint64_t)f->t_utc, v, VTP_GPS_VALIDITY_T_UTC));
    wr32(out + VTP_GPS_FIX_OFF_LAT,
         gate32((uint32_t)f->lat, v, VTP_GPS_VALIDITY_POSITION));
    wr32(out + VTP_GPS_FIX_OFF_LON,
         gate32((uint32_t)f->lon, v, VTP_GPS_VALIDITY_POSITION));
    wr32(out + VTP_GPS_FIX_OFF_ALT_MSL,
         gate32((uint32_t)f->alt_msl, v, VTP_GPS_VALIDITY_ALT_MSL));
    wr32(out + VTP_GPS_FIX_OFF_ALT_ELLIPSOID,
         gate32((uint32_t)f->alt_ellipsoid, v, VTP_GPS_VALIDITY_ALT_ELLIPSOID));
    wr32(out + VTP_GPS_FIX_OFF_VEL_N,
         gate32((uint32_t)f->vel_n, v, VTP_GPS_VALIDITY_VELOCITY));
    wr32(out + VTP_GPS_FIX_OFF_VEL_E,
         gate32((uint32_t)f->vel_e, v, VTP_GPS_VALIDITY_VELOCITY));
    wr32(out + VTP_GPS_FIX_OFF_VEL_D,
         gate32((uint32_t)f->vel_d, v, VTP_GPS_VALIDITY_VELOCITY));
    wr32(out + VTP_GPS_FIX_OFF_HEAD_MOT,
         gate32((uint32_t)f->head_mot, v, VTP_GPS_VALIDITY_HEAD_MOT));
    wr32(out + VTP_GPS_FIX_OFF_H_ACC, gate32(f->h_acc, v, VTP_GPS_VALIDITY_H_ACC));
    wr32(out + VTP_GPS_FIX_OFF_V_ACC, gate32(f->v_acc, v, VTP_GPS_VALIDITY_V_ACC));
    wr32(out + VTP_GPS_FIX_OFF_S_ACC, gate32(f->s_acc, v, VTP_GPS_VALIDITY_S_ACC));
    wr16(out + VTP_GPS_FIX_OFF_P_DOP,
         (uint16_t)gate32(f->p_dop, v, VTP_GPS_VALIDITY_P_DOP));
    out[VTP_GPS_FIX_OFF_FIX_TYPE] = f->fix_type;
    out[VTP_GPS_FIX_OFF_NUM_SV] =
        (uint8_t)gate32(f->num_sv, v, VTP_GPS_VALIDITY_NUM_SV);
    out[VTP_GPS_FIX_OFF_FIX_FLAGS] = f->fix_flags;
    out[VTP_GPS_FIX_OFF_EXT_COUNT] = f->ext_count;

    if (ext_len && ext) memcpy(out + VTP_GPS_FIX_SIZE, ext, ext_len);
    return (int)(VTP_GPS_FIX_SIZE + ext_len);
}

int vtp_encode_can_batch(const vtp_can_header_t *h,
                         const vtp_can_frame_t *frames,
                         uint8_t *out, size_t cap) {
    size_t needed = VTP_CAN_HEADER_SIZE;
    for (uint8_t i = 0; i < h->count; i++) {
        if (frames[i].len > 64) return -1;
        needed += VTP_CAN_RECORD_SIZE + frames[i].len;
    }
    if (cap < needed) return -1;

    memset(out, 0, VTP_CAN_HEADER_SIZE);
    wr16(out + VTP_CAN_HEADER_OFF_SEQ, h->seq);
    wr16(out + VTP_CAN_HEADER_OFF_DROPPED, h->dropped);
    wr64(out + VTP_CAN_HEADER_OFF_T_BASE, h->t_base);
    out[VTP_CAN_HEADER_OFF_COUNT] = h->count;
    out[VTP_CAN_HEADER_OFF_FLAGS] = h->flags;
    /* Reserved bytes are written through rather than forced to zero: a device
     * built against a later minor may have been assigned them, and this
     * encoder must not silently erase a field it does not know about. */
    wr16(out + VTP_CAN_HEADER_OFF_RESERVED, h->reserved);

    size_t off = VTP_CAN_HEADER_SIZE;
    for (uint8_t i = 0; i < h->count; i++) {
        const vtp_can_frame_t *fr = &frames[i];
        uint32_t raw = fr->id & 0x1FFFFFFFu;
        if (fr->extended) raw |= (1u << 29);
        if (fr->fd)       raw |= (1u << 30);
        if (fr->rtr)      raw |= (1u << 31);

        wr16(out + off + VTP_CAN_RECORD_OFF_DT, fr->dt);
        wr32(out + off + VTP_CAN_RECORD_OFF_ID, raw);
        out[off + VTP_CAN_RECORD_OFF_LEN] = fr->len;
        if (fr->len && fr->payload) {
            memcpy(out + off + VTP_CAN_RECORD_SIZE, fr->payload, fr->len);
        }
        off += VTP_CAN_RECORD_SIZE + fr->len;
    }
    return (int)off;
}

int vtp_encode_imu_batch(const vtp_imu_header_t *h,
                         const vtp_imu_sample_t *samples,
                         uint8_t *out, size_t cap) {
    const size_t needed =
        (size_t)VTP_IMU_HEADER_SIZE + (size_t)h->count * VTP_IMU_SAMPLE_SIZE;
    if (cap < needed) return -1;

    memset(out, 0, needed);
    wr16(out + VTP_IMU_HEADER_OFF_SEQ, h->seq);
    wr16(out + VTP_IMU_HEADER_OFF_DROPPED, h->dropped);
    wr64(out + VTP_IMU_HEADER_OFF_T_BASE, h->t_base);
    wr16(out + VTP_IMU_HEADER_OFF_PERIOD, h->period);
    out[VTP_IMU_HEADER_OFF_COUNT] = h->count;
    out[VTP_IMU_HEADER_OFF_FLAGS] = h->flags;

    const int accel = (h->flags & VTP_IMU_HAS_ACCEL) != 0;
    const int gyro = (h->flags & VTP_IMU_HAS_GYRO) != 0;

    for (uint8_t i = 0; i < h->count; i++) {
        uint8_t *p = out + VTP_IMU_HEADER_SIZE + (size_t)i * VTP_IMU_SAMPLE_SIZE;
        const vtp_imu_sample_t *s = &samples[i];
        /* A cleared presence flag means the sensor is absent, so its triple is
         * zero on the wire whatever the caller left in the struct. */
        wr16(p + VTP_IMU_SAMPLE_OFF_AX, accel ? (uint16_t)s->ax : 0);
        wr16(p + VTP_IMU_SAMPLE_OFF_AY, accel ? (uint16_t)s->ay : 0);
        wr16(p + VTP_IMU_SAMPLE_OFF_AZ, accel ? (uint16_t)s->az : 0);
        wr16(p + VTP_IMU_SAMPLE_OFF_GX, gyro ? (uint16_t)s->gx : 0);
        wr16(p + VTP_IMU_SAMPLE_OFF_GY, gyro ? (uint16_t)s->gy : 0);
        wr16(p + VTP_IMU_SAMPLE_OFF_GZ, gyro ? (uint16_t)s->gz : 0);
    }
    return (int)needed;
}

int vtp_encode_info(const vtp_info_t *v, uint8_t *out, size_t cap) {
    if (cap < VTP_INFO_SIZE) return -1;
    memset(out, 0, VTP_INFO_SIZE);

    out[VTP_INFO_OFF_PROTOCOL_MAJOR] = v->protocol_major;
    out[VTP_INFO_OFF_PROTOCOL_MINOR] = v->protocol_minor;
    wr32(out + VTP_INFO_OFF_CAPABILITIES, v->capabilities);
    wr16(out + VTP_INFO_OFF_GPS_RATE_HZ, v->gps_rate_hz);
    wr16(out + VTP_INFO_OFF_GPS_MAX_RATE_HZ, v->gps_max_rate_hz);
    wr16(out + VTP_INFO_OFF_CAN_SUBSCRIPTION_SLOTS, v->can_subscription_slots);
    wr32(out + VTP_INFO_OFF_CAN_MAX_FRAMES_PER_S, v->can_max_frames_per_s);
    wr16(out + VTP_INFO_OFF_IMU_RATE_HZ, v->imu_rate_hz);
    wr16(out + VTP_INFO_OFF_IMU_MAX_RATE_HZ, v->imu_max_rate_hz);
    out[VTP_INFO_OFF_CAN_MAX_PAYLOAD] = v->can_max_payload;
    out[VTP_INFO_OFF_CLOCK_FLAGS] = v->clock_flags;
    wr16(out + VTP_INFO_OFF_MAX_NOTIFY_BYTES, v->max_notify_bytes);
    return VTP_INFO_SIZE;
}
