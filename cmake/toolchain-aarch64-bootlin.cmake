# SPDX-License-Identifier: ISC
#
# CMake toolchain file for cross-compiling chirpmunk-gr4 for aarch64 (Raspberry Pi 4/5,
# generic ARMv8-A) using the Bootlin prebuilt toolchain.
#
# Expected environment variable:
#   AARCH64_TOOLCHAIN_ROOT — path to the extracted toolchain directory, e.g.
#     /home/runner/toolchains/aarch64-bootlin/aarch64--glibc--bleeding-edge-2025.08-1
#
# Usage:
#   export AARCH64_TOOLCHAIN_ROOT=/path/to/aarch64--glibc--bleeding-edge-2025.08-1
#   cmake -S . -B build -G Ninja \
#         -DCMAKE_TOOLCHAIN_FILE=cmake/toolchain-aarch64-bootlin.cmake \
#         -DCMAKE_BUILD_TYPE=Release \
#         -DENABLE_LORA_APPS=ON -DENABLE_LORA_BENCHMARKS=OFF \
#         -DWARNINGS_AS_ERRORS=OFF

if(NOT DEFINED ENV{AARCH64_TOOLCHAIN_ROOT})
    message(FATAL_ERROR
        "AARCH64_TOOLCHAIN_ROOT environment variable must be set to the Bootlin "
        "toolchain root (e.g. /home/runner/toolchains/aarch64-bootlin/aarch64--glibc--bleeding-edge-2025.08-1)")
endif()

set(_toolchain_root "$ENV{AARCH64_TOOLCHAIN_ROOT}")
set(_triple "aarch64-buildroot-linux-gnu")

set(CMAKE_SYSTEM_NAME      Linux)
set(CMAKE_SYSTEM_PROCESSOR aarch64)

set(CMAKE_SYSROOT "${_toolchain_root}/${_triple}/sysroot")

set(CMAKE_C_COMPILER   "${_toolchain_root}/bin/${_triple}-gcc")
set(CMAKE_CXX_COMPILER "${_toolchain_root}/bin/${_triple}-g++")
set(CMAKE_AR           "${_toolchain_root}/bin/${_triple}-ar")
set(CMAKE_RANLIB       "${_toolchain_root}/bin/${_triple}-ranlib")
set(CMAKE_STRIP        "${_toolchain_root}/bin/${_triple}-strip")
set(CMAKE_LINKER       "${_toolchain_root}/bin/${_triple}-ld")

# ABI / CPU: generic ARMv8-A — runs on both Raspberry Pi 4 (Cortex-A72) and
# Raspberry Pi 5 (Cortex-A76). No -mcpu tuning so one binary covers both.
set(_aarch64_flags "-march=armv8-a")
set(CMAKE_C_FLAGS_INIT   "${_aarch64_flags}")
set(CMAKE_CXX_FLAGS_INIT "${_aarch64_flags}")

# Do not let CMake search the host filesystem for programs / libraries / includes.
set(CMAKE_FIND_ROOT_PATH "${CMAKE_SYSROOT}")
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)

# We're cross compiling to a different OS; skip the host libc++ work that
# toolchain-macos-homebrew-llvm.cmake does on macOS (Homebrew LLVM).
set(CMAKE_CROSSCOMPILING TRUE)
