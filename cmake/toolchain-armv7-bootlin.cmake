# SPDX-License-Identifier: ISC
#
# CMake toolchain file for cross-compiling chirpmunk-gr4 for armv7-eabihf (Cortex-A9)
# using the Bootlin prebuilt toolchain.
#
# Expected environment variable:
#   ARMV7_TOOLCHAIN_ROOT — path to the extracted toolchain directory, e.g.
#     /home/runner/toolchains/armv7-bootlin/armv7-eabihf--glibc--stable-2024.05-1
#
# Usage:
#   export ARMV7_TOOLCHAIN_ROOT=/path/to/armv7-eabihf--glibc--stable-2024.05-1
#   cmake -S . -B build -G Ninja \
#         -DCMAKE_TOOLCHAIN_FILE=cmake/toolchain-armv7-bootlin.cmake \
#         -DCMAKE_BUILD_TYPE=Release \
#         -DENABLE_LORA_APPS=OFF -DENABLE_LORA_BENCHMARKS=OFF \
#         -DWARNINGS_AS_ERRORS=OFF

if(NOT DEFINED ENV{ARMV7_TOOLCHAIN_ROOT})
    message(FATAL_ERROR
        "ARMV7_TOOLCHAIN_ROOT environment variable must be set to the Bootlin "
        "toolchain root (e.g. /home/runner/toolchains/armv7-bootlin/armv7-eabihf--glibc--stable-2024.05-1)")
endif()

set(_toolchain_root "$ENV{ARMV7_TOOLCHAIN_ROOT}")
set(_triple "arm-buildroot-linux-gnueabihf")

set(CMAKE_SYSTEM_NAME      Linux)
set(CMAKE_SYSTEM_PROCESSOR arm)

set(CMAKE_SYSROOT "${_toolchain_root}/${_triple}/sysroot")

set(CMAKE_C_COMPILER   "${_toolchain_root}/bin/${_triple}-gcc")
set(CMAKE_CXX_COMPILER "${_toolchain_root}/bin/${_triple}-g++")
set(CMAKE_AR           "${_toolchain_root}/bin/${_triple}-ar")
set(CMAKE_RANLIB       "${_toolchain_root}/bin/${_triple}-ranlib")
set(CMAKE_STRIP        "${_toolchain_root}/bin/${_triple}-strip")
set(CMAKE_LINKER       "${_toolchain_root}/bin/${_triple}-ld")

# ABI / CPU: Cortex-A9, NEON, hard-float VFPv3
set(_armv7_flags "-march=armv7-a -mcpu=cortex-a9 -mfpu=neon -mfloat-abi=hard")
set(CMAKE_C_FLAGS_INIT   "${_armv7_flags}")
set(CMAKE_CXX_FLAGS_INIT "${_armv7_flags}")

# Do not let CMake search the host filesystem for programs / libraries / includes.
set(CMAKE_FIND_ROOT_PATH "${CMAKE_SYSROOT}")
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)

# We're cross compiling to a different OS; skip the host libc++ work that
# toolchain-macos-homebrew-llvm.cmake does on macOS (Homebrew LLVM).
set(CMAKE_CROSSCOMPILING TRUE)
