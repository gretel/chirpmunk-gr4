#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Diversity-combining aggregator for the lora-core daemon.

See :mod:`lora.aggregator.diversity` for the implementation.
"""

from __future__ import annotations

from lora.aggregator.diversity import (
    Candidate,
    DiversityAggregator,
    PendingGroup,
)

__all__ = ["Candidate", "DiversityAggregator", "PendingGroup"]
