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

/* SPEC.md §2 -- reserved bits are ZERO on transmit. Whole reserved FIELDS were
 * already forced to zero here; the reserved PORTION of a bitmask was not, so a
 * caller handing this encoder a capabilities word with bit 19 set, or a gps
 * validity word with bit 30 set, had it transmitted verbatim.
 *
 * Every conforming receiver is required to ignore those bits, which is exactly
 * why writing them is forbidden: they are the only bits on the wire a later
 * minor version may redefine, and a 1.0 device that sets one has published a
 * claim it cannot make and that nothing can retract.
 *
 * The masks are generated from schema/vtp1.yaml, so a bit assigned in a later
 * minor leaves the reserved region by editing the schema and nothing else. */
#define KNOWN_BITS(value, mask) ((value) & (uint32_t)(mask))

/* EVERY bitmask field on the wire goes through KNOWN_BITS. The Python encoder
 * applies this generically in `_pack`, walking the schema; this one is written
 * out per field because it is a separate translation unit with no reflection,
 * and that asymmetry is exactly how three fields came to be missed --
 * can_header.flags, imu_header.flags and info.clock_flags were transmitted
 * verbatim while Python masked them, so the two references produced DIFFERENT
 * BYTES from the same input. conformance/produce.py now carries a
 * reserved-bit case for every bitmask field in the schema, generated from the
 * schema, so a field added later cannot be forgotten here in silence. */

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
    /* SPEC.md 5.3 -- a carrier-phase solution has either resolved its
     * integer ambiguities or it has not, so both RTK bits at once is a claim about
     * solution quality that means nothing. The natural client reading of the pair is
     * "fixed wins", which upgrades a device's accuracy claim on the strength of a
     * bug. And either RTK bit implies `differential`, because an RTK solution IS a
     * differentially corrected one. */
    {
        const uint8_t rtk = f->fix_flags
            & (VTP_FIX_FLAGS_RTK_FLOAT | VTP_FIX_FLAGS_RTK_FIXED);
        if (rtk == (VTP_FIX_FLAGS_RTK_FLOAT | VTP_FIX_FLAGS_RTK_FIXED)) return -1;
        if (rtk && !(f->fix_flags & VTP_FIX_FLAGS_DIFFERENTIAL)) return -1;
    }
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

    /* Reserved bits go no further than this line: gating below reads the
     * caller's word, and the wire gets the normalised one. */
    const uint32_t v = KNOWN_BITS(f->validity, VTP_GPS_VALIDITY_KNOWN);

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
    out[VTP_GPS_FIX_OFF_FIX_FLAGS] =
        (uint8_t)KNOWN_BITS(f->fix_flags, VTP_FIX_FLAGS_KNOWN);
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

/* SPEC.md §2 -- a reserved field is ZERO on transmit. These used to write the
 * caller's value through, reasoning that a later minor might have been
 * assigned the bytes. But this is a 1.0 encoder: a build that knows what those
 * bytes mean is a build that names them, and until then writing them through
 * simply let a caller put arbitrary content into a field every conforming
 * receiver is required to ignore. */

