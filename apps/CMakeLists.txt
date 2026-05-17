# SPDX-License-Identifier: ISC
#
# lora_trx — full-duplex LoRa transceiver (RX + TX) with CBOR/UDP interface.
# All I/O is CBOR over UDP. See CBOR-SCHEMA.md at the repo root.

if(NOT TARGET gr-sdr)
    message(STATUS "gr-sdr not available (SoapySDR not found) -- skipping hardware apps")
    return()
endif()

add_executable(lora_trx lora_trx.cpp config.cpp tx_worker.cpp)
target_link_libraries(lora_trx PRIVATE gnuradio-core-group gr-lora gr-sdr gr-testing tomlplusplus::tomlplusplus)

add_executable(lora_scan lora_scan.cpp config.cpp)
target_link_libraries(lora_scan PRIVATE gnuradio-core-group gr-lora gr-sdr gr-testing tomlplusplus::tomlplusplus)

# IIO block support — enables IIOSource-based on-device decode when cfg.uri is
# non-empty.  Uses libiio directly and the header-only gr4-incubator IIO blocks.
# The IIO headers live in the gr4-incubator submodule under apps/../gr4-incubator/
# which is not a CMake subdirectory — we create the import target here.
#
# Detection: check common sysroot paths, Homebrew prefix, and pkg-config.
if(CMAKE_CROSSCOMPILING AND CMAKE_SYSROOT)
    set(_gr4l_iio_prefix "${CMAKE_SYSROOT}/usr")
else()
    # Try Homebrew prefix first (macOS), then fallback to /usr
    set(_gr4l_iio_prefix_candidates "/opt/homebrew;/usr")
    foreach(_prefix IN LISTS _gr4l_iio_prefix_candidates)
        if(EXISTS "${_prefix}/include/iio.h")
            set(_gr4l_iio_prefix "${_prefix}")
            break()
        endif()
    endforeach()
    if(NOT DEFINED _gr4l_iio_prefix)
        message(STATUS "IIO support disabled (iio.h not found in any search path)")
        return()
    endif()
endif()

if(EXISTS "${_gr4l_iio_prefix}/include/iio.h")
    add_library(gr4l_blocks_iio_headers INTERFACE)
    add_library(gr4l::blocks_iio_headers ALIAS gr4l_blocks_iio_headers)
    target_include_directories(gr4l_blocks_iio_headers INTERFACE
        ${CMAKE_CURRENT_SOURCE_DIR}/../gr4-incubator/blocks/iio/include)
    target_include_directories(gr4l_blocks_iio_headers INTERFACE
        "${_gr4l_iio_prefix}/include")
    # Link libiio — prefer shared lib, fall back to static
    set(_iio_lib "")
    foreach(_libname libiio.so libiio.a libiio.dylib)
        if(EXISTS "${_gr4l_iio_prefix}/lib/${_libname}")
            set(_iio_lib "${_gr4l_iio_prefix}/lib/${_libname}")
            break()
        endif()
    endforeach()
    if(_iio_lib)
        target_link_libraries(gr4l_blocks_iio_headers INTERFACE
            "${_iio_lib}" gnuradio-core gnuradio-blocklib-core)
    else()
        # Last resort: let linker find it via -liio
        target_link_libraries(gr4l_blocks_iio_headers INTERFACE
            iio gnuradio-core gnuradio-blocklib-core)
    endif()
    target_link_libraries(lora_trx PRIVATE gr4l::blocks_iio_headers)
    target_link_libraries(lora_scan PRIVATE gr4l::blocks_iio_headers)
    target_compile_definitions(lora_trx PRIVATE GR4_LORA_HAVE_IIO)
    target_compile_definitions(lora_scan PRIVATE GR4_LORA_HAVE_IIO)
    message(STATUS "IIO support enabled (${_gr4l_iio_prefix}/include/iio.h)")
else()
    message(STATUS "IIO support disabled (iio.h not found at ${_gr4l_iio_prefix}/include/iio.h)")
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
    # through IIOSource/IIOSink instead of SoapySource/SoapySink.
    target_link_libraries(lora_trx PRIVATE gr4_lora::iio_blocks)
    target_link_libraries(lora_scan PRIVATE gr4_lora::iio_blocks)
    target_compile_definitions(lora_trx PRIVATE GR4_LORA_HAS_IIO=1)
    target_compile_definitions(lora_scan PRIVATE GR4_LORA_HAS_IIO=1)
endif()
