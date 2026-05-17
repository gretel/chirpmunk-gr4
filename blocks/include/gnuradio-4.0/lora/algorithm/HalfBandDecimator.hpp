// SPDX-License-Identifier: ISC
#ifndef GNURADIO_LORA_ALGORITHM_HALFBAND_DECIMATOR_HPP
#define GNURADIO_LORA_ALGORITHM_HALFBAND_DECIMATOR_HPP

// Cascaded half-band FIR decimator for wideband channelization.
// Replaces the box-car (integrate-and-dump) decimator in ChannelSlot.
//
// Each stage uses a 23-tap half-band FIR with -45.1 dB stopband rejection
// (coefficients from scipy.signal.remez). Half-band symmetry means only
// 6 unique non-zero taps per stage, plus the center tap (0.5).
//
// Decimation by 2^N is achieved by cascading N stages. Each stage
// decimates by 2, so the cascade gives 2^N total decimation.

#include <algorithm>
#include <array>
#include <complex>
#include <cstddef>
#include <cstdint>
#include <span>
#include <vector>

namespace gr::lora {

using cf32 = std::complex<float>;

// 23-tap half-band FIR coefficients.
// Only the 12 unique taps are stored (symmetric: h[k] = h[22-k]).
// Even-indexed taps (except center) are zero for half-band filters.
// Non-zero taps: h[0], h[2], h[4], h[6], h[8], h[10], h[11], h[12], ...
// But h[10]=h[12], h[8]=h[14], etc. due to symmetry.
namespace halfband_detail {

// 6 unique non-zero outer coefficients (symmetric pairs)
inline constexpr std::array<float, 6> kCoeffs = {
    -7.193324475036694e-03f, // h[0]  = h[22]
    1.361692586579394e-02f,  // h[2]  = h[20]
    -2.628066406628573e-02f, // h[4]  = h[18]
    4.860759433245809e-02f,  // h[6]  = h[16]
    -9.646258355192097e-02f, // h[8]  = h[14]
    3.150052960795258e-01f,  // h[10] = h[12]
};

inline constexpr float       kCenterTap = 0.5f; // h[11]
inline constexpr std::size_t kFilterLen = 23;

} // namespace halfband_detail

// ─── HalfBandStage ──────────────────────────────────────────────────────────
// Single half-band FIR decimation stage (decimate by 2).
// Uses a circular delay line of 23 elements and exploits half-band symmetry
// (even-indexed taps are zero except at the boundaries).

struct HalfBandStage {
    std::array<cf32, halfband_detail::kFilterLen> delay{};
    std::size_t                                   writePos{0};    // circular write pointer
    std::size_t                                   sampleCount{0}; // phase tracking for decimate-by-2

    void reset() noexcept {
        delay.fill(cf32{0.f, 0.f});
        writePos    = 0;
        sampleCount = 0;
        resetBatch();
    }

    /// Process input samples, appending decimated output to `out`.
    /// For every 2 input samples, produces 1 output sample (decimate-by-2).
    void process(std::span<const cf32> input, std::vector<cf32>& out) {
        using namespace halfband_detail;

        for (const auto& sample : input) {
            // Write new sample into delay line at writePos
            delay[writePos] = sample;

            // Only compute output on every 2nd sample (decimate-by-2)
            if ((sampleCount & 1U) == 1U) {
                // Compute FIR output using half-band symmetry.
                // Tap k maps to delay[(writePos + k + 1) % 23] where
                // writePos points to the just-written newest sample (tap 0
                // of a "newest-first" view). But we want tap k to be the
                // (22-k)-th oldest sample, i.e., delay line index:
                //   idx(k) = (writePos - 22 + k + 23) % 23
                //          = (writePos + k + 1) % 23

                cf32 acc{0.f, 0.f};

                // Symmetric pairs: h[0]*( d[0]+d[22] ), h[2]*( d[2]+d[20] ), ...
                // Tap indices: 0,22  2,20  4,18  6,16  8,14  10,12
                for (std::size_t c = 0; c < kCoeffs.size(); ++c) {
                    const std::size_t k       = c * 2;              // tap index: 0, 2, 4, 6, 8, 10
                    const std::size_t kMirror = kFilterLen - 1 - k; // 22, 20, 18, 16, 14, 12

                    const std::size_t idx1 = (writePos + k + 1) % kFilterLen;
                    const std::size_t idx2 = (writePos + kMirror + 1) % kFilterLen;

                    acc += kCoeffs[c] * (delay[idx1] + delay[idx2]);
                }

                // Center tap: h[11] = 0.5
                const std::size_t centerIdx = (writePos + 11 + 1) % kFilterLen;
                acc += kCenterTap * delay[centerIdx];

                out.push_back(acc);
            }

            // Advance write pointer AFTER computing output
            writePos = (writePos + 1) % kFilterLen;
            ++sampleCount;
        }
    }

