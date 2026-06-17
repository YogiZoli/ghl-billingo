"""Thin entrypoint so `python main.py <command>` works alongside `python -m app.cli`."""
from app.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
