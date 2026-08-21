#include "vtp1.h"
#include <string.h>

/* Little-endian readers. Written as byte assembly rather than a cast so the
 * decoder is correct on a big-endian host and on unaligned buffers alike. */
static uint16_t rd16(const uint8_t *p) {
    return (uint16_t)(p[0] | ((uint16_t)p[1] << 8));
}
static uint32_t rd32(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}
static uint64_t rd64(const uint8_t *p) {
    return (uint64_t)rd32(p) | ((uint64_t)rd32(p + 4) << 32);
}

int vtp_fix_type_known(uint8_t t) {
    switch (t) {
        case VTP_FIX_TYPE_NONE:
        case VTP_FIX_TYPE_DEAD_RECKON:
        case VTP_FIX_TYPE_FIX_2D:
        case VTP_FIX_TYPE_FIX_3D:
        case VTP_FIX_TYPE_GNSS_DR:
        case VTP_FIX_TYPE_TIME_ONLY:
            return 1;
        default:
            return 0;   /* A future minor's value. Stays unknown. SPEC.md §11.3 */
    }
}

int vtp_decode_gps_fix(const uint8_t *b, size_t len,
                       vtp_gps_fix_t *o, const char **err) {
    if (len < VTP_GPS_FIX_SIZE) { *err = "length"; return -1; }

    o->seq           = rd16(b + VTP_GPS_FIX_OFF_SEQ);
    o->dropped       = rd16(b + VTP_GPS_FIX_OFF_DROPPED);
    o->validity      = rd32(b + VTP_GPS_FIX_OFF_VALIDITY);
    o->t_device      = rd64(b + VTP_GPS_FIX_OFF_T_DEVICE);
    o->t_utc         = (int64_t)rd64(b + VTP_GPS_FIX_OFF_T_UTC);
    o->lat           = (int32_t)rd32(b + VTP_GPS_FIX_OFF_LAT);
    o->lon           = (int32_t)rd32(b + VTP_GPS_FIX_OFF_LON);
    o->alt_msl       = (int32_t)rd32(b + VTP_GPS_FIX_OFF_ALT_MSL);
    o->alt_ellipsoid = (int32_t)rd32(b + VTP_GPS_FIX_OFF_ALT_ELLIPSOID);
    o->vel_n         = (int32_t)rd32(b + VTP_GPS_FIX_OFF_VEL_N);
    o->vel_e         = (int32_t)rd32(b + VTP_GPS_FIX_OFF_VEL_E);
    o->vel_d         = (int32_t)rd32(b + VTP_GPS_FIX_OFF_VEL_D);
    o->head_mot      = (int32_t)rd32(b + VTP_GPS_FIX_OFF_HEAD_MOT);
    o->h_acc         = rd32(b + VTP_GPS_FIX_OFF_H_ACC);
    o->v_acc         = rd32(b + VTP_GPS_FIX_OFF_V_ACC);
    o->s_acc         = rd32(b + VTP_GPS_FIX_OFF_S_ACC);
    o->p_dop         = rd16(b + VTP_GPS_FIX_OFF_P_DOP);
    o->fix_type      = b[VTP_GPS_FIX_OFF_FIX_TYPE];
    o->num_sv        = b[VTP_GPS_FIX_OFF_NUM_SV];
    o->fix_flags     = b[VTP_GPS_FIX_OFF_FIX_FLAGS];
    o->ext_count     = b[VTP_GPS_FIX_OFF_EXT_COUNT];

    /* SPEC.md §5.5: the length MUST equal the base record plus exactly the
     * bytes ext_count accounts for. Trailing bytes nothing declared are a
     * different protocol on this characteristic, not a longer fix. */
    size_t off = VTP_GPS_FIX_SIZE;
    for (uint8_t i = 0; i < o->ext_count; i++) {
        if (off + 2 > len) { *err = "ext-truncated"; return -1; }
        size_t ext_len = b[off + 1];
        if (off + 2 + ext_len > len) { *err = "ext-truncated"; return -1; }
        off += 2 + ext_len;
    }
    if (off != len) { *err = "length"; return -1; }

    o->ext_offset = VTP_GPS_FIX_SIZE;
    o->ext_bytes  = off - VTP_GPS_FIX_SIZE;
    return 0;
}

