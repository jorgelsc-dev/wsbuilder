/*
 * Native kernels of wsbuilder.accel, compiled on demand with the system C
 * compiler (see accel.py) and loaded with ctypes. Plain C99, libm only.
 *
 * Weights stay in their file layout (little-endian): F32, F64, F16, BF16 and
 * the ggml blocks Q8_0 and Q4_0. Every weight is first turned into its exact
 * float value (the same value wsbuilder's Python decoders produce) and then
 * multiplied by the double input, accumulating in double, so the results stay
 * within rounding of the pure Python backend. Built without -ffast-math and
 * with -ffp-contract=off (no fused multiply-add), so the order and rounding of
 * the additions are fixed: eight independent partial sums per row.
 */
#include <math.h>
#include <stdint.h>
#include <string.h>

#define WSB_F32 0
#define WSB_F64 1
#define WSB_F16 2
#define WSB_BF16 3
#define WSB_Q8_0 4
#define WSB_Q4_0 5

#define WSB_ABI_VERSION 1

static float f16_table[65536];
static int f16_ready = 0;

static float half_to_float(uint16_t half)
{
    uint32_t sign = (uint32_t)(half & 0x8000u) << 16;
    uint32_t exponent = (half >> 10) & 0x1fu;
    uint32_t mantissa = half & 0x3ffu;
    uint32_t bits;
    float value;
    if (exponent == 0) {
        /* zero or subnormal: mantissa * 2^-24, exact in float */
        value = (float)mantissa * (1.0f / 16777216.0f);
        return sign ? -value : value;
    }
    if (exponent == 31)
        bits = sign | 0x7f800000u | (mantissa << 13);
    else
        bits = sign | ((exponent + 112u) << 23) | (mantissa << 13);
    memcpy(&value, &bits, sizeof value);
    return value;
}

int wsb_abi_version(void)
{
    return WSB_ABI_VERSION;
}

/* Fills the half-float table; call once before the kernels (not thread-safe). */
void wsb_init(void)
{
    uint32_t index;
    if (f16_ready)
        return;
    for (index = 0; index < 65536u; index++)
        f16_table[index] = half_to_float((uint16_t)index);
    f16_ready = 1;
}

static uint16_t read_u16(const unsigned char *bytes)
{
    return (uint16_t)(bytes[0] | (bytes[1] << 8));
}

static double sum8(const double *acc)
{
    return ((acc[0] + acc[1]) + (acc[2] + acc[3])) + ((acc[4] + acc[5]) + (acc[6] + acc[7]));
}

static double dot_f32(const float *w, const double *x, int64_t cols)
{
    double acc[8] = {0, 0, 0, 0, 0, 0, 0, 0};
    int64_t j = 0;
    int k;
    for (; j + 8 <= cols; j += 8)
        for (k = 0; k < 8; k++)
            acc[k] += (double)w[j + k] * x[j + k];
    for (; j < cols; j++)
        acc[0] += (double)w[j] * x[j];
    return sum8(acc);
}

static double dot_f64(const double *w, const double *x, int64_t cols)
{
    double acc[8] = {0, 0, 0, 0, 0, 0, 0, 0};
    int64_t j = 0;
    int k;
    for (; j + 8 <= cols; j += 8)
        for (k = 0; k < 8; k++)
            acc[k] += w[j + k] * x[j + k];
    for (; j < cols; j++)
        acc[0] += w[j] * x[j];
    return sum8(acc);
}

static double dot_f16(const unsigned char *w, const double *x, int64_t cols)
{
    double acc[8] = {0, 0, 0, 0, 0, 0, 0, 0};
    int64_t j = 0;
    int k;
    for (; j + 8 <= cols; j += 8)
        for (k = 0; k < 8; k++)
            acc[k] += (double)f16_table[read_u16(w + 2 * (j + k))] * x[j + k];
    for (; j < cols; j++)
        acc[0] += (double)f16_table[read_u16(w + 2 * j)] * x[j];
    return sum8(acc);
}

static float bf16_value(const unsigned char *bytes)
{
    uint32_t bits = (uint32_t)read_u16(bytes) << 16;
    float value;
    memcpy(&value, &bits, sizeof value);
    return value;
}

static double dot_bf16(const unsigned char *w, const double *x, int64_t cols)
{
    double acc[8] = {0, 0, 0, 0, 0, 0, 0, 0};
    int64_t j = 0;
    int k;
    for (; j + 8 <= cols; j += 8)
        for (k = 0; k < 8; k++)
            acc[k] += (double)bf16_value(w + 2 * (j + k)) * x[j + k];
    for (; j < cols; j++)
        acc[0] += (double)bf16_value(w + 2 * j) * x[j];
    return sum8(acc);
}

/* Q8_0 block: f16 scale d, then 32 int8 q; the weight is d * q, exact in float
 * (11 + 8 significant bits), as the Python decoder computes it. */
