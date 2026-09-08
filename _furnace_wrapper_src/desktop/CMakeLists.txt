cmake_minimum_required(VERSION 3.18)
project(furnace_wrap VERSION 0.1.0)

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(PkgConfig REQUIRED)
pkg_check_modules(SNDFILE REQUIRED sndfile)

set(FURNACE_SRC "${CMAKE_CURRENT_SOURCE_DIR}/../fooyin-furnace-plugin/furnace"
    CACHE PATH "Path to Furnace source tree")
set(FURNACE_BUILD "${FURNACE_SRC}/build-lib"
    CACHE PATH "Path to Furnace build-lib directory")

add_library(furnace_wrap SHARED
    src-wrap/furnace_wrap.cpp
)

target_include_directories(furnace_wrap SYSTEM PRIVATE
    ${FURNACE_SRC}/src
    ${FURNACE_SRC}/extern/blip_buf
    ${FURNACE_SRC}/extern/fmt/include
    ${FURNACE_SRC}/extern/fftw/api
    ${FURNACE_SRC}/extern/fftw
    ${FURNACE_SRC}/extern/vgsound_emu-modified
    ${FURNACE_SRC}/extern/zlib
    ${FURNACE_SRC}/extern/IconFontCppHeaders
    ${FURNACE_SRC}/src/icon
    ${FURNACE_BUILD}/extern/zlib
    ${FURNACE_BUILD}/extern/fftw
)

target_compile_definitions(furnace_wrap PRIVATE
    HAVE_SNDFILE
    HAVE_DIRENT_TYPE
)

# Link everything statically into the .so (except system libs)
target_link_libraries(furnace_wrap PRIVATE
    -Wl,--whole-archive
    ${FURNACE_BUILD}/libfurnace_lib.a
    -Wl,--no-whole-archive
    ${FURNACE_BUILD}/extern/fmt/libfmt.a
    ${FURNACE_BUILD}/extern/fftw/libfftw3.a
    ${FURNACE_BUILD}/extern/zlib/libz.a
    ${SNDFILE_LIBRARIES}
    pthread
    dl
    m
)

# Install next to audiocrate
set(AUDIOCRATE_DIR "/home/rednoobmusic/Documents/pythons/audiocrate"
    CACHE PATH "Target directory to install the .so into")
install(TARGETS furnace_wrap DESTINATION "${AUDIOCRATE_DIR}")
