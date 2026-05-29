// SPDX-License-Identifier: ISC
#ifndef GNURADIO_LORA_PHY_DECODE_CHAIN_HPP
#define GNURADIO_LORA_PHY_DECODE_CHAIN_HPP

// DecodeChain drives the post-demod pipeline:
//   Gray-decode + downshift -> deinterleave -> Hamming decode
//     -> header parse (block 0)
//     -> dewhiten + CRC verify (terminal)
//
// Input: post-demod bins (raw argmax indices in [0, 2^sf), as returned by
// CssDemod::demodHard). The class internally undoes the +1 cyclic offset,
// undoes Gray demapping, and feeds the result into the existing (unchanged)
// helpers in algorithm/{interleaving,hamming,crc,tx_chain}.hpp.
//
// State machine:
//   1. CollectingHeader: accumulate 8 symbols, run header interleaver block
//      (sf_app = sf-2, cw_len = 8, cr_app = 4), parse first 5 nibbles via
//      parse_explicit_header. On success, compute frame_symb_numb and
//      transition to CollectingPayload. On failure, transition to Failed
//      and return a degenerate FrameResult.
//   2. CollectingPayload: accumulate 4+cr symbols per block, run payload
//      interleaver block (sf_app = sf or sf-2 if LDRO, cw_len = 4+cr,
//      cr_app = cr). Append nibbles to the running buffer. When the total
//      number of symbols received >= frame_symb_numb, dewhiten + verify CRC
//      and return the FrameResult.
//   3. Done / Failed: subsequent push_symbol calls return std::nullopt.
//
// All numerics match the reference LoRa payload-decode pipeline (Gray
// demap → diagonal interleaver → (4,cr) Hamming → dewhitening → CRC).

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <optional>
#include <span>
#include <vector>

#include <gnuradio-4.0/lora/algorithm/crc.hpp>
#include <gnuradio-4.0/lora/algorithm/hamming.hpp>
#include <gnuradio-4.0/lora/algorithm/interleaving.hpp>
#include <gnuradio-4.0/lora/algorithm/tx_chain.hpp>
#include <gnuradio-4.0/lora/phy/Types.hpp>

namespace gr::lora::phy {

class DecodeChain {
public:
    struct Config {
        uint8_t  sf           = 7;      ///< spreading factor [7, 12]
        uint32_t bandwidth_hz = 125000; ///< for LDRO determination
        bool     soft_decode  = false;  ///< (reserved; phase 4 will use)
    };

    void init(const Config& cfg) noexcept {
        _cfg  = cfg;
        _N    = uint32_t{1} << cfg.sf;
        _ldro = gr::lora::needs_ldro(cfg.sf, cfg.bandwidth_hz);
        reset();
    }

    void reset() noexcept {
        _phase = Phase::CollectingHeader;
        _sym_buf.clear();
        _nibbles.clear();
        _total_symbols_rx  = 0;
        _frame_symb_numb   = 0;
        _cr                = 0;
        _pay_len           = 0;
        _has_crc           = false;
        _payload_syms_seen = 0;
        _payload_lsb0_hits = 0;
    }

    /// Push one freshly-demodulated bin (raw argmax in [0, 2^sf)).
    /// Returns a populated FrameResult when the frame is complete (success
    /// or header-parse failure). Returns std::nullopt while still
    /// accumulating symbols for the next interleaver block.
    [[nodiscard]] std::optional<FrameResult> push_symbol(uint16_t bin) noexcept {
        if (_phase == Phase::Done || _phase == Phase::Failed) {
            return std::nullopt;
        }

        _sym_buf.push_back(bin);
        ++_total_symbols_rx;

        if (_phase == Phase::CollectingHeader) {
            if (_sym_buf.size() == 8) {
                return processHeaderBlock();
            }
            return std::nullopt;
        }

        // CollectingPayload — tap the raw bin for the LDRO mod-2 signature.
        //
        // Under LDRO (sf_app = sf-2) the interleaver packs one symbol as
        // MSB-first bits: [data×sf_app | parity | zero]. After bool2int
        // that lands at integer positions [bit(sf-1)..bit(2) | bit(1) |
        // bit(0)], i.e. the LSB of the interleaver output is ALWAYS zero.
        // TX then applies gray_demap = (gray_decode(sym) + 1) mod N; RX
        // adjustBin inverts both, recovering the interleaver output —
        // so adjustBin(bin) & 1 == 0 is the LDRO signature.
        //
        // Under no-LDRO the symbol is data-only over all sf bits
        // (effectively uniform post-whitening/Hamming/Gray) → bit 0 is
        // 50/50. Header symbols always use sf_app = sf-2 (regardless of
        // user LDRO setting) so we exclude them — only payload symbols
        // discriminate TX LDRO state.
        //
        // Note: mod-4 would also flag the 0-pad bit but the parity bit at
        // position 1 is essentially random (XOR over data), so mod-4
        // collapses back to the mod-2 test in expectation.
        {
            const uint16_t s = adjustBin(bin);
            if ((s & 0x1u) == 0u) {
                ++_payload_lsb0_hits;
            }
            ++_payload_syms_seen;
        }

        const uint8_t cw_len = static_cast<uint8_t>(4 + _cr);
        if (_sym_buf.size() == cw_len) {
            return processPayloadBlock();
        }
        return std::nullopt;
    }