int vtp_encode_can_batch(const vtp_can_header_t *h,
                         const vtp_can_frame_t *frames,
                         uint8_t *out, size_t cap) {
    /* Before anything reads through it. A count with no array behind it used
     * to reach frames[0].dt on the very next line and take the process down;
     * a producer that segfaults on malformed input is not a producer that
     * refused it, and refusing is the contract this file exists to keep. */
    if (h->count && !frames) return -1;
    /* SPEC.md §6.2 -- t_base names record 0, so there is always a record 0. */
    if (h->count == 0) return -1;
    /* SPEC.md §6.1 -- record 0's dt is zero by t_base's own definition. */
    if (frames[0].dt != 0) return -1;
    size_t needed = VTP_CAN_HEADER_SIZE;
    for (uint8_t i = 0; i < h->count; i++) {
        /* An encoder must not emit a frame its own decoder rejects: a device
         * that ships one has produced a notification no conforming client can
         * read, and it finds out from the field rather than from a test.
         * SPEC.md §6.4 and §6.10. */
        const vtp_can_frame_t *v = &frames[i];
        /* An identifier outside the arbitration field is not one to be
         * trimmed to fit: masking made 0x3FFFFFFF into 0x1FFFFFFF, a frame
         * the caller never asked for, on the field a client uses to decide
         * what the bytes mean. Checked HERE, with the rest of validation,
         * rather than in the write loop below -- the header had already been
         * written by then, so a refused batch left the output buffer modified
         * and contradicted this file's own "nothing written on -1" contract. */
        if (v->id > 0x1FFFFFFFu)             return -1;
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
    out[VTP_CAN_HEADER_OFF_FLAGS] =
        (uint8_t)KNOWN_BITS(h->flags, VTP_CAN_FLAGS_KNOWN);
    /* Reserved bytes are written through rather than forced to zero: a device
     * built against a later minor may have been assigned them, and this
     * encoder must not silently erase a field it does not know about. */
    wr16(out + VTP_CAN_HEADER_OFF_RESERVED, 0);   /* §2 */

    size_t off = VTP_CAN_HEADER_SIZE;
    for (uint8_t i = 0; i < h->count; i++) {
        const vtp_can_frame_t *fr = &frames[i];
        uint32_t raw = fr->id;
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
    if (h->count == 0) return -1;      /* t_base names a sample 0 that is absent */
    /* A count with no array behind it. The write loop only dereferenced a
     * sample through a set presence flag, so this crashed for an accel batch
     * and quietly emitted zeroed samples for a batch with neither flag --
     * two different wrong answers to one malformed call. SPEC.md §7.1. */
    if (h->count && !samples) return -1;
    if (cap < needed) return -1;

    memset(out, 0, needed);
    wr16(out + VTP_IMU_HEADER_OFF_SEQ, h->seq);
    wr16(out + VTP_IMU_HEADER_OFF_DROPPED, h->dropped);
    wr64(out + VTP_IMU_HEADER_OFF_T_BASE, h->t_base);
    wr32(out + VTP_IMU_HEADER_OFF_PERIOD, h->period);
    out[VTP_IMU_HEADER_OFF_COUNT] = h->count;
    out[VTP_IMU_HEADER_OFF_FLAGS] =
        (uint8_t)KNOWN_BITS(h->flags, VTP_IMU_FLAGS_KNOWN);
    /*
     * a later minor may have been assigned these bytes. */
    wr16(out + VTP_IMU_HEADER_OFF_RESERVED, 0);   /* §2 */

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

/* SPEC.md §4.1 -- every capability bit set here brings the bits it requires. */
static int capabilities_coherent(uint32_t caps) {
    static const vtp_capability_rule_t rules[] = VTP_CAPABILITY_RULES;
    for (size_t i = 0; i < VTP_CAPABILITY_RULE_COUNT; i++) {
        if (!(caps & rules[i].bit)) continue;
        if ((caps & rules[i].requires_) != rules[i].requires_) return 0;
    }
    return 1;
}

int vtp_encode_info(const vtp_info_t *v, uint8_t *out, size_t cap) {
    if (cap < VTP_INFO_SIZE) return -1;
    const uint32_t caps = KNOWN_BITS(v->capabilities, VTP_CAPABILITIES_KNOWN);

    /* SPEC.md §4.1 -- an encoder must not emit what its own decoder rejects,
     * and the profile matrix is now something the decoder rejects. Checked
     * against the NORMALISED word, because that is what goes on the wire.
     *
     * The loop is duplicated from vtp1.c for the same reason vtp_fd_len_ok is:
     * these are separate translation units on purpose, and a device links only
     * this one. What is NOT duplicated is the rule -- VTP_CAPABILITY_RULES is
     * generated from schema/vtp1.yaml, so both copies read the same table and
     * neither can drift from the specification. */
    if (!capabilities_coherent(caps)) return -1;

    /* Built in a scratch record first, so the capacity sweep below can run
     * over the same generated table the decoder's coherence check loops --
     * per-field hand code here was the one implementation the schema could
     * not update, so the next role added to profile.capacity would have
     * split the C encoder from everything else. Nothing is written to `out`
     * until every rule has passed, keeping the nothing-on-minus-one
     * contract. */
    uint8_t tmp[VTP_INFO_SIZE];
    memset(tmp, 0, sizeof tmp);
    tmp[VTP_INFO_OFF_PROTOCOL_MAJOR] = v->protocol_major;
    tmp[VTP_INFO_OFF_PROTOCOL_MINOR] = v->protocol_minor;
    wr32(tmp + VTP_INFO_OFF_CAPABILITIES, caps);
    wr16(tmp + VTP_INFO_OFF_GPS_RATE_HZ, v->gps_rate_hz);
    wr16(tmp + VTP_INFO_OFF_GPS_MAX_RATE_HZ, v->gps_max_rate_hz);
    wr16(tmp + VTP_INFO_OFF_CAN_SUBSCRIPTION_SLOTS, v->can_subscription_slots);
    wr32(tmp + VTP_INFO_OFF_CAN_MAX_FRAMES_PER_S, v->can_max_frames_per_s);
    wr16(tmp + VTP_INFO_OFF_IMU_RATE_HZ, v->imu_rate_hz);
    wr16(tmp + VTP_INFO_OFF_IMU_MAX_RATE_HZ, v->imu_max_rate_hz);
    tmp[VTP_INFO_OFF_OBD_POLL_SLOTS] = v->obd_poll_slots;
    tmp[VTP_INFO_OFF_CLOCK_FLAGS] =
        (uint8_t)KNOWN_BITS(v->clock_flags, VTP_CLOCK_FLAGS_KNOWN);
    wr16(tmp + VTP_INFO_OFF_OBD_MIN_INTERVAL_MS, v->obd_min_interval_ms);

    /* SPEC.md 4.1 -- a capacity behind a cleared bit is a role the device
     * does not have (sharpest for OBD, where it advertises transmitting on
     * a vehicle bus while declaring not to). Driven by the generated table
     * so a role added in the schema is enforced here without an edit. */
    {
        static const vtp_capacity_rule_t rules[] = VTP_CAPACITY_RULES;
        for (size_t i = 0; i < VTP_CAPACITY_RULE_COUNT; i++) {
            if (caps & rules[i].bit) continue;
            uint32_t val = 0;
            for (uint8_t k = 0; k < rules[i].size; k++)
                val |= (uint32_t)tmp[rules[i].offset + k] << (8 * k);
            if (val) return -1;
        }
    }

    memcpy(out, tmp, VTP_INFO_SIZE);
    return VTP_INFO_SIZE;
}

int vtp_encode_monitor_list(const vtp_monitor_declaration_t *p,
                            const vtp_monitor_channel_t *entries,
                            uint8_t *out, size_t cap) {
    const size_t needed = (size_t)VTP_MONITOR_DECLARATION_SIZE
                        + (size_t)p->count * VTP_MONITOR_CHANNEL_SIZE;
    if (cap < needed) return -1;
    /* The array first: the duplicate-slot sweep below reads through it, and
     * reading through a null one is a crash rather than the refusal this
     * function documents. It used to sit after the sweep. */
    if (p->count && !entries) return -1;
    /* SPEC.md §13.3, §13.4 -- both already enforced by the decoder, so
     * emitting either produced a declaration this repository's own reader
     * refuses to read. */
    if (p->count > VTP_MONITOR_MAX_CHANNELS) return -1;
    for (uint8_t i = 0; i < p->count; i++)
        for (uint8_t j = (uint8_t)(i + 1); j < p->count; j++)
            if (entries[i].slot == entries[j].slot) return -1;
    /* SPEC.md §13.5 -- every declared channel carries a deadline. */
    for (uint8_t i = 0; i < p->count; i++)
        if (entries[i].max_age == 0) return -1;
    memset(out, 0, needed);

    out[VTP_MONITOR_DECLARATION_OFF_COUNT] = p->count;
    out[VTP_MONITOR_DECLARATION_OFF_RESERVED] = 0;   /* §2 */

    for (uint8_t i = 0; i < p->count; i++) {
        uint8_t *e = out + VTP_MONITOR_DECLARATION_SIZE
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
    /* As in monitor_list: the array is checked before anything reads through
     * it, not after. */
    if (h->count && !values) return -1;
    /* SPEC.md §13.4 -- a write with no values is not a complete statement. */
    if (h->count == 0) return -1;
    /* SPEC.md §13.4 -- a slot twice, and nothing says which wins. */
    for (uint8_t i = 0; i < h->count; i++)
        for (uint8_t j = (uint8_t)(i + 1); j < h->count; j++)
            if (values[i].slot == values[j].slot) return -1;
    memset(out, 0, needed);

    wr16(out + VTP_MONITOR_HEADER_OFF_SEQ, h->seq);
    out[VTP_MONITOR_HEADER_OFF_COUNT] = h->count;
    out[VTP_MONITOR_HEADER_OFF_RESERVED] = 0;   /* §2 */

    for (uint8_t i = 0; i < h->count; i++) {
        uint8_t *e = out + VTP_MONITOR_HEADER_SIZE
                   + (size_t)i * VTP_MONITOR_VALUE_SIZE;
        const uint8_t validity =
            (uint8_t)KNOWN_BITS(values[i].validity, VTP_MONITOR_VALIDITY_KNOWN);
        e[VTP_MONITOR_VALUE_OFF_SLOT] = values[i].slot;
        e[VTP_MONITOR_VALUE_OFF_VALIDITY] = validity;
        /* A client that clears the present bit cannot also ship the value it
         * was hiding -- the same rule as everywhere else, in the one place the
         * protocol reverses direction. */
        wr32(e + VTP_MONITOR_VALUE_OFF_VALUE,
             gate32((uint32_t)values[i].value, validity,
                    VTP_MONITOR_VALIDITY_PRESENT));
    }
    return (int)needed;
}

int vtp_encode_gnss_aid_caps(const vtp_gnss_aid_caps_t *c, uint8_t *out, size_t cap) {
    if (cap < VTP_GNSS_AID_CAPS_SIZE) return -1;
    memset(out, 0, VTP_GNSS_AID_CAPS_SIZE);

    const uint32_t v = KNOWN_BITS(c->validity, VTP_AID_VALIDITY_KNOWN);

    out[VTP_GNSS_AID_CAPS_OFF_VALIDITY] = (uint8_t)v;
    out[VTP_GNSS_AID_CAPS_OFF_FORMAT]   = c->format;
    wr32(out + VTP_GNSS_AID_CAPS_OFF_MAX_BYTES, c->max_bytes);
    wr64(out + VTP_GNSS_AID_CAPS_OFF_HELD_UNTIL,
         (uint64_t)gate64((uint64_t)c->held_until, v, VTP_AID_VALIDITY_HELD_UNTIL));
    return VTP_GNSS_AID_CAPS_SIZE;
}

int vtp_encode_aid_begin_result(const vtp_aid_begin_result_t *b, uint8_t *out, size_t cap) {
    if (cap < VTP_AID_BEGIN_RESULT_SIZE) return -1;
    /* SPEC.md 14.3 -- MUST NOT be zero. A transfer that cannot carry a byte,
     * and indistinguishable to the client from a device that will not say: it
     * writes chunks of nothing until the commit reports everything missing. */
    if (b->chunk_bytes == 0) return -1;
    memset(out, 0, VTP_AID_BEGIN_RESULT_SIZE);

    out[VTP_AID_BEGIN_RESULT_OFF_TOKEN] = b->token;
    wr16(out + VTP_AID_BEGIN_RESULT_OFF_CHUNK_BYTES, b->chunk_bytes);
    return VTP_AID_BEGIN_RESULT_SIZE;
}

int vtp_encode_aid_commit_result(const vtp_aid_commit_result_t *c, uint8_t *out, size_t cap) {
    if (cap < VTP_AID_COMMIT_RESULT_SIZE) return -1;

    const uint32_t v = KNOWN_BITS(c->validity, VTP_COMMIT_VALIDITY_KNOWN);

    /* SPEC.md 14.4 -- set if and only if the result is `incomplete`. Set
     * beside any other result it names a chunk as lost from a transfer that
     * lost none; clear beside `incomplete` it says something is missing and
     * refuses to say what, which is the one thing that makes a
     * write-without-response path recoverable.
     *
     * The enum VALUE is deliberately not checked: SPEC.md 11.4 lets a minor
     * version add results, and the corpus carries an unknown one on purpose. */
    const int named = (v & VTP_COMMIT_VALIDITY_FIRST_MISSING) != 0;
    if (named != (c->result == VTP_AID_RESULT_INCOMPLETE)) return -1;

    memset(out, 0, VTP_AID_COMMIT_RESULT_SIZE);

    out[VTP_AID_COMMIT_RESULT_OFF_VALIDITY] = (uint8_t)v;
    out[VTP_AID_COMMIT_RESULT_OFF_RESULT]   = c->result;
    wr16(out + VTP_AID_COMMIT_RESULT_OFF_FIRST_MISSING,
         (uint16_t)gate32(c->first_missing, v, VTP_COMMIT_VALIDITY_FIRST_MISSING));
    return VTP_AID_COMMIT_RESULT_SIZE;
}

int vtp_encode_power_state(const vtp_power_state_t *p, uint8_t *out, size_t cap) {
    /* The device side of SPEC.md 9.7's range rule: a percent above 100 is
     * refused here, and deliberately NOT rejected by the decoder -- the
     * record is well formed, so a receiver decodes it and flags the value. */
    if ((p->validity & VTP_POWER_VALIDITY_PERCENT) && p->percent > 100) return -1;
    if (cap < VTP_POWER_STATE_SIZE) return -1;
    memset(out, 0, VTP_POWER_STATE_SIZE);

    const uint32_t v = KNOWN_BITS(p->validity, VTP_POWER_VALIDITY_KNOWN);

    out[VTP_POWER_STATE_OFF_VALIDITY] = (uint8_t)v;
    out[VTP_POWER_STATE_OFF_SOURCE] =
         (uint8_t)gate32(p->source, v, VTP_POWER_VALIDITY_SOURCE);
    out[VTP_POWER_STATE_OFF_PERCENT] =
         (uint8_t)gate32(p->percent, v, VTP_POWER_VALIDITY_PERCENT);
    return VTP_POWER_STATE_SIZE;
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
    /* An encoder must not emit what its own decoder rejects. SPEC.md §9.5. */
    if (t->t_device_tx < t->t_device_rx) return -1;
    if (cap < VTP_TIME_SYNC_SIZE) return -1;
    wr64(out + VTP_TIME_SYNC_OFF_T_DEVICE_RX, t->t_device_rx);
    wr64(out + VTP_TIME_SYNC_OFF_T_DEVICE_TX, t->t_device_tx);
    return VTP_TIME_SYNC_SIZE;
}

int vtp_encode_obd_info(const vtp_obd_probe_t *p,
                        const vtp_obd_ecu_t *ecus,
                        uint8_t *out, size_t cap) {
    const size_t needed = (size_t)VTP_OBD_PROBE_SIZE
                        + (size_t)p->count * VTP_OBD_ECU_SIZE;
    if (cap < needed) return -1;
    /* The array first, before any sweep reads through it: a count with no
     * array behind it is a refusal, never a dereference. */
    if (p->count && !ecus) return -1;

    const uint32_t v = KNOWN_BITS(p->validity, VTP_OBD_VALIDITY_KNOWN);
    const int responded = (v & VTP_OBD_VALIDITY_RESPONDED) != 0;

    /* SPEC.md 15.2's content rules, which the decoder deliberately accepts:
     * the refusals are the device-side half of each. `responded` set with no
     * entries says something answered and lists nothing that did; an entry
     * behind a silent probe is the reverse; ISO 15765-4 caps the responders
     * to a functional request at eight; and the entry list is strictly
     * ascending over bits 0-29, so one ECU cannot appear to be two and two
     * conforming devices probing one car produce identical bytes. */
    if (responded && p->count == 0) return -1;
    if (!responded && p->count != 0) return -1;
    if (p->count > 8) return -1;
    /* SPEC.md 15.2 -- refused, never masked, for 6.4's reason: masking
     * produces a different identifier that looks entirely valid. Scoped to
     * a probe that answered: with `responded` clear the field is gated to
     * zero below, so a stale invalid value is normalised, not refused --
     * matching the decoder, with which this shares the predicate (vtp1.h). */
    if (responded && !vtp_obd_identifier_ok(p->request_id)) return -1;
    for (uint8_t i = 0; i < p->count; i++) {
        if (!vtp_obd_identifier_ok(ecus[i].id)) return -1;
        if (i && ecus[i].id <= ecus[i - 1].id) return -1;
    }

    memset(out, 0, VTP_OBD_PROBE_SIZE);
    out[VTP_OBD_PROBE_OFF_VALIDITY] = (uint8_t)v;
    out[VTP_OBD_PROBE_OFF_COUNT]    = p->count;
    wr32(out + VTP_OBD_PROBE_OFF_REQUEST_ID,
         gate32(p->request_id, v, VTP_OBD_VALIDITY_RESPONDED));
    wr32(out + VTP_OBD_PROBE_OFF_SUPPORTED_01_20,
         gate32(p->supported_01_20, v, VTP_OBD_VALIDITY_RESPONDED));
    wr32(out + VTP_OBD_PROBE_OFF_SUPPORTED_21_40,
         gate32(p->supported_21_40, v, VTP_OBD_VALIDITY_RESPONDED));
    wr32(out + VTP_OBD_PROBE_OFF_SUPPORTED_41_60,
         gate32(p->supported_41_60, v, VTP_OBD_VALIDITY_RESPONDED));
    for (uint8_t i = 0; i < p->count; i++) {
        wr32(out + VTP_OBD_PROBE_SIZE + (size_t)i * VTP_OBD_ECU_SIZE
             + VTP_OBD_ECU_OFF_ID, ecus[i].id);
    }
    return (int)needed;
}
