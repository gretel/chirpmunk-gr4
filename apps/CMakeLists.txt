# SPDX-License-Identifier: ISC
#
# lora_trx — full-duplex LoRa transceiver (RX + TX) with CBOR/UDP interface.
# All I/O is CBOR over UDP. See CBOR-SCHEMA.md at the repo root.

if(NOT TARGET gr-sdr)
    message(STATUS "gr-sdr not available (SoapySDR not found) -- skipping hardware apps")
    return()
endif()

add_library(gr4l_blocks_soapysdr_headers INTERFACE)
add_library(gr4l::blocks_soapysdr_headers ALIAS gr4l_blocks_soapysdr_headers)
target_include_directories(gr4l_blocks_soapysdr_headers INTERFACE
    ${CMAKE_CURRENT_SOURCE_DIR}/../gr4-incubator/blocks/soapysdr/include)

add_executable(lora_trx lora_trx.cpp config.cpp tx_worker.cpp)
target_link_libraries(lora_trx PRIVATE gnuradio-core-group gr-lora gr-sdr gr-testing tomlplusplus::tomlplusplus gr4l::blocks_soapysdr_headers)

add_executable(lora_scan lora_scan.cpp config.cpp)
target_link_libraries(lora_scan PRIVATE gnuradio-core-group gr-lora gr-sdr gr-testing tomlplusplus::tomlplusplus gr4l::blocks_soapysdr_headers)

# IIO block support — enables IIOSource-based on-device decode when cfg.uri is
# non-empty.  Uses libiio directly and the header-only gr4-incubator IIO blocks.
# The IIO headers live in the gr4-incubator submodule under apps/../gr4-incubator/
# which is not a CMake subdirectory — we create the import target here.
#
# Detection: check common sysroot paths, Homebrew prefix, and pkg-config.
# libiio v0.x: include/iio.h  |  v1.0: include/iio/iio.h (incompatible API).
# v1.0 with LIBIIO_COMPAT provides old symbols in libiio.so.0 — use v0.26
# header (from Cellar or fetched from GitHub) + compat library.
set(_gr4l_iio_v0 FALSE)
if(CMAKE_CROSSCOMPILING AND CMAKE_SYSROOT)
    set(_gr4l_iio_prefix "${CMAKE_SYSROOT}/usr")
    if(EXISTS "${_gr4l_iio_prefix}/include/iio.h")
        set(_gr4l_iio_v0 TRUE)
    else()
        message(STATUS "IIO support disabled (iio.h not found in sysroot)")
    endif()
else()
    # Try v0.26 header first: Homebrew prefix, /usr, then Cellar
    set(_gr4l_iio_prefix_candidates "/opt/homebrew;/usr")
    foreach(_prefix IN LISTS _gr4l_iio_prefix_candidates)
        if(EXISTS "${_prefix}/include/iio.h")
            set(_gr4l_iio_prefix "${_prefix}")
            set(_gr4l_iio_v0 TRUE)
            break()
        endif()
    endforeach()
    # v0.26 header not at prefix — check Cellar directly
    if(NOT _gr4l_iio_v0)
        if(EXISTS "/opt/homebrew/Cellar/libiio/0.26/include/iio.h")
            set(_gr4l_iio_prefix "/opt/homebrew/Cellar/libiio/0.26")
            set(_gr4l_iio_v0 TRUE)
        endif()
    endif()
    # Still not found — fetch v0.26 iio.h from GitHub for compat
    if(NOT _gr4l_iio_v0 AND EXISTS "/opt/homebrew/include/iio/iio.h")
        # v1.0 is installed — fetch compat header
        set(_gr4l_iio_compat_dir "${CMAKE_BINARY_DIR}/_libiio_compat")
        set(_gr4l_iio_compat_hdr "${_gr4l_iio_compat_dir}/include/iio.h")
        if(NOT EXISTS "${_gr4l_iio_compat_hdr}")
            file(MAKE_DIRECTORY "${_gr4l_iio_compat_dir}/include")
            message(STATUS "Fetching libiio v0.26 iio.h for compat...")
            file(DOWNLOAD
                "https://raw.githubusercontent.com/analogdevicesinc/libiio/v0.26/iio.h"
                "${_gr4l_iio_compat_hdr}"
                TIMEOUT 10
                STATUS _fetch_status)
            list(GET _fetch_status 0 _fetch_rc)
            if(_fetch_rc EQUAL 0)
                set(_gr4l_iio_prefix "${_gr4l_iio_compat_dir}")
                set(_gr4l_iio_v0 TRUE)
            else()
                message(WARNING "Failed to fetch libiio v0.26 iio.h")
            endif()
        else()
            set(_gr4l_iio_prefix "${_gr4l_iio_compat_dir}")
            set(_gr4l_iio_v0 TRUE)
        endif()
    endif()
    if(NOT _gr4l_iio_v0)
        message(STATUS "IIO support disabled (iio.h not found)")
    endif()