static double dot_q8_0(const unsigned char *w, const double *x, int64_t cols)
{
    double acc[8] = {0, 0, 0, 0, 0, 0, 0, 0};
    int64_t block;
    int k;
    for (block = 0; block < cols / 32; block++) {
        const unsigned char *data = w + 34 * block;
        const signed char *quants = (const signed char *)(data + 2);
        const double *xs = x + 32 * block;
        float scale = f16_table[read_u16(data)];
        for (k = 0; k < 32; k++)
            acc[k & 7] += (double)(scale * (float)quants[k]) * xs[k];
    }
    return sum8(acc);
}

/* Q4_0 block: f16 scale d, then 16 bytes; element k is the low nibble of byte
 * k and element k + 16 its high nibble; the weight is (nibble - 8) * d. */
static double dot_q4_0(const unsigned char *w, const double *x, int64_t cols)
{
    double acc[8] = {0, 0, 0, 0, 0, 0, 0, 0};
    int64_t block;
    int k;
    for (block = 0; block < cols / 32; block++) {
        const unsigned char *data = w + 18 * block;
        const double *xs = x + 32 * block;
        float scale = f16_table[read_u16(data)];
        for (k = 0; k < 16; k++) {
            int byte = data[2 + k];
            acc[k & 7] += (double)(scale * (float)((byte & 0x0f) - 8)) * xs[k];
            acc[(k + 16) & 7] += (double)(scale * (float)((byte >> 4) - 8)) * xs[k + 16];
        }
    }
    return sum8(acc);
}

static int64_t row_bytes(int dtype, int64_t cols)
{
    switch (dtype) {
    case WSB_F32: return 4 * cols;
    case WSB_F64: return 8 * cols;
    case WSB_F16:
    case WSB_BF16: return 2 * cols;
    case WSB_Q8_0: return 34 * (cols / 32);
    case WSB_Q4_0: return 18 * (cols / 32);
    default: return -1;
    }
}

/*
 * out[r] = dot(row r of w, x) (+ bias[r]) for rows [row_start, row_end).
 * bias may be NULL. Returns 0, or -1 for an unknown dtype or bad arguments.
 */
int wsb_matvec(int dtype, const void *weights, const double *x, const double *bias,
               double *out, int64_t rows, int64_t cols, int64_t row_start, int64_t row_end)
{
    const unsigned char *base = (const unsigned char *)weights;
    int64_t stride = row_bytes(dtype, cols);
    int64_t r;
    if (stride < 0 || row_start < 0 || row_end > rows || row_start > row_end || !f16_ready
        || !weights || !x || !out)
        return -1;
    if ((dtype == WSB_Q8_0 || dtype == WSB_Q4_0) && cols % 32)
        return -1;
    for (r = row_start; r < row_end; r++) {
        const unsigned char *row = base + stride * r;
        double value;
        switch (dtype) {
        case WSB_F32: value = dot_f32((const float *)row, x, cols); break;
        case WSB_F64: value = dot_f64((const double *)row, x, cols); break;
        case WSB_F16: value = dot_f16(row, x, cols); break;
        case WSB_BF16: value = dot_bf16(row, x, cols); break;
        case WSB_Q8_0: value = dot_q8_0(row, x, cols); break;
        default: value = dot_q4_0(row, x, cols); break;
        }
        out[r] = bias ? value + bias[r] : value;
    }
    return 0;
}

/*
 * Attention of the `group` query heads that share one KV head.
 * q: group x head_dim; keys/values: length x head_dim (row per position);
 * out: group x head_dim; scores: scratch of `length` doubles. Positions
 * [start, length) are attended. softcap <= 0 disables the logit softcap.
 */
int wsb_attend(const double *q, const double *keys, const double *values, double *out,
               double *scores, int64_t group, int64_t head_dim, int64_t start, int64_t length,
               double scale, double softcap)
{
    int64_t head, i, d;
    if (start < 0 || start >= length || group < 1 || head_dim < 1
        || !q || !keys || !values || !out || !scores)
        return -1;
    for (head = 0; head < group; head++) {
        const double *query = q + head * head_dim;
        double *result = out + head * head_dim;
        double peak = -HUGE_VAL;
        double total = 0.0;
        for (i = start; i < length; i++) {
            const double *key = keys + i * head_dim;
            double score = 0.0;
            for (d = 0; d < head_dim; d++)
                score += query[d] * key[d];
            score *= scale;
            if (softcap > 0.0)
                score = tanh(score / softcap) * softcap;
            scores[i - start] = score;
            if (score > peak)
                peak = score;
        }
        for (i = 0; i < length - start; i++) {
            scores[i] = exp(scores[i] - peak);
            total += scores[i];
        }
        for (i = 0; i < length - start; i++)
            scores[i] /= total;
        for (d = 0; d < head_dim; d++)
            result[d] = 0.0;
        for (i = start; i < length; i++) {
            const double *value = values + i * head_dim;
            double weight = scores[i - start];
            for (d = 0; d < head_dim; d++)
                result[d] += weight * value[d];
        }
    }
    return 0;
}
