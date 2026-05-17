// SPDX-License-Identifier: ISC
//
// log.hpp — UTC timestamp + structured log helpers for chirpmunk-gr4.
//
// ts_now()                        → "2026-03-09T14:33:17.342Z"
// log_ts(level, tag, fmt, ...)    → writes to stderr with unified format:
//                                   "<ts>  <level>  <tag>  <message>"
//
// Level strings (caller pads to 5 chars):
//   "debug"  "info "  "warn "  "error"
//
// set_log_level("DEBUG"|"INFO"|"WARNING"|"ERROR")
//   Sets the minimum log level.  Messages below this level are suppressed.
//   Default: INFO.  Matches the Python logging convention.

#ifndef GR4_LORA_LOG_HPP
#define GR4_LORA_LOG_HPP

#include <atomic>
#include <cctype>
#include <chrono>
#include <cstdarg>
#include <cstdio>
#include <ctime>
#include <string>
#include <string_view>

namespace gr::lora {

/// Numeric log levels — matches Python logging convention.
enum class LogLevel : int {
    Debug   = 10,
    Info    = 20,
    Warning = 30,
    Error   = 40,
};

namespace detail {
inline std::atomic<int> g_log_level{static_cast<int>(LogLevel::Info)};

/// Map a level string ("debug", "info ", "warn ", "error") to numeric value.
[[nodiscard]] inline int level_from_str(const char* lvl) {
    if (lvl[0] == 'd' || lvl[0] == 'D') {
        return static_cast<int>(LogLevel::Debug);
    }
    if (lvl[0] == 'i' || lvl[0] == 'I') {
        return static_cast<int>(LogLevel::Info);
    }
    if (lvl[0] == 'w' || lvl[0] == 'W') {
        return static_cast<int>(LogLevel::Warning);
    }
    if (lvl[0] == 'e' || lvl[0] == 'E') {
        return static_cast<int>(LogLevel::Error);
    }
    return static_cast<int>(LogLevel::Info); // unknown → info
}
} // namespace detail

/// Set the minimum log level from a string.
/// Accepts: "DEBUG", "INFO", "WARNING", "ERROR" (case-insensitive, first char sufficient).
inline void set_log_level(std::string_view level_str) {
    if (level_str.empty()) {
        return;
    }
    detail::g_log_level.store(detail::level_from_str(level_str.data()), std::memory_order_relaxed);
}

/// Set the minimum log level from an enum.
inline void set_log_level(LogLevel level) { detail::g_log_level.store(static_cast<int>(level), std::memory_order_relaxed); }

/// Return current UTC time as ISO-8601 with milliseconds: "2026-03-09T14:33:17.342Z"
[[nodiscard]] inline std::string ts_now() {
    auto    now = std::chrono::system_clock::now();
    auto    t   = std::chrono::system_clock::to_time_t(now);
    auto    ms  = std::chrono::duration_cast<std::chrono::milliseconds>(now.time_since_epoch()) % 1000;
    std::tm tm{};
    gmtime_r(&t, &tm);
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ", tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday, tm.tm_hour, tm.tm_min, tm.tm_sec, static_cast<int>(ms.count()));
    return buf;
}

/// Log a structured line to stderr:  "<ts>  <level>  <tag>  <message>\n"
/// level: "debug", "info ", "warn ", "error"  (5 chars, caller must pad)
/// Messages below the current log level are suppressed.
// NOLINTNEXTLINE(cppcoreguidelines-pro-type-vararg)
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wformat-nonliteral"
inline void log_ts(const char* level, const char* tag, const char* fmt, ...) {
    if (detail::level_from_str(level) < detail::g_log_level.load(std::memory_order_relaxed)) {
        return;
    }
    auto ts = ts_now();
    std::fprintf(stderr, "%s  %s  %s  ", ts.c_str(), level, tag);
    va_list args;
    va_start(args, fmt);
    std::vfprintf(stderr, fmt, args);
    va_end(args);
    std::fputc('\n', stderr);
}
#pragma GCC diagnostic pop

} // namespace gr::lora

#endif // GR4_LORA_LOG_HPP