int vtp_can_batch_begin(const uint8_t *b, size_t len,
                        vtp_can_header_t *h, vtp_can_iter_t *it,
                        const char **err) {
    if (len < VTP_CAN_HEADER_SIZE) { *err = "length"; return -1; }

    h->seq      = rd16(b + VTP_CAN_HEADER_OFF_SEQ);
    h->dropped  = rd16(b + VTP_CAN_HEADER_OFF_DROPPED);
    h->t_base   = rd64(b + VTP_CAN_HEADER_OFF_T_BASE);
    h->count    = b[VTP_CAN_HEADER_OFF_COUNT];
    h->flags    = b[VTP_CAN_HEADER_OFF_FLAGS];
    h->reserved = rd16(b + VTP_CAN_HEADER_OFF_RESERVED);
    /* h->reserved is carried, never validated: SPEC.md §2 requires unknown
     * reserved content to be ignored, not rejected. A later minor may assign
     * it, and this build must keep working when it does. */

    /* Walk the whole batch before yielding anything, so a truncated trailing
     * record rejects the notification instead of half-decoding it. */
    size_t off = VTP_CAN_HEADER_SIZE;
    for (uint8_t i = 0; i < h->count; i++) {
        if (off + VTP_CAN_RECORD_SIZE > len) { *err = "truncated-record"; return -1; }
        size_t plen = b[off + VTP_CAN_RECORD_OFF_LEN];
        if (plen > 64) { *err = "bad-length"; return -1; }
        {
            /* SPEC.md §6.4 — frames that cannot exist are rejected, not
             * repaired. Truncating an over-long standard identifier would
             * yield a different identifier that looks entirely valid. */
            const uint32_t raw = rd32(b + off + VTP_CAN_RECORD_OFF_ID);
            const int ext = (raw & (1u << 29)) != 0;
            const int fd  = (raw & (1u << 30)) != 0;
            const int rtr = (raw & (1u << 31)) != 0;
            if (!ext && (raw & 0x1FFFFFFFu) > 0x7FFu) {
                *err = "bad-standard-id"; return -1;
            }
            if (fd && rtr)   { *err = "fd-rtr"; return -1; }
            if (rtr && plen) { *err = "rtr-with-payload"; return -1; }
        }
        if (off + VTP_CAN_RECORD_SIZE + plen > len) { *err = "truncated-record"; return -1; }
        off += VTP_CAN_RECORD_SIZE + plen;
    }
    if (off != len) { *err = "length"; return -1; }

    it->p         = b + VTP_CAN_HEADER_SIZE;
    it->remaining = len - VTP_CAN_HEADER_SIZE;
    it->left      = h->count;
    it->t_base    = h->t_base;
    return 0;
}

int vtp_can_iter_next(vtp_can_iter_t *it, vtp_can_frame_t *o) {
    if (it->left == 0) return 0;

    const uint8_t *p = it->p;
    o->dt  = rd16(p + VTP_CAN_RECORD_OFF_DT);
    uint32_t raw = rd32(p + VTP_CAN_RECORD_OFF_ID);
    o->id       = raw & 0x1FFFFFFFu;
    o->extended = (raw & (1u << 29)) != 0;
    o->fd       = (raw & (1u << 30)) != 0;
    o->rtr      = (raw & (1u << 31)) != 0;
    o->len      = p[VTP_CAN_RECORD_OFF_LEN];
    o->payload  = p + VTP_CAN_RECORD_SIZE;
    /* dt counts 10 us ticks — SPEC.md §6.1. */
    o->t_device = it->t_base + (uint64_t)o->dt * 10u;

    size_t step = VTP_CAN_RECORD_SIZE + o->len;
    it->p += step;
    it->remaining -= step;
    it->left--;
    return 1;
}

