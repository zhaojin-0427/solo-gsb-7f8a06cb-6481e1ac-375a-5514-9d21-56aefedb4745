#ifndef SHAPES_H
#define SHAPES_H

#include <stdint.h>

/* 旧版布局（v1）：字段顺序 width, height, scale */
typedef struct box {
    int32_t width;
    int32_t height;
    double  scale;
} box_t;

/* 旧版签名 */
int32_t box_area(const box_t b);
int32_t add_i32(int32_t a, int32_t b);
double  scale_f64(double x);
void    log_event(int32_t code);

#endif
