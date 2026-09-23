# rca-agent task entry point
#
# NOTE: This project intentionally provides NO `clean` / `distclean` target.
#       Deleting files is the job of the policy enforcement point and must
#       go through the quarantine area (so it stays reversible).
#
# NOTE: This file is deliberately ASCII-only. Non-ASCII in a parsed script
#       is a known hazard on Windows (see scripts/dev.ps1 header).

.PHONY: help sync doctor check test lint fmt dev world-up world-stop world-logs smoke eval demo share

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

dev: world-up

# 启动被诊断系统（一条命令起全栈）
world-up:
	docker compose up -d --build
	@echo ""
	@echo "world is up. business entry point: http://127.0.0.1:8080"
	@echo "verify the chain with: make smoke"

# 只停止，不删除容器。
# 刻意不提供 'down' —— 它会删除容器与网络，与项目的"不删除"原则冲突。
world-stop:
	docker compose stop
	@echo "world stopped. containers kept; resume with: docker compose start"

world-logs:
	docker compose logs -f --no-log-prefix

# 冒烟测试：验证链路 / trace 传递 / 指标端点
smoke:
	uv run python scripts/smoke_world.py

eval:
	@echo "not implemented yet - arrives D10 (eval harness)"
	@exit 1

demo:
	@echo "not implemented yet - arrives D14"
	@exit 1

share:
	@echo "not implemented yet - see docs/03-event-protocol.md section 8"
	@exit 1
