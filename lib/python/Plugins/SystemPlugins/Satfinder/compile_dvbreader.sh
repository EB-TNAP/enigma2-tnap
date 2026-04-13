#!/bin/bash
# Cross-compile dvbreader.c for ARM (cortexa15hf-neon-vfpv4 / Dreambox)
# Requires the openpli-dreambox-oe-core Yocto build tree to be present.

set -e

OE=/home/pli/builds/dreambpx-fairbird-2/openpli-dreambox-oe-core/build/tmp/sysroots-components
DIR="$(cd "$(dirname "$0")" && pwd)"

CROSS=$OE/x86_64/gcc-cross-arm/usr/bin/arm-oe-linux-gnueabi/arm-oe-linux-gnueabi-gcc
STRIP=$OE/x86_64/binutils-cross-arm/usr/bin/arm-oe-linux-gnueabi/arm-oe-linux-gnueabi-strip
GLIBC=$OE/cortexa15hf-neon-vfpv4/glibc
PY_INC=$OE/cortexa15hf-neon-vfpv4/python3/usr/include
LINUX_INC=$OE/cortexa15hf-neon-vfpv4/linux-libc-headers/usr/include
LIBGCC_RT=$OE/cortexa15hf-neon-vfpv4/libgcc/usr/lib/arm-oe-linux-gnueabi/15.2.0
LIBGCC_LIB=$OE/cortexa15hf-neon-vfpv4/libgcc/lib

# GCC 15 converts -mfpu=neon-vfpv4 to -march=armv7-a+neon-vfpv4 in assembler
# flags, which the system 'as' rejects.  Use the Yocto cross assembler instead.
AS=$OE/x86_64/binutils-cross-arm/usr/bin/arm-oe-linux-gnueabi/arm-oe-linux-gnueabi-as
mkdir -p /tmp/cross-tools
ln -sf "$AS" /tmp/cross-tools/as
ln -sf "$(dirname "$AS")/arm-oe-linux-gnueabi-ld" /tmp/cross-tools/ld

# Detect Python version from include dir (handles future upgrades gracefully)
PY_VER=$(ls "$PY_INC" | grep '^python3' | head -1)

$CROSS \
    -B/tmp/cross-tools -B"$LIBGCC_RT" \
    --sysroot="$GLIBC" \
    -march=armv7-a -mfpu=neon-vfpv4 -mfloat-abi=hard \
    -I"$PY_INC" -I"$PY_INC/$PY_VER" -I"$LINUX_INC" \
    -L"$LIBGCC_RT" -L"$LIBGCC_LIB" \
    -shared -fPIC -O2 \
    -o /tmp/dvbreader_new.so \
    "$DIR/dvbreader.c" \
    2>&1

$STRIP /tmp/dvbreader_new.so -o "$DIR/dvbreader.so"
echo "OK: $(file "$DIR/dvbreader.so")"