    // --- Linear-buffer batch FIR state (for stages 1+) ---
    std::vector<cf32> _tail;    // last kFilterLen-1 samples from previous call
    std::vector<cf32> _workBuf; // concatenation buffer: [tail | input]

    /// Initialize batch FIR state. Must be called before processBatch().
    /// @param maxInputSize  max expected input chunk size (for pre-allocation)
    void initBatch(std::size_t maxInputSize = 0) {
        using namespace halfband_detail;
        constexpr std::size_t kTailLen = kFilterLen - 1; // 22
        _tail.assign(kTailLen, cf32{0.f, 0.f});
        _workBuf.clear();
        if (maxInputSize > 0) {
            _workBuf.reserve(kTailLen + maxInputSize);
        }
    }

    /// Reset batch FIR state alongside the circular delay line state.
    void resetBatch() noexcept {
        using namespace halfband_detail;
        constexpr std::size_t kTailLen = kFilterLen - 1;
        if (_tail.size() == kTailLen) {
            std::fill(_tail.begin(), _tail.end(), cf32{0.f, 0.f});
        }
    }

    /// Batch FIR with linear buffer — no modulo indexing.
    /// Uses a persistent tail buffer for state across calls.
    /// Produces identical output to process() but with contiguous memory access.
    void processBatch(std::span<const cf32> input, std::vector<cf32>& out) {
        using namespace halfband_detail;
        constexpr std::size_t kTailLen = kFilterLen - 1; // 22

        if (input.empty()) {
            return;
        }

        // Build linear work buffer: [tail | input]
        const std::size_t totalLen = kTailLen + input.size();
        _workBuf.resize(totalLen);
        std::copy(_tail.begin(), _tail.end(), _workBuf.begin());
        std::copy(input.begin(), input.end(), _workBuf.begin() + static_cast<std::ptrdiff_t>(kTailLen));

        const cf32* buf = _workBuf.data();

        // Process each input sample; output on odd sampleCount (decimate-by-2).
        // In the linear buffer, sample i of the input is at buf[kTailLen + i].
        // The FIR taps span buf[kTailLen + i - 22 .. kTailLen + i],
        // i.e. buf[i .. i + 22] (since kTailLen == 22).
        for (std::size_t i = 0; i < input.size(); ++i) {
            if (((sampleCount + i) & 1U) == 1U) {
                // FIR output: symmetric pairs + center tap.
                // Tap k (oldest=0, newest=22) is at buf[i + k].
                cf32 acc{0.f, 0.f};
                for (std::size_t c = 0; c < kCoeffs.size(); ++c) {
                    const std::size_t k     = c * 2;              // 0, 2, 4, 6, 8, 10
                    const std::size_t kMirr = kFilterLen - 1 - k; // 22, 20, 18, 16, 14, 12
                    acc += kCoeffs[c] * (buf[i + k] + buf[i + kMirr]);
                }
                acc += kCenterTap * buf[i + 11];
                out.push_back(acc);
            }
        }

        // Save tail (last kTailLen samples of workBuf)
        std::copy(_workBuf.end() - static_cast<std::ptrdiff_t>(kTailLen), _workBuf.end(), _tail.begin());

        sampleCount += input.size();
    }

