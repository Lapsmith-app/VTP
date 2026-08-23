/* Producer-conformance adapter for the C reference encoder.
 *
 * The counterpart to vtp1_cli.c. That one implements the DECODER contract in
 * conformance/README.md; this implements the PRODUCER contract:
 *
 *   stdin  — one case per line: <record><TAB><json-object>
 *   stdout — one JSON object per line, in the same order:
 *              {"ok":true,"hex":"..."}      the encoder produced these bytes
 *              {"ok":false,"reason":"..."}  the encoder refused
 *
 * It exists because tools/check_encoders.py used to import the Python encoder
 * directly, so "14/14 producer cases" measured one of this repository's two
 * reference encoders and CI stayed green over four crashes and a contract
 * violation in the other. A producer suite that can only test the language it
 * is written in is not a conformance suite.
 *
 * The JSON reader below is deliberately minimal — objects, arrays, strings,
 * integers, booleans, null, and nothing else. It parses harness input, not
 * protocol data: no VTP/1 payload is JSON, so none of this is on any path a
 * device runs. Keeping it here rather than in vtp1.c is the point.
 */
#include "vtp1_encode.h"
#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAX_NODES     512
#define MAX_TEXT      8192
#define MAX_FRAMES    64
#define MAX_SAMPLES   64
#define MAX_ENTRIES   64
#define MAX_BYTES     4096
#define MAX_LINE      16384

/* ---- a very small JSON reader ---------------------------------------- */

enum jv_kind { JV_NULL, JV_BOOL, JV_INT, JV_STR, JV_ARR, JV_OBJ };

typedef struct jv {
    enum jv_kind kind;
    long long    num;        /* JV_INT, and 0/1 for JV_BOOL */
    const char  *str;        /* JV_STR: NUL-terminated, into `text` below */
    int          first;      /* JV_ARR/JV_OBJ: index of first child, or -1 */
    int          next;       /* index of the next sibling, or -1 */
    const char  *key;        /* member name when the parent is JV_OBJ */
} jv;

typedef struct {
    jv     nodes[MAX_NODES];
    int    used;
    char   text[MAX_TEXT];
    size_t text_used;
    const char *p;
    const char *err;
} jctx;

static int jparse_value(jctx *c);

static void jskip(jctx *c) {
    while (*c->p && isspace((unsigned char)*c->p)) c->p++;
}

static int jnode(jctx *c, enum jv_kind kind) {
    if (c->used >= MAX_NODES) { c->err = "too many json nodes"; return -1; }
    jv *n = &c->nodes[c->used];
    memset(n, 0, sizeof *n);
    n->kind = kind;
    n->first = n->next = -1;
    return c->used++;
}

/* Strings are copied into `text` so every jv.str is NUL-terminated. Only the
 * escapes a harness file can actually contain are honoured; anything else is a
 * parse error rather than a silently mangled string. */
static const char *jstring(jctx *c) {
    if (*c->p != '"') { c->err = "expected a string"; return NULL; }
    c->p++;
    char *out = c->text + c->text_used;
    size_t room = MAX_TEXT - c->text_used;
    size_t n = 0;
    while (*c->p && *c->p != '"') {
        if (n + 2 > room) { c->err = "json text overflow"; return NULL; }
        if (*c->p == '\\') {
            c->p++;
            switch (*c->p) {
            case '"':  out[n++] = '"';  break;
            case '\\': out[n++] = '\\'; break;
            case '/':  out[n++] = '/';  break;
            case 'n':  out[n++] = '\n'; break;
            case 't':  out[n++] = '\t'; break;
            case 'r':  out[n++] = '\r'; break;
            case 'b':  out[n++] = '\b'; break;
            case 'f':  out[n++] = '\f'; break;
            default:   c->err = "unsupported json escape"; return NULL;
            }
            c->p++;
        } else {
            out[n++] = *c->p++;
        }
    }
    if (*c->p != '"') { c->err = "unterminated string"; return NULL; }
    c->p++;
    out[n] = 0;
    c->text_used += n + 1;
    return out;
}

static int jparse_container(jctx *c, enum jv_kind kind, char close) {
    int self = jnode(c, kind);
    if (self < 0) return -1;
    c->p++;                                   /* '[' or '{' */
    jskip(c);
    if (*c->p == close) { c->p++; return self; }
    int prev = -1;
    for (;;) {
        jskip(c);
        const char *key = NULL;
        if (kind == JV_OBJ) {
            key = jstring(c);
            if (!key) return -1;
            jskip(c);
            if (*c->p != ':') { c->err = "expected ':'"; return -1; }
            c->p++;
            jskip(c);
        }
        int child = jparse_value(c);
        if (child < 0) return -1;
        c->nodes[child].key = key;
        if (prev < 0) c->nodes[self].first = child;
        else          c->nodes[prev].next = child;
        prev = child;
        jskip(c);
        if (*c->p == ',') { c->p++; continue; }
        if (*c->p == close) { c->p++; return self; }
        c->err = "expected ',' or a closing bracket";
        return -1;
    }
}

