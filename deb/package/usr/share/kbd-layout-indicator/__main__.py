#!/usr/bin/env python3
"""Entry point for kbd-layout-indicator."""

import argparse
import logging
import sys


def main():
    parser = argparse.ArgumentParser(
        description="Keyboard layout indicator for LXQt/Wayland (labwc)"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    # Late import so --help works without PyQt5 installed
    from indicator import KeyboardLayoutIndicator

    indicator = KeyboardLayoutIndicator()
    sys.exit(indicator.run())


if __name__ == "__main__":
    main()