endif()

if(_gr4l_iio_v0)
    add_library(gr4l_blocks_iio_headers INTERFACE)
    add_library(gr4l::blocks_iio_headers ALIAS gr4l_blocks_iio_headers)
    target_include_directories(gr4l_blocks_iio_headers INTERFACE
        ${CMAKE_CURRENT_SOURCE_DIR}/../gr4-incubator/blocks/iio/include)
    target_include_directories(gr4l_blocks_iio_headers INTERFACE
        "${_gr4l_iio_prefix}/include")
    # libiio v0.26 header has old-style casts
    target_compile_options(gr4l_blocks_iio_headers INTERFACE -Wno-error=old-style-cast)
    # Prefer libiio.so.0 (v1.0 compat library with v0.26 symbols) over libiio.dylib (v1.0 API)
    set(_iio_lib "")
    foreach(_libname libiio.so.0 libiio.so libiio.a libiio.dylib)
        foreach(_libdir "${_gr4l_iio_prefix}/lib" "/opt/homebrew/lib" "/opt/homebrew/Cellar/libiio/HEAD-32af424/lib")
            if(EXISTS "${_libdir}/${_libname}" AND NOT _iio_lib)
                set(_iio_lib "${_libdir}/${_libname}")
                break()
            endif()
        endforeach()
    endforeach()
    if(_iio_lib)
        target_link_libraries(gr4l_blocks_iio_headers INTERFACE
            "${_iio_lib}" gnuradio-core gnuradio-blocklib-core)
        # libiio.so.0 dlopens iio.framework/Versions/1.0/iio — create in build tree
        if(_iio_lib MATCHES "libiio\\.so\\.0")
            find_file(_gr4l_iio_v1_dylib libiio.1.0.dylib
                HINTS /opt/homebrew/Cellar/libiio/HEAD-32af424/lib NO_DEFAULT_PATH)
            if(_gr4l_iio_v1_dylib)
                set(_iio_fw "${CMAKE_BINARY_DIR}/iio.framework/Versions/1.0")
                file(MAKE_DIRECTORY "${_iio_fw}")
                file(CREATE_LINK "${_gr4l_iio_v1_dylib}" "${_iio_fw}/iio" SYMBOLIC)
                file(CREATE_LINK "Versions/1.0" "${CMAKE_BINARY_DIR}/iio.framework/Versions/Current" SYMBOLIC)
                file(CREATE_LINK "Versions/Current/iio" "${CMAKE_BINARY_DIR}/iio.framework/iio" SYMBOLIC)
            endif()
        endif()
    else()
        target_link_libraries(gr4l_blocks_iio_headers INTERFACE
            iio gnuradio-core gnuradio-blocklib-core)
    endif()
    target_link_libraries(lora_trx PRIVATE gr4l::blocks_iio_headers)
    target_link_libraries(lora_scan PRIVATE gr4l::blocks_iio_headers)
    target_compile_definitions(lora_trx PRIVATE GR4_LORA_HAVE_IIO)
    target_compile_definitions(lora_scan PRIVATE GR4_LORA_HAVE_IIO)
    message(STATUS "IIO support enabled (${_gr4l_iio_prefix}/include/iio.h)")
