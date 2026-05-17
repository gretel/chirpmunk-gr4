"""Allow ``python -m lora.bridges.serial`` entry point."""

from __future__ import annotations

import sys

from lora.bridges.serial import main

sys.exit(main())
