# SPDX-License-Identifier: ISC
cmake_minimum_required(VERSION 3.27)
project(chirpmunk-gr4 CXX)

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_VISIBILITY_PRESET hidden)
set(CMAKE_VISIBILITY_INLINES_HIDDEN ON)
set(CMAKE_POSITION_INDEPENDENT_CODE TRUE)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# Build type: default to RelWithAssert (optimised, assertions enabled)
if(CMAKE_CXX_COMPILER_ID MATCHES "(Clang|GNU|Intel)")
    set(CMAKE_CXX_FLAGS_RELWITHASSERT "-O2 -DASSERT_ENABLED -ffunction-sections -fdata-sections"
        CACHE INTERNAL "Flags for RelWithAssert builds.")
    if(NOT CMAKE_BUILD_TYPE)
        set(CMAKE_BUILD_TYPE "RelWithAssert" CACHE STRING "Build type" FORCE)
    endif()
endif()

# Clang / GCC colour output + libc++
if(CMAKE_CXX_COMPILER_ID MATCHES ".*Clang")
    add_compile_options(-stdlib=libc++ -fcolor-diagnostics)
    set(CMAKE_EXE_LINKER_FLAGS "${CMAKE_EXE_LINKER_FLAGS} -stdlib=libc++")
elseif(CMAKE_CXX_COMPILER_ID STREQUAL "GNU")
    add_compile_options(-fdiagnostics-color=always)
endif()

# --- Reproducible builds ---
add_compile_options(-Werror=date-time)
if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
    add_compile_options(-ffile-prefix-map=${PROJECT_SOURCE_DIR}=.)
endif()

# --- Security hardening ---
if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
    add_compile_options(-ftrivial-auto-var-init=zero)
endif()

# --- toml++ (header-only TOML parser for config file support) ---
include(FetchContent)
FetchContent_Declare(
    tomlplusplus
    GIT_REPOSITORY https://github.com/marzer/tomlplusplus.git
    GIT_TAG        v3.4.0
    GIT_SHALLOW    TRUE
)
FetchContent_MakeAvailable(tomlplusplus)

# --- GR4 (provides gnuradio-core, gnuradio-algorithm, gr-sdr, gr-testing, ut, …) ---
# When consumed via FetchContent (e.g. by opendigitizer), the parent project
# already provides gnuradio4 — skip the submodule to avoid double inclusion.
if(NOT TARGET gnuradio-core)
    set(GR_ENABLE_HTTP "OFF" CACHE STRING "" FORCE)
    add_subdirectory(gnuradio4)
endif()

# gnuradio-blocklib-core (STATIC) and gnuradio-core (STATIC) have a circular
# symbol dependency: core's PluginLoader.cpp references globalBlockRegistry()
# defined in blocklib-core. On Linux, wrap both in a RESCAN group so the GNU
# linker resolves symbols across both archives. On macOS, ld64 rescans by
# default — just list both libraries and let the linker handle it.
if(TARGET gnuradio-blocklib-core AND TARGET gnuradio-core)
    add_library(gnuradio-core-group INTERFACE)
    if(APPLE)
        target_link_libraries(gnuradio-core-group
            INTERFACE gnuradio-blocklib-core gnuradio-core)
    else()
        target_link_libraries(gnuradio-core-group
            INTERFACE "$<LINK_GROUP:RESCAN,gnuradio-blocklib-core,gnuradio-core>")
    endif()
endif()

# --- LoRa block library ---
add_subdirectory(blocks)

# --- Tests ---
option(ENABLE_LORA_TESTING "Enable chirpmunk-gr4 test suite" ON)
if(ENABLE_LORA_TESTING)
    enable_testing()
    add_subdirectory(test)
endif()

# --- Benchmarks ---
option(ENABLE_LORA_BENCHMARKS "Enable chirpmunk-gr4 benchmarks" ON)
if(ENABLE_LORA_BENCHMARKS)
    add_subdirectory(benchmarks)
