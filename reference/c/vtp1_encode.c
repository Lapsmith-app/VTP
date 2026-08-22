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
    /* Reporting success for bytes that were never written is worse than
     * failing: the caller transmits whatever the buffer happened to hold. */
    if (ext_len && !ext) return -1;
    /* SPEC.md §5.4 -- ranges, where a validity bit claims a meaning. */
    if (f->validity & VTP_GPS_VALIDITY_POSITION) {
        if (f->lat > 900000000 || f->lat < -900000000) return -1;
        if (f->lon > 1800000000 || f->lon < -1800000000) return -1;
    }
    if ((f->validity & VTP_GPS_VALIDITY_HEAD_MOT)
        && (f->head_mot < 0 || f->head_mot >= 36000000)) return -1;
    /* SPEC.md §5.5 — the notification length MUST equal the base record plus
     * exactly the bytes accounted for by ext_count. An encoder that writes a
     * count disagreeing with its own payload emits something no conforming
     * decoder will accept, so it is refused here rather than on the wire. */
    {
        size_t off = 0;
        uint8_t n = 0;
        for (; n < f->ext_count; n++) {
            if (off + 2 > ext_len) return -1;
            off += (size_t)2 + ext[off + 1];
        }
        if (off != ext_len) return -1;
    }
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

/* SPEC.md §6.10 — mirrors the decoder's ladder. Duplicated rather than shared
 * because vtp1.c and vtp1_encode.c are separate translation units on purpose:
 * a client links only the decoder, a device only the encoder. */
static int vtp_fd_len_ok(uint8_t n) {
    if (n <= 8) return 1;
    switch (n) {
    case 12: case 16: case 20: case 24: case 32: case 48: case 64: return 1;
    default: return 0;
    }
}

