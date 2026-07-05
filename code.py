#!/usr/bin/env python3
"""Compatibility entrypoint for the generic agent runtime.

The implementation moved into the ``agent_runtime`` package. Prefer:

    python -m agent_runtime --provider openai
    python -m agent_runtime --provider anthropic
"""

from agent_runtime.cli import main


if __name__ == "__main__":
    main()
