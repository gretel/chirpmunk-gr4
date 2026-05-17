# SPDX-License-Identifier: ISC

# --- Local test helpers (independent of GR4 core test infrastructure) ---
function(setup_test TEST_NAME)
    target_link_libraries(${TEST_NAME} PRIVATE
        gr-lora gnuradio-options gnuradio-core-group gr-testing ut)
    add_test(NAME ${TEST_NAME}
             COMMAND ${CMAKE_CROSSCOMPILING_EMULATOR} ${CMAKE_CURRENT_BINARY_DIR}/${TEST_NAME})
    set_tests_properties(${TEST_NAME} PROPERTIES TIMEOUT 30)
endfunction()

function(add_ut_test TEST_NAME)
    add_executable(${TEST_NAME} ${TEST_NAME}.cpp)
    setup_test(${TEST_NAME})
endfunction()

# --- Test targets ---
# TEST_VECTORS_DIR is set on every test that includes test_helpers.hpp (file-scope
# initializer of testVectorDir()), even those that don't call load_* at runtime.

function(add_vector_test TEST_NAME)
    add_ut_test(${TEST_NAME})
    target_compile_definitions(${TEST_NAME} PRIVATE
        TEST_VECTORS_DIR="${PROJECT_SOURCE_DIR}/test_vectors")
endfunction()

add_vector_test(qa_lora_tx)
add_vector_test(qa_lora_rx)
add_vector_test(qa_lora_splitter)
add_ut_test(qa_lora_udp)

# Config test: needs config.cpp (from apps/) + toml++ (not part of gr-lora)
add_executable(qa_lora_config qa_lora_config.cpp ${PROJECT_SOURCE_DIR}/apps/config.cpp)
setup_test(qa_lora_config)
target_link_libraries(qa_lora_config PRIVATE tomlplusplus::tomlplusplus)
target_compile_definitions(qa_lora_config PRIVATE
    CONFIG_TOML_PATH="${PROJECT_SOURCE_DIR}/apps/config-uhd.toml"
    GIT_REV="test")

add_vector_test(qa_lora_framesink)

add_vector_test(qa_lora_scan)
set_tests_properties(qa_lora_scan PROPERTIES TIMEOUT 60)

add_vector_test(qa_lora_cbor)
add_ut_test(qa_lora_tx_queue)

# Exception-safety tests for apps/tx_worker.cpp (process_tx_request).
add_executable(qa_lora_trx_worker qa_lora_trx_worker.cpp
    ${PROJECT_SOURCE_DIR}/apps/tx_worker.cpp
    ${PROJECT_SOURCE_DIR}/apps/config.cpp)
setup_test(qa_lora_trx_worker)
target_link_libraries(qa_lora_trx_worker PRIVATE tomlplusplus::tomlplusplus)
target_compile_definitions(qa_lora_trx_worker PRIVATE GIT_REV="test")

add_ut_test(qa_lora_l1_detector)
add_ut_test(qa_lora_primitives)
add_ut_test(qa_lora_vangelista)
add_ut_test(qa_lora_decode_chain)
add_ut_test(qa_lora_css_demod)
add_ut_test(qa_lora_preamble_sync)
add_ut_test(qa_lora_preamble_id)
add_ut_test(qa_lora_half_band_decimator)
add_ut_test(qa_lora_cad)
add_ut_test(qa_lora_scan_classification)
add_ut_test(qa_lora_sinks)
add_ut_test(qa_lora_antenna_combiner)

add_vector_test(qa_lora_multisf)
set_tests_properties(qa_lora_multisf PROPERTIES TIMEOUT 120)

add_ut_test(qa_lora_tag_forwarding)

add_ut_test(qa_lora_lib_pipeline)

# --- SoapyLoopback: hardware-free SoapySDR loopback testing ---
# Override with -DFETCHCONTENT_SOURCE_DIR_SOAPYLOOPBACK=<path> for local development.
# Skip when cross-compiling because SoapySDR is typically not in the cross sysroot.
if(NOT CMAKE_CROSSCOMPILING)
    FetchContent_Declare(
        SoapyLoopback
        GIT_REPOSITORY https://github.com/gretel/SoapyLoopback.git
        GIT_TAG        eab0395f5033da85adb5af6ccd30a5e7129a453e  # fix/tx-loopback-qa
    )
    FetchContent_MakeAvailable(SoapyLoopback)

    add_executable(qa_soapy_loopback qa_soapy_loopback.cpp)
    target_link_libraries(qa_soapy_loopback PRIVATE
        gr-lora gnuradio-options gnuradio-core-group gr-sdr gr-testing ut)
    target_compile_definitions(qa_soapy_loopback PRIVATE
        SOAPY_LOOPBACK_MODULE_PATH="$<TARGET_FILE:soapyloopback>")
    add_test(NAME qa_soapy_loopback
             COMMAND ${CMAKE_CROSSCOMPILING_EMULATOR} ${CMAKE_CURRENT_BINARY_DIR}/qa_soapy_loopback)
    set_tests_properties(qa_soapy_loopback PROPERTIES
        TIMEOUT 60
        ENVIRONMENT "ASAN_OPTIONS=detect_leaks=0")
    add_dependencies(qa_soapy_loopback soapyloopback)

    # Graph-builder property_map smoke test (needs SoapyLoopback).
    # Catches drift between apps/graph_builder.hpp and SoapySource
    # GR_MAKE_REFLECTABLE keys after upstream gnuradio4 rebases.
    add_executable(qa_lora_graph_builder qa_lora_graph_builder.cpp
        ${PROJECT_SOURCE_DIR}/apps/config.cpp)
    target_link_libraries(qa_lora_graph_builder PRIVATE
        gr-lora gnuradio-options gnuradio-core-group gr-sdr gr-testing ut
        tomlplusplus::tomlplusplus)
    target_compile_definitions(qa_lora_graph_builder PRIVATE
        SOAPY_LOOPBACK_MODULE_PATH="$<TARGET_FILE:soapyloopback>"
        CONFIG_TOML_PATH="${PROJECT_SOURCE_DIR}/apps/config-uhd.toml"
        GIT_REV="test")
    add_test(NAME qa_lora_graph_builder
             COMMAND ${CMAKE_CROSSCOMPILING_EMULATOR} ${CMAKE_CURRENT_BINARY_DIR}/qa_lora_graph_builder)
    set_tests_properties(qa_lora_graph_builder PROPERTIES
        TIMEOUT 30
        ENVIRONMENT "ASAN_OPTIONS=detect_leaks=0")
    add_dependencies(qa_lora_graph_builder soapyloopback)
endif()

# Test vector generator (not a test — run manually to regenerate vectors)
add_executable(generate_vectors generate_vectors.cpp)
target_link_libraries(generate_vectors PRIVATE gr-lora)
