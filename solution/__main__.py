"""
Executable Package Entrypoint.

Allows running the pipeline directly via:
    python -m solution ingest <drop_path>
    python -m solution report --out <output_dir>
"""

from __future__ import annotations

from solution.cli import main

if __name__ == "__main__":
    main()
