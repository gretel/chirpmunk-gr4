// SPDX-License-Identifier: ISC
//
// common.hpp — shared utilities for lora_trx and lora_scan.

#ifndef LORA_APPS_COMMON_HPP
#define LORA_APPS_COMMON_HPP

#include <atomic>
#include <chrono>
#include <complex>
#include <csignal>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <exception>
#include <fcntl.h>
#include <sstream>
#include <string>
#include <sys/wait.h>
#include <thread>
#include <unistd.h>

#include <gnuradio-4.0/lora/log.hpp>

// log_hardware_info() uses gr::blocks::sdr::soapy types — include
// SoapySource.hpp and/or SoapyRaiiWrapper.hpp before this header.

namespace lora_apps {

using cf32 = std::complex<float>;

inline std::atomic<bool> g_stop{false}; // NOLINT(cppcoreguidelines-avoid-non-const-global-variables)

inline void signal_handler(int /*sig*/) { g_stop.store(true, std::memory_order_relaxed); }

inline std::atomic<bool>& install_signal_handler() {
    std::ignore = std::signal(SIGINT, signal_handler);
    std::ignore = std::signal(SIGTERM, signal_handler);
    return g_stop;
}

/// Holds the app name pointer used by the std::terminate handler.
/// Set by :func:`install_terminate_handler` so the handler can
/// produce role-tagged diagnostics without globals at the call site.
inline const char* g_terminate_app_name = "app"; // NOLINT(cppcoreguidelines-avoid-non-const-global-variables)

[[noreturn]] inline void terminate_handler() {
    // Print whatever exception (if any) is in flight so the harness log
    // shows the cause before the process aborts. The default libc++abi
    // banner ("terminating due to uncaught exception of type X") elides
    // the .what() text — useless for triaging UHD/libusb crashes.
    if (auto eptr = std::current_exception()) {
        try {
            std::rethrow_exception(eptr);
        } catch (const std::exception& e) {
            std::fprintf(stderr, "%s: terminate(): %s\n", g_terminate_app_name, e.what());
        } catch (...) {
            std::fprintf(stderr, "%s: terminate(): unknown exception\n", g_terminate_app_name);
        }
    } else {
        std::fprintf(stderr, "%s: terminate(): no current exception\n", g_terminate_app_name);
    }
    std::fflush(stderr);
    std::abort();
}

/// Install a std::terminate handler that names the app and logs the
/// in-flight exception's .what() before aborting. Idempotent.
///
/// Catches the LIBUSB_ERROR_OTHER / uhd::usb_error class of crashes
/// that previously appeared as the libc++abi default banner without
/// the actual UHD error string. After installation, the harness's
/// _assert_alive can point at a log file that ends with the exact
/// failure message instead of the symbol-stripped abort tail.
inline void install_terminate_handler(const char* app_name) {
    g_terminate_app_name = app_name ? app_name : "app";
    std::set_terminate(terminate_handler);
}

struct HardwareInfo {
    std::string serial;
    std::size_t tx_channels{0};
    std::size_t rx_channels{0};
};

/// Log device + driver params at startup. Returns inferred channel
/// counts; serial is left empty (callers display "?"). Cannot call
/// SoapySDRDevice_enumerate here — on UHD that opens the device,
/// loads firmware, then closes, racing against the scheduler's
/// reinitDevice() and aborting Device::make. Keep this function
/// strictly out-of-band of the hardware.
inline HardwareInfo log_hardware_info(const char* app_name, const std::string& device, const std::string& device_param) {
    std::size_t nTx = 1, nRx = 1;
    if (device == "uhd" && device_param.find("type=b2") != std::string::npos) {
        nTx = 2;
        nRx = 4;
    }
    gr::lora::log_ts("debug", app_name, "hw probe   driver=%s (%zuT%zuR inferred)", device.c_str(), nTx, nRx);
    if (!device_param.empty()) {
        gr::lora::log_ts("debug", app_name, "hw params  %s", device_param.c_str());
    }
    return {.serial = "", .tx_channels = nTx, .rx_channels = nRx};
}

// --- SoapyBlock reliability defaults (shared by lora_trx and lora_scan) ---

constexpr uint32_t kSoapyChunkSize   = 512U << 4U; // 8192 samples per poll
constexpr uint32_t kSoapyMaxOverflow = 0U;         // disabled: upstream logs overflows without throttling
// readStream timeout: must exceed chunk airtime at the LOWEST sample rate we use,
// otherwise the IO thread busy-spins on SOAPY_SDR_TIMEOUT.  At 250 kS/s narrowband
// decode and 8192-sample chunks, one chunk = 32.768 ms.  100 ms gives 3x safety
// margin and is still <<100 ms at 16 MS/s scan rate (chunk = 0.512 ms there).
constexpr uint32_t kSoapyTimeoutUs = 100'000U;

inline gr::property_map soapy_reliability_defaults() {
    return {
        {"max_chunk_size", kSoapyChunkSize},
        {"max_overflow_count", gr::Size_t{kSoapyMaxOverflow}},
        {"max_time_out_us", kSoapyTimeoutUs},
        {"max_fragment_count", gr::Size_t{0U}}, // disabled
        {"verbose_overflow", false},
    };
}

inline void log_version_banner(const char* app_name, const char* git_rev) {
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wdate-time"
    gr::lora::log_ts("info ", app_name, "starting %s %s", app_name, git_rev);
    gr::lora::log_ts("debug", app_name, "build      " __DATE__ " " __TIME__);
#pragma GCC diagnostic pop
}

} // namespace lora_apps

#endif // LORA_APPS_COMMON_HPP
