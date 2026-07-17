from __future__ import annotations

import argparse
from pathlib import Path

from agent_runtime.bootstrap import build_runtime
from agent_runtime.settings import load_settings


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run Agent Runtime')
    parser.add_argument('--provider', choices=('openai', 'anthropic'))
    parser.add_argument('--model')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--workdir', type=Path, default=Path.cwd())
    args = parser.parse_args(argv)

    settings = load_settings(args.config)
    if args.provider or args.model:
        settings = settings.with_model(args.provider, args.model)
    runtime = build_runtime(settings=settings, workdir=args.workdir)

    print('agent-runtime: enter a question, empty line to quit')
    while True:
        try:
            query = input('agent >> ')
        except (EOFError, KeyboardInterrupt):
            break
        if not query.strip():
            break
        print(runtime.run_turn(query))
