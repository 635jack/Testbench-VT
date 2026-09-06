#!/usr/bin/env python3
"""Point d'entrée : ``python3 -m vtctl``."""
import sys

from .cli import main

sys.exit(main())
