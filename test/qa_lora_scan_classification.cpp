// SPDX-License-Identifier: ISC
//
// Tests for scan-path SF classification: detectMultiSf must report the
// correct spreading factor for synthetic preamble chirps across SF7-12.
// Exercises the SF11/12 OR-mode fix (C3) and serves as a regression
// gate for the two-pass probe refinement (C2).

#include <boost/ut.hpp>

#include <complex>
#include <cstdint>
#include <random>
#include <vector>

#include <gnuradio-4.0/lora/ChannelActivityDetector.hpp>
#include <gnuradio-4.0/lora/algorithm/tx_chain.hpp>

using namespace boost::ut;

namespace {

using cf32 = std::complex<float>;

constexpr uint32_t kBandwidth   = 62500;
constexpr uint8_t  kCr          = 4;
constexpr uint16_t kSync        = 0x12;
constexpr uint16_t kPreambleLen = 8;

/// Build a multi-SF CAD with os_factor=1 (same as ScanController probes).
[[nodiscard]] gr::lora::ChannelActivityDetector makeCadMultiSf() {
    gr::lora::ChannelActivityDetector cad;
    cad.os_factor  = 1U;
    cad.bandwidth  = kBandwidth;
    cad.dual_chirp = true;
    cad.initMultiSf();
    return cad;
}

/// Generate a preamble-only IQ burst at the given SF (os=1, Nyquist rate).
[[nodiscard]] std::vector<cf32> makePreamble(uint8_t sf) {
    const std::vector<uint8_t> payload = {'T'};
    return gr::lora::generate_frame_iq(payload, sf, kCr, /*os_factor=*/1U, kSync, kPreambleLen,
        /*has_crc=*/true,
        /*zero_pad=*/0U,
        /*ldro_mode=*/uint8_t{2}, kBandwidth);
}

} // namespace

const suite<"ScanClassification"> tests = [] {
    "detectMultiSf classifies SF7-12 correctly"_test = [] {
        auto cad = makeCadMultiSf();

        for (uint8_t sf = 7; sf <= 12; ++sf) {
            auto iq = makePreamble(sf);

            // detectMultiSf expects a SF12-sized buffer (2 * 4096 = 8192
            // samples at os=1). Pad shorter-SF frames to 8192.
            constexpr std::size_t kBufLen = (1U << 12) * 2;
            if (iq.size() < kBufLen) {
                iq.resize(kBufLen, cf32{0.f, 0.f});
            }

            auto r = cad.detectMultiSf(iq.data(), /*require_both=*/true);
            expect(r.detected) << "SF" << sf << " must be detected";
            expect(eq(r.sf, static_cast<uint32_t>(sf))) << "SF" << sf << " classified as SF" << r.sf;
        }
    };

    "SF12 detected in OR mode when only one window has energy"_test = [] {
        auto cad = makeCadMultiSf();

        // Generate a short SF12 burst — only ~1.5 symbols — so one
        // sub-window is populated and the other is noise-floor.
        constexpr uint32_t    N12  = 1U << 12; // 4096
        constexpr std::size_t kBuf = N12 * 2;  // 8192
        const auto            full = makePreamble(12);

        // Take 1.5 symbols of actual chirp + zero-pad the rest.
        const std::size_t chirpLen = N12 + N12 / 2;
        std::vector<cf32> partial(kBuf, cf32{0.f, 0.f});
        const std::size_t copyLen = std::min(chirpLen, full.size());
        std::copy_n(full.begin(), copyLen, partial.begin());

        // detectMultiSf runs AND-then-OR-fallback internally; SF11/12 use
        // OR-mode regardless. Explicit OR call documents scan intent.
        auto rOr = cad.detectMultiSf(partial.data(), /*require_both=*/false);

        expect(rOr.detected) << "SF12 OR-mode must detect partial burst";
        if (rOr.detected) {
            expect(eq(rOr.sf, 12U)) << "SF12 OR-mode classified as SF" << rOr.sf;
        }
    };

    "noise floor produces no detection"_test = [] {
        auto cad = makeCadMultiSf();

        std::mt19937                    rng(0xDEAD);
        std::normal_distribution<float> dist(0.f, 0.01f);
        constexpr std::size_t           kBuf = (1U << 12) * 2;
        std::vector<cf32>               noise(kBuf);
        for (auto& s : noise) {
            s = cf32{dist(rng), dist(rng)};
        }

        auto r = cad.detectMultiSf(noise.data(), /*require_both=*/true);
        expect(not r.detected) << "noise must not trigger detectMultiSf";
    };
};

int main() { return 0; }
