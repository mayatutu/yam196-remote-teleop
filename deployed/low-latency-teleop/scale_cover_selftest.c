#include "scale_cover.h"

#include <stdio.h>

static int failed;
#define EXPECT(x) do { if (!(x)) { fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #x); failed++; } } while (0)

static void test_43_into_170(void) {
    /* 4:3 source into 1.70:1 window: full width, symmetric top/bottom crop. */
    ScaleCoverRect src = scale_cover_src(640, 480, 170, 100);
    EXPECT(src.x == 0);
    EXPECT(src.w == 640);
    EXPECT(src.h == 376);
    EXPECT(src.y == 52);
    EXPECT(src.y + src.h + src.y == 480);
}

static void test_square_crops_sides(void) {
    ScaleCoverRect src = scale_cover_src(640, 480, 100, 100);
    EXPECT(src.y == 0);
    EXPECT(src.h == 480);
    EXPECT(src.w == 480);
    EXPECT(src.x == 80);
}

static void test_same_aspect_is_full(void) {
    ScaleCoverRect src = scale_cover_src(640, 480, 640, 480);
    EXPECT(src.x == 0);
    EXPECT(src.y == 0);
    EXPECT(src.w == 640);
    EXPECT(src.h == 480);
}

static void test_bad_sizes(void) {
    ScaleCoverRect src = scale_cover_src(0, 480, 170, 100);
    EXPECT(src.w == 0);
    EXPECT(src.h == 480);
}

int main(void) {
    test_43_into_170();
    test_square_crops_sides();
    test_same_aspect_is_full();
    test_bad_sizes();
    if (failed) return 1;
    puts("scale_cover_selftest: ok");
    return 0;
}
