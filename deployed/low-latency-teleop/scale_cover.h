#ifndef SCALE_COVER_H
#define SCALE_COVER_H

/* Center-crop a source texture so it covers dst without stretching.
 * Destination is always the full window; never use a dest rect that
 * goes outside the window. */
typedef struct {
    int x;
    int y;
    int w;
    int h;
} ScaleCoverRect;

static inline ScaleCoverRect scale_cover_src(int tex_w, int tex_h, int dst_w, int dst_h) {
    ScaleCoverRect src = {0, 0, tex_w > 0 ? tex_w : 0, tex_h > 0 ? tex_h : 0};
    if (tex_w <= 0 || tex_h <= 0 || dst_w <= 0 || dst_h <= 0) {
        return src;
    }
    long long tw = tex_w;
    long long th = tex_h;
    long long dw = dst_w;
    long long dh = dst_h;
    if (tw * dh < th * dw) {
        int vis_h = (int)((tw * dh) / dw);
        if (vis_h < 1) vis_h = 1;
        if (vis_h > tex_h) vis_h = tex_h;
        src.x = 0;
        src.w = tex_w;
        src.h = vis_h;
        src.y = (tex_h - vis_h) / 2;
    } else {
        int vis_w = (int)((th * dw) / dh);
        if (vis_w < 1) vis_w = 1;
        if (vis_w > tex_w) vis_w = tex_w;
        src.y = 0;
        src.h = tex_h;
        src.w = vis_w;
        src.x = (tex_w - vis_w) / 2;
    }
    return src;
}

#endif