    /// Total symbols received across all calls. Useful for OUTPUT-state
    /// stall timeouts in the driving block.
    [[nodiscard]] uint32_t total_symbols_rx() const noexcept { return _total_symbols_rx; }

    /// Symbols still expected before the frame is complete; only valid after
    /// the header has been parsed (returns 0 in CollectingHeader).
    [[nodiscard]] uint32_t symbols_remaining() const noexcept {
        if (_frame_symb_numb == 0 || _total_symbols_rx >= _frame_symb_numb) {
            return 0;
        }
        return _frame_symb_numb - _total_symbols_rx;
    }

    [[nodiscard]] bool done() const noexcept { return _phase == Phase::Done || _phase == Phase::Failed; }

private:
    enum class Phase { CollectingHeader, CollectingPayload, Done, Failed };

    Config                _cfg;
    uint32_t              _N     = 0;
    bool                  _ldro  = false;
    Phase                 _phase = Phase::CollectingHeader;
    std::vector<uint16_t> _sym_buf; ///< current interleaver block
    std::vector<uint8_t>  _nibbles; ///< all nibbles header + payload
    uint32_t              _total_symbols_rx  = 0;
    uint32_t              _frame_symb_numb   = 0;
    uint8_t               _cr                = 0;
    uint16_t              _pay_len           = 0;
    bool                  _has_crc           = false;
    uint32_t              _payload_syms_seen = 0; ///< tested for LDRO LSB-zero signature
    uint32_t              _payload_lsb0_hits = 0; ///< adjustBin(bin) & 1 == 0

    /// Undo the TX-side `(gray_decode(sym) + 1) mod N` mapping. The TX
    /// modulator emits a chirp at bin index `(gray_decode(sym) + 1) mod N`;
    /// the RX argmax recovers that same bin. Subtracting 1 mod N gives
    /// `gray_decode(sym)`, and applying `binary_to_gray` (= x ^ (x >> 1))
    /// inverts gray_decode to recover the original interleaver output.
    [[nodiscard]] uint16_t adjustBin(uint16_t bin) const noexcept {
        const uint16_t adj = (bin == 0) ? static_cast<uint16_t>(_N - 1) : static_cast<uint16_t>(bin - 1);
        return static_cast<uint16_t>(adj ^ (adj >> 1));
    }

    [[nodiscard]] std::vector<uint8_t> processInterleaverBlock(std::span<const uint16_t> bins, uint8_t sf_app, uint8_t cw_len, uint8_t cr_app) const {
        std::vector<uint16_t> sym_decoded;
        sym_decoded.reserve(bins.size());
        const uint8_t shift = static_cast<uint8_t>(_cfg.sf - sf_app);
        for (auto b : bins) {
            uint16_t s = adjustBin(b);
            if (shift > 0) {
                s = static_cast<uint16_t>(s >> shift);
            }
            sym_decoded.push_back(s);
        }
        auto                 codewords = gr::lora::deinterleave_block(sym_decoded, _cfg.sf, cw_len, sf_app);
        std::vector<uint8_t> nibs;
        nibs.reserve(codewords.size());
        for (auto cw : codewords) {
            nibs.push_back(gr::lora::hamming_decode_hard(cw, cr_app));
        }
        return nibs;
    }