static int jparse_value(jctx *c) {
    jskip(c);
    if (*c->p == '{') return jparse_container(c, JV_OBJ, '}');
    if (*c->p == '[') return jparse_container(c, JV_ARR, ']');
    if (*c->p == '"') {
        const char *s = jstring(c);
        if (!s) return -1;
        int self = jnode(c, JV_STR);
        if (self < 0) return -1;
        c->nodes[self].str = s;
        return self;
    }
    if (!strncmp(c->p, "true", 4) || !strncmp(c->p, "false", 5)) {
        int self = jnode(c, JV_BOOL);
        if (self < 0) return -1;
        c->nodes[self].num = (*c->p == 't');
        c->p += (*c->p == 't') ? 4 : 5;
        return self;
    }
    if (!strncmp(c->p, "null", 4)) {
        c->p += 4;
        return jnode(c, JV_NULL);
    }
    if (*c->p == '-' || isdigit((unsigned char)*c->p)) {
        char *end;
        long long v = strtoll(c->p, &end, 10);
        if (end == c->p) { c->err = "malformed number"; return -1; }
        /* A fractional or exponent part would be silently truncated to the
         * integer part, which is exactly the kind of quiet reshaping this
         * whole suite exists to catch. No producer case needs one. */
        if (*end == '.' || *end == 'e' || *end == 'E') {
            c->err = "non-integer number";
            return -1;
        }
        int self = jnode(c, JV_INT);
        if (self < 0) return -1;
        c->nodes[self].num = v;
        c->p = end;
        return self;
    }
    c->err = "unrecognised json value";
    return -1;
}

static const jv *jget(const jctx *c, const jv *obj, const char *key) {
    if (!obj || obj->kind != JV_OBJ) return NULL;
    for (int i = obj->first; i >= 0; i = c->nodes[i].next)
        if (c->nodes[i].key && !strcmp(c->nodes[i].key, key))
            return &c->nodes[i];
    return NULL;
}

static int jlen(const jctx *c, const jv *arr) {
    int n = 0;
    if (!arr || arr->kind != JV_ARR) return -1;
    for (int i = arr->first; i >= 0; i = c->nodes[i].next) n++;
    return n;
}

static const jv *jat(const jctx *c, const jv *arr, int want) {
    int n = 0;
    if (!arr || arr->kind != JV_ARR) return NULL;
    for (int i = arr->first; i >= 0; i = c->nodes[i].next, n++)
        if (n == want) return &c->nodes[i];
    return NULL;
}

/* Missing keys read as zero, exactly as the Python encoder's `.get(name, 0)`
 * does, so the two adapters present their encoders with the same call. */
static long long jint(const jctx *c, const jv *obj, const char *key) {
    const jv *v = jget(c, obj, key);
    return (v && (v->kind == JV_INT || v->kind == JV_BOOL)) ? v->num : 0;
}

static int jbool(const jctx *c, const jv *obj, const char *key) {
    return jint(c, obj, key) != 0;
}

/* ---- helpers ---------------------------------------------------------- */

