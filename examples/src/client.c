#include <stdint.h>
#include <stdio.h>

/* 按旧版链接的客户端：这里不依赖头文件，直接用与旧版一致的 ABI 声明。
   对 LIBGEOM_1.0 版本符号的引用通过 .symver 产生（见 build_example.sh）。 */
typedef struct box {
    int32_t width;
    int32_t height;
    double  scale;
} box_t;

extern int32_t box_area(box_t b);
extern int32_t add_i32(int32_t a, int32_t b);
extern double  scale_f64(double x);
extern void    log_event(int32_t code);

int main(void) {
    box_t b = {3, 4, 2.0};
    printf("box_area = %d (expect 24)\n", box_area(b));
    printf("add_i32  = %d (expect 7)\n", add_i32(3, 4));
    printf("scale_f64 = %f (expect 20.000000 under old semantics)\n", scale_f64(10.0));
    log_event(1);
    return 0;
}