endif()

# Version: pass -DGIT_REV=<hash> at configure time, or auto-detect from git.
if(NOT DEFINED GIT_REV)
    execute_process(
        COMMAND git rev-parse --short HEAD
        WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"
        OUTPUT_VARIABLE GIT_REV
        OUTPUT_STRIP_TRAILING_WHITESPACE
        ERROR_QUIET
        RESULT_VARIABLE _git_result)
    if(NOT _git_result EQUAL 0)
        set(GIT_REV "dev")
    endif()
endif()

target_compile_definitions(lora_trx PRIVATE GIT_REV="${GIT_REV}")
target_compile_definitions(lora_scan PRIVATE GIT_REV="${GIT_REV}")

# tx_implicit — standalone implicit-header IQ generator for end-to-end validation.
# Requires libiio for IIO TX (PlutoSDR). On non-IIO builds, tx_implicit is omitted.
# Works with both gr4_lora::iio_blocks (CI/GR4_LORA_BUILD_IIO path) and
# gr4l::blocks_iio_headers (local macOS Homebrew path).
if(TARGET gr4_lora::iio_blocks OR GR4_LORA_HAVE_IIO OR TARGET gr4l::blocks_iio_headers)
    add_executable(tx_implicit tx_implicit.cpp)
    target_include_directories(tx_implicit PRIVATE ${CMAKE_SOURCE_DIR}/gnuradio4/blocks/include ${CMAKE_SOURCE_DIR}/blocks/include)
    if(TARGET gr4_lora::iio_blocks)
        target_link_libraries(tx_implicit PRIVATE gr4_lora::iio_blocks)
    else()
        target_link_libraries(tx_implicit PRIVATE gr4l::blocks_iio_headers)
    endif()
    target_compile_definitions(tx_implicit PRIVATE GIT_REV="${GIT_REV}")
    # Apple Clang errors on old-style cast in system iio.h — suppress
    target_compile_options(tx_implicit PRIVATE -Wno-error=old-style-cast)
    message(STATUS "tx_implicit: enabled (IIO available)")
else()
    message(STATUS "tx_implicit: skipped (no IIO)")
endif()

# IIO smoke (RX-only) — opt-in: requires gr4-incubator iio block + libiio.
if(GR4_LORA_BUILD_IIO AND TARGET gr4_lora::iio_blocks)
    # iio_smoke is optional — only build if CLI11 is available (it's a dev
    # helper, not required for hardware operation). On cross-builds where
    # CLI11 is not in the sysroot, iio_smoke is simply skipped.
    find_package(CLI11 CONFIG QUIET)
    if(CLI11_FOUND OR TARGET CLI11::CLI11)
        add_executable(iio_smoke iio_smoke.cpp)
        target_link_libraries(iio_smoke PRIVATE gr4_lora::iio_blocks gr-testing CLI11::CLI11)
        target_compile_definitions(iio_smoke PRIVATE GIT_REV="${GIT_REV}")
    endif()

    # Wire IIO backend into lora_trx + lora_scan so config.driver="iio" routes
    # through IIOSource/IIOSink instead of SoapyDevSource/SoapyDevSink.
    target_link_libraries(lora_trx PRIVATE gr4_lora::iio_blocks)
    target_link_libraries(lora_scan PRIVATE gr4_lora::iio_blocks)
    target_compile_definitions(lora_trx PRIVATE GR4_LORA_HAS_IIO=1)
    target_compile_definitions(lora_scan PRIVATE GR4_LORA_HAS_IIO=1)
endif()