static int unhex(const char *s, uint8_t *out, size_t cap, size_t *len) {
    size_t n = 0;
    while (s && s[0]) {
        if (!s[1] || n >= cap) return -1;
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

static const char *jhex(const jctx *c, const jv *obj, const char *key) {
    const jv *v = jget(c, obj, key);
    return (v && v->kind == JV_STR) ? v->str : "";
}

static void refuse(const char *why) {
    printf("{\"ok\":false,\"reason\":\"%s\"}\n", why);
}

static void produced(const uint8_t *buf, int n) {
    printf("{\"ok\":true,\"hex\":\"");
    for (int i = 0; i < n; i++) printf("%02x", buf[i]);
    printf("\"}\n");
}

/* The encoders return -1 for every refusal without distinguishing them. The
 * runner does not compare reason strings — SPEC.md defines none — so one
 * honest label is better than a guess at which rule fired. */
static void encoded(int n, const uint8_t *buf) {
    if (n < 0) refuse("the encoder refused this input");
    else       produced(buf, n);
}

/* ---- per-record adapters ---------------------------------------------- */

static uint8_t out[MAX_BYTES];

static void do_gps_fix(const jctx *c, const jv *in) {
    const jv *f = jget(c, in, "fix");
    if (!f) { refuse("no `fix` object"); return; }
    vtp_gps_fix_t fix;
    memset(&fix, 0, sizeof fix);
    fix.seq           = (uint16_t)jint(c, f, "seq");
    fix.dropped       = (uint16_t)jint(c, f, "dropped");
    fix.validity      = (uint32_t)jint(c, f, "validity");
    fix.t_device      = (uint64_t)jint(c, f, "t_device");
    fix.t_utc         = (int64_t) jint(c, f, "t_utc");
    fix.lat           = (int32_t) jint(c, f, "lat");
    fix.lon           = (int32_t) jint(c, f, "lon");
    fix.alt_msl       = (int32_t) jint(c, f, "alt_msl");
    fix.alt_ellipsoid = (int32_t) jint(c, f, "alt_ellipsoid");
    fix.vel_n         = (int32_t) jint(c, f, "vel_n");
    fix.vel_e         = (int32_t) jint(c, f, "vel_e");
    fix.vel_d         = (int32_t) jint(c, f, "vel_d");
    fix.head_mot      = (int32_t) jint(c, f, "head_mot");
    fix.h_acc         = (uint32_t)jint(c, f, "h_acc");
    fix.v_acc         = (uint32_t)jint(c, f, "v_acc");
    fix.s_acc         = (uint32_t)jint(c, f, "s_acc");
    fix.p_dop         = (uint16_t)jint(c, f, "p_dop");
    fix.fix_type      = (uint8_t) jint(c, f, "fix_type");
    fix.num_sv        = (uint8_t) jint(c, f, "num_sv");
    fix.fix_flags     = (uint8_t) jint(c, f, "fix_flags");
    fix.ext_count     = (uint8_t) jint(c, f, "ext_count");

    uint8_t ext[MAX_BYTES];
    size_t ext_len = 0;
    if (unhex(jhex(c, in, "ext_hex"), ext, sizeof ext, &ext_len) < 0) {
        refuse("malformed ext_hex"); return;
    }
    encoded(vtp_encode_gps_fix(&fix, ext_len ? ext : NULL, ext_len,
                               out, sizeof out), out);
}

static void do_can_batch(const jctx *c, const jv *in) {
    const jv *h = jget(c, in, "header");
    const jv *rs = jget(c, in, "records");
    if (!h) { refuse("no `header` object"); return; }
    int n = jlen(c, rs);
    if (n < 0) { refuse("no `records` array"); return; }
    if (n > MAX_FRAMES) { refuse("too many records for this harness"); return; }

    vtp_can_header_t hdr;
    memset(&hdr, 0, sizeof hdr);
    hdr.seq      = (uint16_t)jint(c, h, "seq");
    hdr.dropped  = (uint16_t)jint(c, h, "dropped");
    hdr.t_base   = (uint64_t)jint(c, h, "t_base");
    hdr.count    = (uint8_t) jint(c, h, "count");
    hdr.flags    = (uint8_t) jint(c, h, "flags");
    hdr.reserved = (uint16_t)jint(c, h, "reserved");

    /* The C encoder reads `hdr->count` frames from the array, so a count that
     * disagrees with the array is not a call this API can express — there is
     * no length to compare against. The Python encoder rejects it explicitly;
     * here the harness does, and says so, rather than reading off the end.
     *
     * The same applies one level down, to `len` against the payload actually
     * supplied, and missing it made this adapter certify output the Python
     * encoder refuses: `len` of 8 behind one byte of payload produced a frame
     * with seven zeroes the caller never supplied, and `len` of 0 behind one
     * byte silently dropped it. Both answered `ok`. A producer suite whose
     * adapter reshapes its input has the exact defect the suite exists to
     * find, one layer further out. */
    if ((int)hdr.count != n) { refuse("can_header.count disagrees with `records`"); return; }

    static vtp_can_frame_t frames[MAX_FRAMES];
    static uint8_t payloads[MAX_FRAMES][64];
    memset(frames, 0, sizeof frames);
    for (int i = 0; i < n; i++) {
        const jv *r = jat(c, rs, i);
        size_t plen = 0;
        if (unhex(jhex(c, r, "payload"), payloads[i], sizeof payloads[i], &plen) < 0) {
            refuse("malformed frame payload"); return;
        }
        if (plen != (size_t)jint(c, r, "len")) {
            refuse("can_record.len disagrees with the payload supplied");
            return;
        }
        frames[i].dt       = (uint16_t)jint(c, r, "dt");
        /* A negative identifier lands here exactly as firmware would land it:
         * assigned to the unsigned field the struct declares, where it reads
         * as 0xFFFFFFFF and the encoder refuses it for being outside the
         * arbitration field. The harness does not pre-screen it — the whole
         * question is what the ENCODER does. */
        frames[i].id       = (uint32_t)jint(c, r, "id");
        frames[i].extended = jbool(c, r, "extended");
        frames[i].fd       = jbool(c, r, "fd");
        frames[i].rtr      = jbool(c, r, "rtr");
        frames[i].len      = (uint8_t)jint(c, r, "len");
        frames[i].payload  = plen ? payloads[i] : NULL;
    }
    encoded(vtp_encode_can_batch(&hdr, n ? frames : NULL, out, sizeof out), out);
}

static void do_imu_batch(const jctx *c, const jv *in) {
    const jv *h = jget(c, in, "header");
    const jv *ss = jget(c, in, "samples");
    if (!h) { refuse("no `header` object"); return; }
    int n = jlen(c, ss);
    if (n < 0) { refuse("no `samples` array"); return; }
    if (n > MAX_SAMPLES) { refuse("too many samples for this harness"); return; }

    vtp_imu_header_t hdr;
    memset(&hdr, 0, sizeof hdr);
    hdr.seq      = (uint16_t)jint(c, h, "seq");
    hdr.dropped  = (uint16_t)jint(c, h, "dropped");
    hdr.t_base   = (uint64_t)jint(c, h, "t_base");
    hdr.period   = (uint32_t)jint(c, h, "period");
    hdr.count    = (uint8_t) jint(c, h, "count");
    hdr.flags    = (uint8_t) jint(c, h, "flags");
    hdr.reserved = (uint16_t)jint(c, h, "reserved");
    if ((int)hdr.count != n) { refuse("imu_header.count disagrees with `samples`"); return; }

    static vtp_imu_sample_t samples[MAX_SAMPLES];
    memset(samples, 0, sizeof samples);
    for (int i = 0; i < n; i++) {
        const jv *s = jat(c, ss, i);
        samples[i].ax = (int16_t)jint(c, s, "ax");
        samples[i].ay = (int16_t)jint(c, s, "ay");
        samples[i].az = (int16_t)jint(c, s, "az");
        samples[i].gx = (int16_t)jint(c, s, "gx");
        samples[i].gy = (int16_t)jint(c, s, "gy");
        samples[i].gz = (int16_t)jint(c, s, "gz");
    }
    encoded(vtp_encode_imu_batch(&hdr, n ? samples : NULL, out, sizeof out), out);
}

static void do_info(const jctx *c, const jv *in) {
    vtp_info_t v;
    memset(&v, 0, sizeof v);
    v.protocol_major         = (uint8_t) jint(c, in, "protocol_major");
    v.protocol_minor         = (uint8_t) jint(c, in, "protocol_minor");
    v.capabilities           = (uint32_t)jint(c, in, "capabilities");
    v.gps_rate_hz            = (uint16_t)jint(c, in, "gps_rate_hz");
    v.gps_max_rate_hz        = (uint16_t)jint(c, in, "gps_max_rate_hz");
    v.can_subscription_slots = (uint16_t)jint(c, in, "can_subscription_slots");
    v.can_max_frames_per_s   = (uint32_t)jint(c, in, "can_max_frames_per_s");
    v.imu_rate_hz            = (uint16_t)jint(c, in, "imu_rate_hz");
    v.imu_max_rate_hz        = (uint16_t)jint(c, in, "imu_max_rate_hz");
    v.reserved_20            = (uint8_t) jint(c, in, "reserved_20");
    v.clock_flags            = (uint8_t) jint(c, in, "clock_flags");
    v.reserved_22            = (uint16_t)jint(c, in, "reserved_22");
    encoded(vtp_encode_info(&v, out, sizeof out), out);
}

static void do_monitor_list(const jctx *c, const jv *in) {
    const jv *p = jget(c, in, "declaration");
    const jv *es = jget(c, in, "entries");
    if (!p) { refuse("no `declaration` object"); return; }
    int n = jlen(c, es);
    if (n < 0) { refuse("no `entries` array"); return; }
    if (n > MAX_ENTRIES) { refuse("too many entries for this harness"); return; }

    vtp_monitor_declaration_t page;
    memset(&page, 0, sizeof page);
    page.count    = (uint8_t) jint(c, p, "count");
    page.reserved = (uint8_t) jint(c, p, "reserved");
    if ((int)page.count != n) { refuse("monitor_declaration.count disagrees with `entries`"); return; }

    static vtp_monitor_channel_t entries[MAX_ENTRIES];
    memset(entries, 0, sizeof entries);
    for (int i = 0; i < n; i++) {
        const jv *e = jat(c, es, i);
        entries[i].slot    = (uint8_t) jint(c, e, "slot");
        entries[i].channel = (uint16_t)jint(c, e, "channel");
        entries[i].max_age = (uint8_t) jint(c, e, "max_age");
    }
    encoded(vtp_encode_monitor_list(&page, n ? entries : NULL, out, sizeof out), out);
}

static void do_monitor_update(const jctx *c, const jv *in) {
    const jv *h = jget(c, in, "header");
    const jv *vs = jget(c, in, "values");
    if (!h) { refuse("no `header` object"); return; }
    int n = jlen(c, vs);
    if (n < 0) { refuse("no `values` array"); return; }
    if (n > MAX_ENTRIES) { refuse("too many values for this harness"); return; }

    vtp_monitor_header_t hdr;
    memset(&hdr, 0, sizeof hdr);
    hdr.seq      = (uint16_t)jint(c, h, "seq");
    hdr.count    = (uint8_t) jint(c, h, "count");
    hdr.reserved = (uint8_t) jint(c, h, "reserved");
    if ((int)hdr.count != n) { refuse("monitor_header.count disagrees with `values`"); return; }

    static vtp_monitor_value_t values[MAX_ENTRIES];
    memset(values, 0, sizeof values);
    for (int i = 0; i < n; i++) {
        const jv *v = jat(c, vs, i);
        values[i].slot     = (uint8_t)jint(c, v, "slot");
        values[i].validity = (uint8_t)jint(c, v, "validity");
        values[i].value    = (int32_t)jint(c, v, "value");
    }
    encoded(vtp_encode_monitor_update(&hdr, n ? values : NULL, out, sizeof out), out);
}

static void do_control_response(const jctx *c, const jv *in) {
    uint8_t detail[MAX_BYTES];
    size_t detail_len = 0;
    if (unhex(jhex(c, in, "detail_hex"), detail, sizeof detail, &detail_len) < 0) {
        refuse("malformed detail_hex"); return;
    }
    vtp_control_response_t r;
    memset(&r, 0, sizeof r);
    r.opcode     = (uint8_t)jint(c, in, "opcode");
    r.tag        = (uint8_t)jint(c, in, "tag");
    r.status     = (uint8_t)jint(c, in, "status");
    r.detail     = detail_len ? detail : NULL;
    r.detail_len = detail_len;
    encoded(vtp_encode_control_response(&r, out, sizeof out), out);
}

static void do_time_sync(const jctx *c, const jv *in) {
    vtp_time_sync_t t;
    memset(&t, 0, sizeof t);
    t.t_device_rx = (uint64_t)jint(c, in, "t_device_rx");
    t.t_device_tx = (uint64_t)jint(c, in, "t_device_tx");
    encoded(vtp_encode_time_sync(&t, out, sizeof out), out);
}

/* ---- main ------------------------------------------------------------- */

int main(void) {
    static char line[MAX_LINE];
    static jctx ctx;

    while (fgets(line, sizeof line, stdin)) {
        char *tab = strchr(line, '\t');
        if (!tab) continue;                       /* blank or comment line */
        *tab = 0;
        char *record = line, *json = tab + 1;

        ctx.used = 0;
        ctx.text_used = 0;
        ctx.err = NULL;
        ctx.p = json;
        int root = jparse_value(&ctx);
        if (root < 0) {
            printf("{\"ok\":false,\"reason\":\"harness: %s\"}\n",
                   ctx.err ? ctx.err : "unparsable json");
            fflush(stdout);
            continue;
        }
        const jv *in = &ctx.nodes[root];

        if      (!strcmp(record, "gps_fix"))          do_gps_fix(&ctx, in);
        else if (!strcmp(record, "can_batch"))        do_can_batch(&ctx, in);
        else if (!strcmp(record, "imu_batch"))        do_imu_batch(&ctx, in);
        else if (!strcmp(record, "info"))             do_info(&ctx, in);
        else if (!strcmp(record, "monitor_list"))     do_monitor_list(&ctx, in);
        else if (!strcmp(record, "monitor_update"))   do_monitor_update(&ctx, in);
        else if (!strcmp(record, "control_response")) do_control_response(&ctx, in);
        else if (!strcmp(record, "time_sync"))        do_time_sync(&ctx, in);
        else printf("{\"ok\":false,\"reason\":\"harness: no encoder for record\"}\n");
        fflush(stdout);
    }
    return 0;
}
