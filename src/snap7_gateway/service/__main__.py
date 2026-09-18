"""Allow ``python -m snap7_gateway.service``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
