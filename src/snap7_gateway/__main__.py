"""Allow ``python -m snap7_gateway``."""

from .service.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