    /// Batch FIR with NCO mixing fused into input loading — no modulo indexing.
    /// Replaces processWithMix() for stage 0 in channelization.
    /// Uses the same linear-buffer approach as processBatch(): builds [tail | mixed_input]
    /// and accesses taps via simple buf[i + k] indexing.
    /// mixFn(i) returns the mixed sample for input index i.
    ///
    /// Requires initBatch() to have been called first (same as processBatch).
    template<typename MixFn>
    void processWithMixBatch(std::size_t count, MixFn&& mixFn, std::vector<cf32>& out) {
        using namespace halfband_detail;
        constexpr std::size_t kTailLen = kFilterLen - 1; // 22

        if (count == 0) {
            return;
        }

        // Build linear work buffer: [tail | mixed_input]
        const std::size_t totalLen = kTailLen + count;
        _workBuf.resize(totalLen);
        std::copy(_tail.begin(), _tail.end(), _workBuf.begin());

        // Apply NCO mix while loading input into the work buffer
        for (std::size_t i = 0; i < count; ++i) {
            _workBuf[kTailLen + i] = mixFn(i);
        }

        const cf32* buf = _workBuf.data();

        // FIR: identical to processBatch — linear indexing, no modulo
        for (std::size_t i = 0; i < count; ++i) {
            if (((sampleCount + i) & 1U) == 1U) {
                cf32 acc{0.f, 0.f};
                for (std::size_t c = 0; c < kCoeffs.size(); ++c) {
                    const std::size_t k     = c * 2;
                    const std::size_t kMirr = kFilterLen - 1 - k;
                    acc += kCoeffs[c] * (buf[i + k] + buf[i + kMirr]);
                }
                acc += kCenterTap * buf[i + 11];
                out.push_back(acc);
            }
        }

        // Save tail (last kTailLen samples of workBuf)
        std::copy(_workBuf.end() - static_cast<std::ptrdiff_t>(kTailLen), _workBuf.end(), _tail.begin());

        sampleCount += count;
    }

    /// Single-pass NCO mix + FIR: fills work buffer and computes FIR output
    /// in one fused loop. Eliminates the second traversal of the work buffer
    /// that the two-loop processWithMixBatch() requires, improving cache
    /// locality at 16 MS/s (8192 samples × 8 bytes = 64 KB per call).
    ///
    /// The FIR can only produce output once 22 (kTailLen) new samples have
    /// been written, because the filter taps span buf[i..i+22]. So we fill
    /// samples one at a time and compute FIR output inline when ready.
    ///
    /// Requires initBatch() to have been called first.
    void processNcoFirFused(std::span<const cf32> input, cf32& rot, const cf32& step, std::vector<cf32>& out) {
        using namespace halfband_detail;
        constexpr std::size_t kTailLen = kFilterLen - 1; // 22

        const std::size_t count = input.size();
        if (count == 0) {
            return;
        }

        // Build work buffer header from tail
        const std::size_t totalLen = kTailLen + count;
        _workBuf.resize(totalLen);
        std::copy(_tail.begin(), _tail.end(), _workBuf.begin());

        cf32* buf = _workBuf.data();

        // Single pass: NCO mix + FIR output fused
        for (std::size_t i = 0; i < count; ++i) {
            // NCO mix: write mixed sample into work buffer
            buf[kTailLen + i] = input[i] * rot;
            rot *= step;
            if ((i & 0x3FFU) == 0x3FFU) {
                rot /= std::abs(rot);
            }

            // FIR output on odd samples (decimate-by-2)
            if (((sampleCount + i) & 1U) == 1U) {
                cf32 acc{0.f, 0.f};
                for (std::size_t c = 0; c < kCoeffs.size(); ++c) {
                    const std::size_t k     = c * 2;
                    const std::size_t kMirr = kFilterLen - 1 - k;
                    acc += kCoeffs[c] * (buf[i + k] + buf[i + kMirr]);
                }
                acc += kCenterTap * buf[i + 11];
                out.push_back(acc);
            }
        }

        // Save tail
        std::copy(_workBuf.end() - static_cast<std::ptrdiff_t>(kTailLen), _workBuf.end(), _tail.begin());

        sampleCount += count;
    }
};

// ─── CascadedDecimator ──────────────────────────────────────────────────────
// N cascaded half-band stages for decimation by 2^N.
// Uses ping-pong scratch buffers to avoid allocations in the hot path.

struct CascadedDecimator {
    std::vector<HalfBandStage> stages;
    std::vector<cf32>          scratch0; // ping buffer
    std::vector<cf32>          scratch1; // pong buffer