    [[nodiscard]] std::optional<FrameResult> processHeaderBlock() {
        const uint8_t sf_app = static_cast<uint8_t>(_cfg.sf - 2);
        auto          nibs   = processInterleaverBlock(std::span<const uint16_t>(_sym_buf.data(), 8), sf_app, /*cw_len=*/8, /*cr_app=*/4);
        _sym_buf.clear();

        if (nibs.size() < 5) {
            return failFrame();
        }

        const auto info = gr::lora::parse_explicit_header(nibs[0], nibs[1], nibs[2], nibs[3], nibs[4]);
        if (!info.checksum_valid || info.payload_len == 0) {
            return failFrame();
        }
        // LoRa: cr in [1, 4] (4/5, 4/6, 4/7, 4/8). Values 0 or 5-7 are
        // invalid and indicate a corrupted header that passed the header
        // Hamming by chance. Reject here so downstream does not build a
        // bogus cw_len = 4 + cr interleaver block.
        if (info.cr < 1 || info.cr > 4) {
            return failFrame();
        }

        _cr      = info.cr;
        _pay_len = info.payload_len;
        _has_crc = info.has_crc;

        // Number of payload symbols (post-header) needed to deliver
        // the requested nibble count.
        const double numerator = static_cast<double>(2 * _pay_len - _cfg.sf + 2 + 5 + (_has_crc ? 4 : 0));
        const double denom     = static_cast<double>(_cfg.sf - 2 * static_cast<int>(_ldro));
        _frame_symb_numb       = 8u + static_cast<uint32_t>(std::ceil(numerator / denom)) * (4u + _cr);

        _nibbles.insert(_nibbles.end(), nibs.begin(), nibs.end());
        _phase = Phase::CollectingPayload;

        // Edge case: very short payload + high SF — header block alone can
        // already deliver enough nibbles to complete the frame.
        if (_total_symbols_rx >= _frame_symb_numb) {
            return finishFrame();
        }
        return std::nullopt;
    }

    [[nodiscard]] std::optional<FrameResult> processPayloadBlock() {
        const uint8_t sf_app = static_cast<uint8_t>(_ldro ? (_cfg.sf - 2) : _cfg.sf);
        const uint8_t cw_len = static_cast<uint8_t>(4 + _cr);

        auto nibs = processInterleaverBlock(std::span<const uint16_t>(_sym_buf.data(), cw_len), sf_app, cw_len, _cr);
        _sym_buf.clear();
        _nibbles.insert(_nibbles.end(), nibs.begin(), nibs.end());

        if (_total_symbols_rx >= _frame_symb_numb) {
            return finishFrame();
        }
        return std::nullopt;
    }

    [[nodiscard]] FrameResult finishFrame() noexcept {
        _phase = Phase::Done;

        FrameResult r{};
        r.sf                = _cfg.sf;
        r.cr                = _cr;
        r.payload_len       = _pay_len;
        r.has_crc           = _has_crc;
        r.header_valid      = true;
        r.ldro_cfg          = _ldro;
        r.payload_syms_seen = _payload_syms_seen;
        r.payload_lsb0_hits = _payload_lsb0_hits;

        constexpr std::size_t kHeaderNibbles = 5;
        if (_nibbles.size() <= kHeaderNibbles) {
            return r; // header parsed, but no payload nibbles produced
        }

        const uint32_t    total_data_nibs = static_cast<uint32_t>(2u * _pay_len + (_has_crc ? 4u : 0u));
        const std::size_t avail_nibs      = _nibbles.size() - kHeaderNibbles;
        std::size_t       use_nibs        = std::min(static_cast<std::size_t>(total_data_nibs), avail_nibs);
        use_nibs &= ~std::size_t{1}; // round down to even (whole bytes)

        auto bytes = gr::lora::dewhiten(std::span<const uint8_t>(&_nibbles[kHeaderNibbles], use_nibs), _pay_len);

        r.crc_valid = true;
        if (_has_crc && _pay_len >= 2 && bytes.size() >= _pay_len + 2u) {
            r.crc_valid = gr::lora::lora_verify_crc(std::span<const uint8_t>(bytes.data(), _pay_len), bytes[_pay_len], bytes[_pay_len + 1]);
        }

        if (bytes.size() >= _pay_len) {
            r.payload.assign(bytes.begin(), bytes.begin() + static_cast<std::ptrdiff_t>(_pay_len));
        } else {
            r.payload.assign(bytes.begin(), bytes.end());
        }
        return r;
    }

    [[nodiscard]] FrameResult failFrame() noexcept {
        _phase = Phase::Failed;
        FrameResult r{};
        r.sf                = _cfg.sf;
        r.ldro_cfg          = _ldro;
        r.payload_syms_seen = _payload_syms_seen;
        r.payload_lsb0_hits = _payload_lsb0_hits;
        return r;
    }
};

} // namespace gr::lora::phy

#endif // GNURADIO_LORA_PHY_DECODE_CHAIN_HPP
