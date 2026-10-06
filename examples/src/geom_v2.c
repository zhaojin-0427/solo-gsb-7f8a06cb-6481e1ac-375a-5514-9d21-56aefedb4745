#include <stdint.h>

/* 候选版结构体：字段重排（scale 提前）并新增 depth(int32)、weight(double)。
   标签名 box 保持不变，便于按名关联默认值。 */
typedef struct box {
    double  scale;     /* 重排：旧字段同名同型 */
    int32_t width;
    int32_t height;
    int32_t depth;     /* 新增字段，默认值由请求给出 */
    double  weight;    /* 新增字段，默认值由请求给出 */
} box_t;

/* 候选版签名（函数名不变，符号版本升级为 LIBGEOM_2.0）。
   几何面积仍由旧字段算出；新增字段只是被累加到结果，便于观察默认值生效。 */
int32_t box_area(const box_t b) {
    int32_t geo = (int32_t)((double)b.width * (double)b.height * b.scale);
    return geo + b.depth + (int32_t)b.weight;
}

int32_t add_i32(int32_t a, int32_t b) {
    /* 签名不变也必须转发；内部实现可以变化 */
    return a + b;
}

double scale_f64(double x) {
    return x * 2.0;  /* 与旧版一致：验证“签名未变也转发” */
}

void log_event(int32_t code) {
    (void)code;
}
