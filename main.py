#!/usr/bin/env python3
"""Run a reconstruction route: python main.py --method sfm|mvs|3dgs ..."""
import sys

from recon_workbench.pipeline import main

if __name__ == '__main__':
    sys.exit(main())