endif()

# --- IIO block library (header-only, sourced from gr4-incubator) ---
# Phase A: external sibling path. After the iio branch is published the path will
# move to a real git submodule and this option flips to ON by default.
option(GR4_LORA_BUILD_IIO "Build IIO source/sink blocks (requires libiio + gr4-incubator headers)" OFF)
set(GR4_INCUBATOR_DIR "${CMAKE_CURRENT_SOURCE_DIR}/gr4-incubator"
    CACHE PATH "Path to gr4-incubator source tree containing blocks/iio/include")
if(GR4_LORA_BUILD_IIO)
    if(NOT EXISTS "${GR4_INCUBATOR_DIR}/blocks/iio/include/gnuradio-4.0/iio/IIOSource.hpp")
        message(FATAL_ERROR "GR4_LORA_BUILD_IIO=ON but '${GR4_INCUBATOR_DIR}/blocks/iio/include/gnuradio-4.0/iio/IIOSource.hpp' not found. Set GR4_INCUBATOR_DIR.")
    endif()
    find_package(PkgConfig REQUIRED)
    pkg_check_modules(LIBIIO REQUIRED IMPORTED_TARGET libiio)
    add_library(gr4_lora_iio_blocks INTERFACE)
    target_include_directories(gr4_lora_iio_blocks INTERFACE "${GR4_INCUBATOR_DIR}/blocks/iio/include")
    target_link_libraries(gr4_lora_iio_blocks INTERFACE PkgConfig::LIBIIO)
    if(TARGET gnuradio-core-group)
        target_link_libraries(gr4_lora_iio_blocks INTERFACE gnuradio-core-group)
    endif()
    add_library(gr4_lora::iio_blocks ALIAS gr4_lora_iio_blocks)
    message(STATUS "GR4_LORA_BUILD_IIO   : ON (headers='${GR4_INCUBATOR_DIR}/blocks/iio/include')")
endif()

# --- Hardware applications (require UHD or SoapySDR) ---
option(ENABLE_LORA_APPS "Enable chirpmunk-gr4 hardware applications" ON)
if(ENABLE_LORA_APPS)
    # SoapySDR may already be found by GR4 subdirectory; if not, search common paths
    if(NOT TARGET SoapySDR)
        list(APPEND CMAKE_PREFIX_PATH /opt/homebrew)
        find_package(SoapySDR CONFIG QUIET)
    endif()
    add_subdirectory(apps)

    # IIO graph-builder property_map smoke test (requires libiio + gr4-incubator).
    # Must be registered AFTER add_subdirectory(apps) where the gr4l::blocks_iio_headers
    # target is created. Does NOT start the scheduler — property reflection only.
    if(TARGET gr4l::blocks_iio_headers AND NOT CMAKE_CROSSCOMPILING)
        message(STATUS "IIO test: qa_lora_graph_builder_iio enabled")
        add_executable(qa_lora_graph_builder_iio
            test/qa_lora_graph_builder_iio.cpp apps/config.cpp)
        target_link_libraries(qa_lora_graph_builder_iio PRIVATE
            gr-lora gnuradio-options gnuradio-core-group gr-sdr gr-testing ut
            tomlplusplus::tomlplusplus gr4l::blocks_iio_headers)
        target_compile_definitions(qa_lora_graph_builder_iio PRIVATE
            CONFIG_TOML_PATH="${PROJECT_SOURCE_DIR}/apps/config-iio-local.toml"
            GIT_REV="test"
            GR4_LORA_HAVE_IIO
            GR4_LORA_HAS_IIO=1)
        add_test(NAME qa_lora_graph_builder_iio
                 COMMAND ${CMAKE_CROSSCOMPILING_EMULATOR} ${CMAKE_CURRENT_BINARY_DIR}/qa_lora_graph_builder_iio)
        set_tests_properties(qa_lora_graph_builder_iio PROPERTIES TIMEOUT 30)
    endif()
endif()