    /// Initialize with nStages half-band stages (total decimation = 2^nStages).
    /// maxChunkSize is the maximum expected input chunk size (for pre-allocation).
    void init(std::size_t nStages, std::size_t maxChunkSize = 8192) {
        stages.resize(nStages);
        // Each stage's input is half the previous stage's output
        std::size_t stageInput = maxChunkSize;
        for (auto& s : stages) {
            s.reset();
            s.initBatch(stageInput);
            stageInput = (stageInput + 1) / 2; // decimate-by-2
        }
        // Pre-allocate scratch buffers for the largest possible intermediate size
        scratch0.reserve(maxChunkSize);
        scratch1.reserve(maxChunkSize);
    }

    /// Reset all stage state (delay lines, phase counters).
    void reset() {
        for (auto& s : stages) {
            s.reset();
        }
    }

    /// Process input through all stages, appending decimated output to `out`.
    /// The input is decimated by 2^nStages total.
    void process(std::span<const cf32> input, std::vector<cf32>& out) {
        if (stages.empty()) {
            out.insert(out.end(), input.begin(), input.end());
            return;
        }

        // First stage reads from input
        scratch0.clear();
        stages[0].process(input, scratch0);

        // Subsequent stages ping-pong between scratch buffers
        for (std::size_t i = 1; i < stages.size(); ++i) {
            auto& src = (i & 1U) ? scratch0 : scratch1;
            auto& dst = (i & 1U) ? scratch1 : scratch0;
            dst.clear();
            stages[i].process(std::span<const cf32>(src), dst);
        }

        // Append final buffer to output
        auto& result = (stages.size() & 1U) ? scratch0 : scratch1;
        out.insert(out.end(), result.begin(), result.end());
    }

    /// Process input through all stages, returning decimated output.
    /// The input is decimated by 2^nStages total.
    [[nodiscard]] std::vector<cf32> process(std::span<const cf32> input) {
        std::vector<cf32> out;
        process(input, out);
        return out;
    }

    /// Process input with NCO mixing fused into the first half-band stage.
    /// Eliminates the intermediate mixed buffer — each wideband sample is read
    /// once from the input span and never materialized as a mixed result.
    /// Updates rot in-place for phase continuity across calls.
    void processWithNco(std::span<const cf32> input, std::vector<cf32>& out, cf32& rot, const cf32& step) {
        if (stages.empty()) {
            // No FIR stages — just mix directly to output
            for (std::size_t i = 0; i < input.size(); ++i) {
                out.push_back(input[i] * rot);
                rot *= step;
                if ((i & 0x3FFU) == 0x3FFU) {
                    rot /= std::abs(rot);
                }
            }
            return;
        }

        // First stage: single-pass NCO mix + FIR
        const std::size_t lastStage = stages.size() - 1;
        auto&             stage0dst = (lastStage == 0) ? out : scratch0;
        if (&stage0dst != &out) {
            stage0dst.clear();
        }
        stages[0].processNcoFirFused(input, rot, step, stage0dst);

        // Remaining stages: last stage writes directly to `out`
        for (std::size_t i = 1; i <= lastStage; ++i) {
            auto& src = (i & 1U) ? scratch0 : scratch1;
            auto& dst = (i == lastStage) ? out : ((i & 1U) ? scratch1 : scratch0);
            if (&dst != &out) {
                dst.clear();
            }
            stages[i].process(std::span<const cf32>(src), dst);
        }
    }

