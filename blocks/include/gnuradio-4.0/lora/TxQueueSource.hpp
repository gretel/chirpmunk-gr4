// SPDX-License-Identifier: ISC
#ifndef LORA_TX_QUEUE_SOURCE_HPP
#define LORA_TX_QUEUE_SOURCE_HPP

/// TxQueueSource — GR4 source block that drains a thread-safe IQ burst queue.
///
/// External code pushes bursts via push(); the block outputs one burst at a
/// time. Between bursts it publishes 0 samples so the singleThreadedBlocking
/// scheduler can sleep. Call notifyProgress() after every push() to wake it.
///
/// Two output ports (out0, out1) are produced in lockstep (same sample count
/// per call), so SoapyDevSink<cf32,2> stays in sync. out0 carries the IQ
/// signal; out1 carries matching zeros for the second TX channel (if present).

#include <algorithm>
#include <complex>
#include <mutex>
#include <queue>
#include <vector>

#include <gnuradio-4.0/Block.hpp>

namespace gr::lora {

struct TxQueueSource : gr::Block<TxQueueSource> {
    using Description = Doc<"Thread-safe burst IQ source for persistent TX graph">;
    using cf32        = std::complex<float>;

    gr::PortOut<cf32> out0; ///< IQ signal channel
    gr::PortOut<cf32> out1; ///< zeros channel (second TX chain dummy)

    GR_MAKE_REFLECTABLE(TxQueueSource, out0, out1);

    std::mutex                    _mutex{};
    std::queue<std::vector<cf32>> _queue{};
    std::vector<cf32>             _current{}; ///< burst currently being drained
    std::size_t                   _offset{0};

    /// Push a burst from the UDP loop thread. Call notifyProgress() after.
    void push(std::vector<cf32> burst) {
        std::lock_guard<std::mutex> lock(_mutex);
        _queue.push(std::move(burst));
    }

    /// Wake the singleThreadedBlocking scheduler after push().
    void notifyProgress() {
        this->progress->incrementAndGet();
        this->progress->notify_all();
    }

    gr::work::Status processBulk(gr::OutputSpanLike auto& iqOut, gr::OutputSpanLike auto& zeroOut) noexcept {
        // Load the next burst if the current one is exhausted.
        if (_offset >= _current.size()) {
            std::lock_guard<std::mutex> lock(_mutex);
            if (_queue.empty()) {
                iqOut.publish(0UZ);
                zeroOut.publish(0UZ);
                if (this->state() == gr::lifecycle::State::REQUESTED_STOP) {
                    return gr::work::Status::DONE;
                }
                return gr::work::Status::OK;
            }
            _current = std::move(_queue.front());
            _queue.pop();
            _offset = 0;
        }

        const std::size_t avail    = _current.size() - _offset;
        const std::size_t nSamples = std::min({avail, iqOut.size(), zeroOut.size()});
        if (nSamples == 0) {
            iqOut.publish(0UZ);
            zeroOut.publish(0UZ);
            return gr::work::Status::OK;
        }

        auto iqSpan   = std::span<cf32>(iqOut).subspan(0, nSamples);
        auto zeroSpan = std::span<cf32>(zeroOut).subspan(0, nSamples);
        std::copy(_current.begin() + static_cast<std::ptrdiff_t>(_offset), _current.begin() + static_cast<std::ptrdiff_t>(_offset + nSamples), iqSpan.begin());
        std::fill(zeroSpan.begin(), zeroSpan.end(), cf32{});

        iqOut.publish(nSamples);
        zeroOut.publish(nSamples);
        _offset += nSamples;
        return gr::work::Status::OK;
    }
};

} // namespace gr::lora

#endif // LORA_TX_QUEUE_SOURCE_HPP