int vtp_encode_can_batch(const vtp_can_header_t *h,
                         const vtp_can_frame_t *frames,
                         uint8_t *out, size_t cap) {
    /* SPEC.md §6.1 -- record 0's dt is zero by t_base's own definition. */
    if (h->count && frames[0].dt != 0) return -1;
    size_t needed = VTP_CAN_HEADER_SIZE;
    for (uint8_t i = 0; i < h->count; i++) {
        /* An encoder must not emit a frame its own decoder rejects: a device
         * that ships one has produced a notification no conforming client can
         * read, and it finds out from the field rather than from a test.
         * SPEC.md §6.4 and §6.10. */
        const vtp_can_frame_t *v = &frames[i];
        if (!v->extended && v->id > 0x7FFu)  return -1;
        if (v->fd && v->rtr)                 return -1;
        if (v->rtr && v->len)                return -1;
        if (!v->fd && v->len > 8)            return -1;
        if (v->fd && !vtp_fd_len_ok(v->len)) return -1;
        /* As above: a length with no payload behind it would be reported as
         * written and sent as uninitialised memory. */
        if (v->len && !v->payload) return -1;
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
    /* An encoder must not emit what its own decoder rejects. SPEC.md §7. */
    if (h->period == 0) return -1;
    if (cap < needed) return -1;

    memset(out, 0, needed);
    wr16(out + VTP_IMU_HEADER_OFF_SEQ, h->seq);
    wr16(out + VTP_IMU_HEADER_OFF_DROPPED, h->dropped);
    wr64(out + VTP_IMU_HEADER_OFF_T_BASE, h->t_base);
    wr32(out + VTP_IMU_HEADER_OFF_PERIOD, h->period);
    out[VTP_IMU_HEADER_OFF_COUNT] = h->count;
    out[VTP_IMU_HEADER_OFF_FLAGS] = h->flags;
    /* Written through, not zeroed, for the same reason as can_header.reserved:
     * a later minor may have been assigned these bytes. */
    wr16(out + VTP_IMU_HEADER_OFF_RESERVED, h->reserved);

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

int vtp_encode_monitor_list(const vtp_monitor_page_t *p,
                            const vtp_monitor_channel_t *entries,
                            uint8_t *out, size_t cap) {
    const size_t needed = (size_t)VTP_MONITOR_PAGE_SIZE
                        + (size_t)p->count * VTP_MONITOR_CHANNEL_SIZE;
    if (cap < needed) return -1;
    if (p->count && !entries) return -1;
    memset(out, 0, needed);

    wr16(out + VTP_MONITOR_PAGE_OFF_TOTAL, p->total);
    wr16(out + VTP_MONITOR_PAGE_OFF_INDEX, p->index);
    out[VTP_MONITOR_PAGE_OFF_COUNT] = p->count;
    out[VTP_MONITOR_PAGE_OFF_RESERVED] = p->reserved;

    for (uint8_t i = 0; i < p->count; i++) {
        uint8_t *e = out + VTP_MONITOR_PAGE_SIZE
                   + (size_t)i * VTP_MONITOR_CHANNEL_SIZE;
        e[VTP_MONITOR_CHANNEL_OFF_SLOT] = entries[i].slot;
        wr16(e + VTP_MONITOR_CHANNEL_OFF_CHANNEL, entries[i].channel);
        e[VTP_MONITOR_CHANNEL_OFF_MAX_AGE] = entries[i].max_age;
    }
    return (int)needed;
}

int vtp_encode_monitor_update(const vtp_monitor_header_t *h,
                              const vtp_monitor_value_t *values,
                              uint8_t *out, size_t cap) {
    const size_t needed = (size_t)VTP_MONITOR_HEADER_SIZE
                        + (size_t)h->count * VTP_MONITOR_VALUE_SIZE;
    if (cap < needed) return -1;
    if (h->count && !values) return -1;
    memset(out, 0, needed);

    wr16(out + VTP_MONITOR_HEADER_OFF_SEQ, h->seq);
    out[VTP_MONITOR_HEADER_OFF_COUNT] = h->count;
    out[VTP_MONITOR_HEADER_OFF_RESERVED] = h->reserved;

    for (uint8_t i = 0; i < h->count; i++) {
        uint8_t *e = out + VTP_MONITOR_HEADER_SIZE
                   + (size_t)i * VTP_MONITOR_VALUE_SIZE;
        e[VTP_MONITOR_VALUE_OFF_SLOT] = values[i].slot;
        e[VTP_MONITOR_VALUE_OFF_VALIDITY] = values[i].validity;
        /* A client that clears the present bit cannot also ship the value it
         * was hiding -- the same rule as everywhere else, in the one place the
         * protocol reverses direction. */
        wr32(e + VTP_MONITOR_VALUE_OFF_VALUE,
             gate32((uint32_t)values[i].value, values[i].validity,
                    VTP_MONITOR_VALIDITY_PRESENT));
    }
    return (int)needed;
}

int vtp_encode_can_list(const vtp_can_list_page_t *p,
                        const vtp_can_subscription_t *entries,
                        uint8_t *out, size_t cap) {
    const size_t needed = (size_t)VTP_CAN_LIST_PAGE_SIZE
                        + (size_t)p->count * VTP_CAN_SUBSCRIPTION_SIZE;
    if (cap < needed) return -1;
    if (p->count && !entries) return -1;
    memset(out, 0, needed);

    wr16(out + VTP_CAN_LIST_PAGE_OFF_TOTAL, p->total);
    wr16(out + VTP_CAN_LIST_PAGE_OFF_INDEX, p->index);
    out[VTP_CAN_LIST_PAGE_OFF_COUNT] = p->count;
    out[VTP_CAN_LIST_PAGE_OFF_RESERVED] = p->reserved;

    for (uint8_t i = 0; i < p->count; i++) {
        uint8_t *e = out + VTP_CAN_LIST_PAGE_SIZE
                   + (size_t)i * VTP_CAN_SUBSCRIPTION_SIZE;
        wr16(e + VTP_CAN_SUBSCRIPTION_OFF_HANDLE, entries[i].handle);
        wr32(e + VTP_CAN_SUBSCRIPTION_OFF_ID, entries[i].id);
        wr32(e + VTP_CAN_SUBSCRIPTION_OFF_MASK, entries[i].mask);
        e[VTP_CAN_SUBSCRIPTION_OFF_MODE] = entries[i].mode;
        wr16(e + VTP_CAN_SUBSCRIPTION_OFF_ARG, entries[i].arg);
    }
    return (int)needed;
}

int vtp_encode_link_params(const vtp_link_params_t *l, uint8_t *out, size_t cap) {
    if (cap < VTP_LINK_PARAMS_SIZE) return -1;
    memset(out, 0, VTP_LINK_PARAMS_SIZE);

    const uint32_t v = l->validity;

    wr16(out + VTP_LINK_PARAMS_OFF_VALIDITY, l->validity);
    wr16(out + VTP_LINK_PARAMS_OFF_ATT_MTU,
         (uint16_t)gate32(l->att_mtu, v, VTP_LINK_VALIDITY_ATT_MTU));
    wr16(out + VTP_LINK_PARAMS_OFF_LL_MAX_TX_OCTETS,
         (uint16_t)gate32(l->ll_max_tx_octets, v, VTP_LINK_VALIDITY_LL_DATA_LENGTH));
    wr16(out + VTP_LINK_PARAMS_OFF_LL_MAX_RX_OCTETS,
         (uint16_t)gate32(l->ll_max_rx_octets, v, VTP_LINK_VALIDITY_LL_DATA_LENGTH));
    wr16(out + VTP_LINK_PARAMS_OFF_CONN_INTERVAL,
         (uint16_t)gate32(l->conn_interval, v, VTP_LINK_VALIDITY_CONN_PARAMS));
    wr16(out + VTP_LINK_PARAMS_OFF_PERIPHERAL_LATENCY,
         (uint16_t)gate32(l->peripheral_latency, v, VTP_LINK_VALIDITY_CONN_PARAMS));
    wr16(out + VTP_LINK_PARAMS_OFF_SUPERVISION_TIMEOUT,
         (uint16_t)gate32(l->supervision_timeout, v, VTP_LINK_VALIDITY_CONN_PARAMS));
    out[VTP_LINK_PARAMS_OFF_PHY_TX] =
         (uint8_t)gate32(l->phy_tx, v, VTP_LINK_VALIDITY_PHY);
    out[VTP_LINK_PARAMS_OFF_PHY_RX] =
         (uint8_t)gate32(l->phy_rx, v, VTP_LINK_VALIDITY_PHY);
    return VTP_LINK_PARAMS_SIZE;
}

int vtp_encode_control_response(const vtp_control_response_t *r,
                                uint8_t *out, size_t cap) {
    /* An encoder must not emit what its own decoder rejects. SPEC.md §9. */
    if (r->detail_len && r->status != VTP_STATUS_OK) return -1;
    if (r->detail_len && !r->detail) return -1;
    const size_t needed = VTP_CONTROL_RESPONSE_SIZE + r->detail_len;
    if (cap < needed) return -1;

    out[VTP_CONTROL_RESPONSE_OFF_OPCODE] = r->opcode;
    out[VTP_CONTROL_RESPONSE_OFF_TAG]    = r->tag;
    out[VTP_CONTROL_RESPONSE_OFF_STATUS] = r->status;
    if (r->detail_len) {
        memcpy(out + VTP_CONTROL_RESPONSE_SIZE, r->detail, r->detail_len);
    }
    return (int)needed;
}

int vtp_encode_time_sync(const vtp_time_sync_t *t, uint8_t *out, size_t cap) {
    /* An encoder must not emit what its own decoder rejects. SPEC.md §9.7. */
    if (t->t_device_tx < t->t_device_rx) return -1;
    if (cap < VTP_TIME_SYNC_SIZE) return -1;
    wr64(out + VTP_TIME_SYNC_OFF_T_DEVICE_RX, t->t_device_rx);
    wr64(out + VTP_TIME_SYNC_OFF_T_DEVICE_TX, t->t_device_tx);
    return VTP_TIME_SYNC_SIZE;
}
