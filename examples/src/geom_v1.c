#include "shapes.h"

/* 旧版实现：导出符号版本 LIBGEOM_1.0 */
int32_t box_area(const box_t b) {
    return (int32_t)((double)b.width * (double)b.height * b.scale);
}

int32_t add_i32(int32_t a, int32_t b) {
    return a + b;
}

double scale_f64(double x) {
    return x * 2.0;
}

void log_event(int32_t code) {
    (void)code;
}