int vtp_decode_imu_batch(const uint8_t *b, size_t len,
                         vtp_imu_header_t *h, const char **err) {
    if (len < VTP_IMU_HEADER_SIZE) { *err = "length"; return -1; }

    h->seq     = rd16(b + VTP_IMU_HEADER_OFF_SEQ);
    h->dropped = rd16(b + VTP_IMU_HEADER_OFF_DROPPED);
    h->t_base  = rd64(b + VTP_IMU_HEADER_OFF_T_BASE);
    h->period  = rd32(b + VTP_IMU_HEADER_OFF_PERIOD);
    h->count   = b[VTP_IMU_HEADER_OFF_COUNT];
    h->flags   = b[VTP_IMU_HEADER_OFF_FLAGS];
    h->reserved = rd16(b + VTP_IMU_HEADER_OFF_RESERVED);

    if (len != (size_t)VTP_IMU_HEADER_SIZE +
               (size_t)h->count * VTP_IMU_SAMPLE_SIZE) {
        *err = "length";
        return -1;
    }
    return 0;
}

void vtp_imu_sample_at(const uint8_t *b, const vtp_imu_header_t *h,
                       uint8_t i, vtp_imu_sample_t *o) {
    const uint8_t *p = b + VTP_IMU_HEADER_SIZE + (size_t)i * VTP_IMU_SAMPLE_SIZE;
    o->ax = (int16_t)rd16(p + VTP_IMU_SAMPLE_OFF_AX);
    o->ay = (int16_t)rd16(p + VTP_IMU_SAMPLE_OFF_AY);
    o->az = (int16_t)rd16(p + VTP_IMU_SAMPLE_OFF_AZ);
    o->gx = (int16_t)rd16(p + VTP_IMU_SAMPLE_OFF_GX);
    o->gy = (int16_t)rd16(p + VTP_IMU_SAMPLE_OFF_GY);
    o->gz = (int16_t)rd16(p + VTP_IMU_SAMPLE_OFF_GZ);
    /* Samples are evenly spaced — SPEC.md §7. */
    o->t_device = h->t_base + (uint64_t)i * h->period;
}

int vtp_decode_info(const uint8_t *b, size_t len,
                    vtp_info_t *o, const char **err) {
    if (len != VTP_INFO_SIZE) { *err = "length"; return -1; }

    o->protocol_major         = b[VTP_INFO_OFF_PROTOCOL_MAJOR];
    o->protocol_minor         = b[VTP_INFO_OFF_PROTOCOL_MINOR];
    o->capabilities           = rd32(b + VTP_INFO_OFF_CAPABILITIES);
    o->gps_rate_hz            = rd16(b + VTP_INFO_OFF_GPS_RATE_HZ);
    o->gps_max_rate_hz        = rd16(b + VTP_INFO_OFF_GPS_MAX_RATE_HZ);
    o->can_subscription_slots = rd16(b + VTP_INFO_OFF_CAN_SUBSCRIPTION_SLOTS);
    o->can_max_frames_per_s   = rd32(b + VTP_INFO_OFF_CAN_MAX_FRAMES_PER_S);
    o->imu_rate_hz            = rd16(b + VTP_INFO_OFF_IMU_RATE_HZ);
    o->imu_max_rate_hz        = rd16(b + VTP_INFO_OFF_IMU_MAX_RATE_HZ);
    o->can_max_payload        = b[VTP_INFO_OFF_CAN_MAX_PAYLOAD];
    o->clock_flags            = b[VTP_INFO_OFF_CLOCK_FLAGS];
    o->max_notify_bytes       = rd16(b + VTP_INFO_OFF_MAX_NOTIFY_BYTES);
    return 0;
}

