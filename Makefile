# rca-agent task entry point
#
# NOTE: This project intentionally provides NO `clean` / `distclean` target.
#       Deleting files is the job of the policy enforcement point and must
#       go through the quarantine area (so it stays reversible).
#
# NOTE: This file is deliberately ASCII-only. Non-ASCII in a parsed script
#       is a known hazard on Windows (see scripts/dev.ps1 header).

.PHONY: help sync doctor check test lint fmt dev world-up world-stop world-logs smoke eval demo share ci ci-offline mutants seal

help:
	@echo "rca-agent targets:"
	@echo "  make sync      install/sync dependencies (uv sync)"
	@echo "  make doctor    environment self-check (python / imports / docker)"
	@echo "  make check     lint + tests"
	@echo "  make lint      lint only"
	@echo "  make fmt       format only"
	@echo "  make world-up  start the diagnosed system (docker compose)"
	@echo "  make smoke     verify the world chain / trace / metrics"
	@echo "  make world-stop  stop the world (containers kept)"
	@echo "  make world-logs  follow world logs"
	@echo "  make eval      run the eval set: make eval ARGS=\"--agent multi --rounds 3\""
	@echo "  make demo      regenerate the public page from the archives"
	@echo "  make ci        offline gate (same commands as .github/workflows/ci.yml)"
	@echo "  make mutants   every mutation definition still applies (fast)"
	@echo "  make seal      every 'sealed' claim has a mutation group (fast)"
	@echo "  make share     not implemented (see docs/03-event-protocol.md section 8)"
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

# ---- offline gate: exactly what CI runs, minus the Docker job ----

ci:
	@powershell -NoProfile -ExecutionPolicy Bypass -File scripts/ci.ps1

mutants:
	uv run python scripts/mutate_check.py --verify-only

seal:
	uv run python scripts/seal_report.py --skip-mutations

# ---- targets below arrive with later milestones (D1..D14) ----

dev: world-up

# Start the diagnosed system (one command brings up the whole stack)
world-up:
	docker compose up -d --build
	@echo ""
	@echo "world is up. business entry point: http://127.0.0.1:8080"
	@echo "verify the chain with: make smoke"

# Stop only; containers are kept.
# There is deliberately no 'down' target: it removes containers and the
# network, which conflicts with this project's "never delete" rule.
world-stop:
	docker compose stop
	@echo "world stopped. containers kept; resume with: docker compose start"

world-logs:
	docker compose logs -f --no-log-prefix

# Smoke test: verifies the chain / trace propagation / metrics endpoints
smoke:
	uv run python scripts/smoke_world.py

# ---- eval / demo ----
#
# `eval` COSTS MONEY: it needs DEEPSEEK_API_KEY and a running world.
# Baseline over all 7 scenarios is about CNY 0.36; multi is about CNY 0.073
# per attempt. Pass the flags explicitly so the sample is never accidental:
#
#   make eval ARGS="--agent baseline --rounds 3"
#   make eval ARGS="--agent multi --faults F4 --rounds 3 --max-steps 40"
#
eval:
	@if [ -z "$(ARGS)" ]; then \
	  echo "usage: make eval ARGS=\"--agent baseline|multi [--faults F1,F8] [--rounds 3]\""; \
	  echo "NOTE: this spends API credit and needs a running world."; \
	  exit 2; \
	fi
	uv run python eval/runner.py $(ARGS)

# Regenerate the public page FROM THE ARCHIVES. Free, offline, and it must
# reproduce the committed HTML byte for byte (a test guards that).
demo:
	uv run python scripts/make_demo.py

share:
	@echo "not implemented yet - see docs/03-event-protocol.md section 8"
	@exit 1
