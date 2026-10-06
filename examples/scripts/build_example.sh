#!/usr/bin/env bash
# 构建示例产物（需要容器内/本机有 gcc）：
#   dist/libgeom.so.1          旧版共享库（SONAME=libgeom.so.1，符号版本 LIBGEOM_1.0）
#   dist/libgeom_v2.so         候选库（独立文件名；SONAME 同为 libgeom.so.1，符号版本 LIBGEOM_2.0）
#   dist/client                按旧版链接、强引用 LIBGEOM_1.0 的客户端
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
SRC="$ROOT/src"
OUT="$ROOT/dist"
mkdir -p "$OUT"

cat > "$OUT/geom_v1.map" <<'EOF'
LIBGEOM_1.0 {
  global:
    box_area;
    add_i32;
    scale_f64;
    log_event;
  local: *;
};
EOF

cat > "$OUT/geom_v2.map" <<'EOF'
LIBGEOM_2.0 {
  global:
    box_area;
    add_i32;
    scale_f64;
    log_event;
  local: *;
};
EOF

# 旧库：保留 DWARF（默认 -g），带 SONAME
gcc -g -O0 -fPIC -shared \
    -Wl,-soname,libgeom.so.1 \
    -Wl,--version-script="$OUT/geom_v1.map" \
    "$SRC/geom_v1.c" -o "$OUT/libgeom.so.1"

# 候选库：独立文件名，SONAME 与旧库相同（发布时常见做法），新版本号
gcc -g -O0 -fPIC -shared \
    -Wl,-soname,libgeom.so.1 \
    -Wl,--version-script="$OUT/geom_v2.map" \
    "$SRC/geom_v2.c" -o "$OUT/libgeom_v2.so"

# 客户端：直接按旧库链接；用 .symver 把四个引用钉到 LIBGEOM_1.0
cat > "$OUT/client_symver.s" <<'EOF'
.symver box_area, box_area@LIBGEOM_1.0
.symver add_i32, add_i32@LIBGEOM_1.0
.symver scale_f64, scale_f64@LIBGEOM_1.0
.symver log_event, log_event@LIBGEOM_1.0
EOF

gcc -g -O0 \
    "$SRC/client.c" "$OUT/client_symver.s" \
    -L"$OUT" -l:libgeom.so.1 \
    -Wl,-rpath,'$ORIGIN' \
    -o "$OUT/client"

echo "built into $OUT"
readelf -V "$OUT/client" | sed -n '/Version needs/,/^$/p' | head -20