int vtp_sub_mode_known(uint8_t m) {
    switch (m) {
        case VTP_SUB_MODE_EVERY_FRAME:
        case VTP_SUB_MODE_PERIODIC:
        case VTP_SUB_MODE_ON_CHANGE:
        case VTP_SUB_MODE_EVERY_NTH:
            return 1;
        default:
            return 0;   /* A later minor's mode. Stays unknown. SPEC.md §11.3 */
    }
}

int vtp_decode_can_list(const uint8_t *b, size_t len,
                        vtp_can_list_page_t *p, const char **err) {
    if (len < VTP_CAN_LIST_PAGE_SIZE) { *err = "length"; return -1; }

    p->total    = rd16(b + VTP_CAN_LIST_PAGE_OFF_TOTAL);
    p->index    = rd16(b + VTP_CAN_LIST_PAGE_OFF_INDEX);
    p->count    = b[VTP_CAN_LIST_PAGE_OFF_COUNT];
    p->reserved = b[VTP_CAN_LIST_PAGE_OFF_RESERVED];

    const size_t needed = (size_t)VTP_CAN_LIST_PAGE_SIZE
                        + (size_t)p->count * VTP_CAN_SUBSCRIPTION_SIZE;
    if (len < needed) { *err = "truncated-record"; return -1; }
    if (len != needed) { *err = "length"; return -1; }
    return 0;
}

void vtp_can_subscription_at(const uint8_t *b, uint8_t index,
                             vtp_can_subscription_t *o) {
    const uint8_t *e = b + VTP_CAN_LIST_PAGE_SIZE
                     + (size_t)index * VTP_CAN_SUBSCRIPTION_SIZE;
    o->handle = rd16(e + VTP_CAN_SUBSCRIPTION_OFF_HANDLE);
    o->id     = rd32(e + VTP_CAN_SUBSCRIPTION_OFF_ID);
    o->mask   = rd32(e + VTP_CAN_SUBSCRIPTION_OFF_MASK);
    o->mode   = e[VTP_CAN_SUBSCRIPTION_OFF_MODE];
    o->arg    = rd16(e + VTP_CAN_SUBSCRIPTION_OFF_ARG);
}

int vtp_phy_known(uint8_t p) {
    switch (p) {
        case VTP_PHY_LE_1M:
        case VTP_PHY_LE_2M:
        case VTP_PHY_LE_CODED:
            return 1;
        default:
            return 0;   /* A future Bluetooth revision's PHY. Stays unknown. */
    }
}

int vtp_decode_link_params(const uint8_t *b, size_t len,
                           vtp_link_params_t *o, const char **err) {
    /* Fixed size, no extension mechanism: any other length is malformed. */
    if (len != VTP_LINK_PARAMS_SIZE) { *err = "length"; return -1; }

    o->validity            = rd16(b + VTP_LINK_PARAMS_OFF_VALIDITY);
    o->att_mtu             = rd16(b + VTP_LINK_PARAMS_OFF_ATT_MTU);
    o->ll_max_tx_octets    = rd16(b + VTP_LINK_PARAMS_OFF_LL_MAX_TX_OCTETS);
    o->ll_max_rx_octets    = rd16(b + VTP_LINK_PARAMS_OFF_LL_MAX_RX_OCTETS);
    o->conn_interval       = rd16(b + VTP_LINK_PARAMS_OFF_CONN_INTERVAL);
    o->peripheral_latency  = rd16(b + VTP_LINK_PARAMS_OFF_PERIPHERAL_LATENCY);
    o->supervision_timeout = rd16(b + VTP_LINK_PARAMS_OFF_SUPERVISION_TIMEOUT);
    o->phy_tx              = b[VTP_LINK_PARAMS_OFF_PHY_TX];
    o->phy_rx              = b[VTP_LINK_PARAMS_OFF_PHY_RX];
    return 0;
}