    /// Batch-process input through all stages using linear-buffer FIR for stages 1+.
    /// Stage 0 uses the existing circular process(). Stages 1+ use processBatch()
    /// which eliminates modulo indexing and enables contiguous memory access.
    void processBatch(std::span<const cf32> input, std::vector<cf32>& out) {
        if (stages.empty()) {
            out.insert(out.end(), input.begin(), input.end());
            return;
        }

        // Stage 0: circular FIR (same as existing)
        scratch0.clear();
        stages[0].process(input, scratch0);
        const std::size_t s0_size = scratch0.size(); // capture before ping-pong overwrites

        // Stages 1+: linear-buffer batch FIR
        for (std::size_t i = 1; i < stages.size(); ++i) {
            auto& src = (i & 1U) ? scratch0 : scratch1;
            auto& dst = (i & 1U) ? scratch1 : scratch0;
            dst.clear();
            stages[i].processBatch(std::span<const cf32>(src), dst);
        }

        auto& result = (stages.size() & 1U) ? scratch0 : scratch1;
        // DEBUG: per-stage magnitude trace (s0_size captured before ping-pong) (disabled)
        if (false)
        {
            float magN = 0.f;
            if (!result.empty()) { for (auto& v : result) magN += std::abs(v); magN /= static_cast<float>(result.size()); }
            std::fprintf(stderr, "AA_STAGE n=%zu in=%zu s0_out=%zu final=%zu mag_final=%.6f\n",
                stages.size(), input.size(), s0_size, result.size(), static_cast<double>(magN));
            std::fflush(stderr);
        }
        out.insert(out.end(), result.begin(), result.end());
    }

    /// Batch-process with NCO mixing fused into stage 0.
    /// Stage 0 uses single-pass NCO+FIR fusion (processNcoFirFused).
    /// Stages 1+ use linear-buffer batch FIR (processBatch).
    void processWithNcoBatch(std::span<const cf32> input, std::vector<cf32>& out, cf32& rot, const cf32& step) {
        if (stages.empty()) {
            for (std::size_t i = 0; i < input.size(); ++i) {
                out.push_back(input[i] * rot);
                rot *= step;
                if ((i & 0x3FFU) == 0x3FFU) {
                    rot /= std::abs(rot);
                }
            }
            return;
        }

        // Stage 0: single-pass NCO mix + FIR (no lambda, no second traversal)
        const std::size_t lastStage = stages.size() - 1;
        auto&             stage0dst = (lastStage == 0) ? out : scratch0;
        if (&stage0dst != &out) {
            stage0dst.clear();
        }
        stages[0].processNcoFirFused(input, rot, step, stage0dst);

        // Stages 1+: linear-buffer batch FIR.
        // Last stage writes directly to `out`, avoiding a final memcpy.
        for (std::size_t i = 1; i <= lastStage; ++i) {
            auto& src = (i & 1U) ? scratch0 : scratch1;
            auto& dst = (i == lastStage) ? out : ((i & 1U) ? scratch1 : scratch0);
            if (&dst != &out) {
                dst.clear();
            }
            stages[i].processBatch(std::span<const cf32>(src), dst);
        }
    }
};

} // namespace gr::lora

#endif // GNURADIO_LORA_ALGORITHM_HALFBAND_DECIMATOR_HPP
