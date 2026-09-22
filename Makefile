# rca-agent task entry point
#
# NOTE: This project intentionally provides NO `clean` / `distclean` target.
#       Deleting files is the job of the policy enforcement point and must
#       go through the quarantine area (so it stays reversible).
#
# NOTE: This file is deliberately ASCII-only. Non-ASCII in a parsed script
#       is a known hazard on Windows (see scripts/dev.ps1 header).

.PHONY: help sync doctor check test lint fmt dev eval demo share

help:
	@echo "rca-agent targets:"
	@echo "  make sync      install/sync dependencies (uv sync)"
	@echo "  make doctor    environment self-check (python / imports / docker)"
	@echo "  make check     lint + tests"
	@echo "  make lint      lint only"
	@echo "  make fmt       format only"
	@echo "  make dev       start the diagnosed system (docker compose)"
	@echo "  make eval      run the eval set (excludes holdout)"
	@echo "  make demo      3-minute demo flow"
	@echo "  make share     export a redacted replay bundle"
	@echo ""
	@echo "  There is no 'clean'. Deletion always goes through quarantine."

sync:
	uv sync

doctor:
	@powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

test:
	uv run pytest

check: lint test

# ---- targets below arrive with later milestones (D1..D14) ----

dev:
	@echo "not implemented yet - arrives D1 (docker compose)"
	@exit 1

eval:
	@echo "not implemented yet - arrives D10 (eval harness)"
	@exit 1

demo:
	@echo "not implemented yet - arrives D14"
	@exit 1

share:
	@echo "not implemented yet - see docs/03-event-protocol.md section 8"
	@exit 1
