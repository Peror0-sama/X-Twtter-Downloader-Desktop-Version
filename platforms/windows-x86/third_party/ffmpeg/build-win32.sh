#!/bin/bash
set -euo pipefail
cd /home/builder
mkdir -p ffmpeg-build/prefix/include ffmpeg-build/prefix/lib
cd ffmpeg-build
tar -xf ../ffmpeg-8.1.2.tar.xz
test -s zlib-1.3.1.tar.gz
tar -xf zlib-1.3.1.tar.gz
cd zlib-1.3.1
make -f win32/Makefile.gcc PREFIX=i686-w64-mingw32- -j4 libz.a > ../zlib.log 2>&1
cp libz.a ../prefix/lib/
cp zlib.h zconf.h ../prefix/include/
cd ../ffmpeg-8.1.2
./configure --target-os=mingw32 --arch=x86 --cpu=i686 --enable-cross-compile --cross-prefix=i686-w64-mingw32- --disable-autodetect --disable-doc --disable-debug --disable-ffplay --disable-ffprobe --disable-network --enable-static --disable-shared --disable-everything --enable-zlib --enable-ffmpeg --enable-protocol=file,pipe --enable-demuxer=mov,mpegts,concat,image2,gif --enable-muxer=mp4,mov,gif,image2,mpegts,hls,null --enable-decoder=h264,hevc,av1,vp9,vp8,mpeg2video,mpeg4,wrapped_avframe,aac,mp3,mp2,gif,png,mjpeg,bmp,rawvideo --enable-encoder=gif,png,mpeg2video,mpeg4,wrapped_avframe,pcm_s16le,rawvideo,aac --enable-parser=h264,hevc,av1,vp9,mpegvideo,mpeg4video,aac,mpegaudio --enable-filter=palettegen,paletteuse,scale,fps,split,format,null,testsrc,testsrc2,sine,aresample,aformat,trim,atrim,setpts,asetpts --enable-bsf=aac_adtstoasc,h264_mp4toannexb,extract_extradata --enable-indev=lavfi --extra-cflags='-I/home/builder/ffmpeg-build/prefix/include -D_WIN32_WINNT=0x0601' --extra-ldflags='-L/home/builder/ffmpeg-build/prefix/lib -static' > ../configure.log 2>&1
make -j4 > ../make.log 2>&1
file ffmpeg.exe
sha256sum ffmpeg.exe > ../ffmpeg-win32.sha256
