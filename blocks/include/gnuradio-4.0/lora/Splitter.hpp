// SPDX-License-Identifier: ISC
#ifndef GNURADIO_LORA_SPLITTER_HPP
#define GNURADIO_LORA_SPLITTER_HPP

#include <algorithm>
#include <complex>
#include <cstdint>
#include <vector>

#include <gnuradio-4.0/Block.hpp>
#include <gnuradio-4.0/BlockRegistry.hpp>

namespace gr::lora {

// 1-to-N signal splitter: copies input samples and tags to all outputs.
//
// Used to fan out a single radio channel to multiple decode chains (e.g.
// different SF or sync word configurations). When n_outputs=1, the block
// is a trivial passthrough with no copy overhead.
GR_REGISTER_BLOCK("gr::lora::Splitter", gr::lora::Splitter)
struct Splitter : gr::Block<Splitter, gr::NoTagPropagation> {
    using Description = Doc<"1-to-N signal splitter — copies samples and tags to all outputs">;

    gr::PortIn<std::complex<float>>               in;
    std::vector<gr::PortOut<std::complex<float>>> out{};

    Annotated<gr::Size_t, "n_outputs", gr::Visible, gr::Limits<1U, 8U>> n_outputs = 2U;
    bool                                                                debug     = false;

    GR_MAKE_REFLECTABLE(Splitter, in, out, n_outputs, debug);

    void start() { out.resize(n_outputs); }

    void settingsChanged(const gr::property_map& /*oldSettings*/, const gr::property_map& newSettings) {
        if (newSettings.contains("n_outputs")) {
            out.resize(n_outputs);
        }
    }

    template<gr::InputSpanLike TInSpan, gr::OutputSpanLike TOutSpan>
    gr::work::Status processBulk(TInSpan& inSpan, std::span<TOutSpan>& outs) {
        const auto inData   = std::span(inSpan);
        const auto nSamples = inData.size();

        // Forward tags to all outputs.  Upstream PR #625 multi-tag API:
        // processBulk reads per-tag via inSpan.tags() rather than the
        // old mergedInputTag().  Preserve per-tag relative index when
        // republishing so multiple tags per chunk land at their correct
        // sample positions downstream.
        for (const auto& [relIndex, tagMapRef] : inSpan.tags()) {
            for (std::size_t i = 0; i < outs.size(); i++) {
                outs[i].publishTag(tagMapRef.get(), static_cast<std::size_t>(relIndex));
            }
        }

        // Determine how many samples ALL outputs can accept — consume only
        // that many to avoid silent sample loss on backpressured paths.
        // Upstream PR #439 made OutputSpan move-only with one reserve/publish
        // per lifetime; repeated std::span(outs[i]) calls tripped the
        // CircularBuffer::SingleWriter double-reserve assert.  Access each
        // OutputSpan's underlying span exactly once per processBulk call.
        auto nCopy = nSamples;
        for (std::size_t i = 0; i < outs.size(); i++) {
            nCopy = std::min(nCopy, outs[i].size());
        }

        if (nCopy == 0) {
            std::ignore = inSpan.consume(0UZ);
            for (std::size_t i = 0; i < outs.size(); i++) {
                outs[i].publish(0UZ);
            }
            return gr::work::Status::INSUFFICIENT_OUTPUT_ITEMS;
        }

        // Copy samples to all outputs.  Use OutputSpan's iterator API
        // directly rather than std::span(outs[i]) to avoid a second
        // writer reservation.
        for (std::size_t i = 0; i < outs.size(); i++) {
            std::copy_n(inData.begin(), nCopy, outs[i].begin());
            outs[i].publish(nCopy);
        }

        std::ignore = inSpan.consume(nCopy);
        return gr::work::Status::OK;
    }
};

} // namespace gr::lora

#endif // GNURADIO_LORA_SPLITTER_HPP
