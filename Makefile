# Developer commands. Run `make help` for the list. Needs: uv, Docker (for Postgres/Qdrant).
SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE_DEV := docker compose -f docker-compose.yml -f docker-compose.dev.yml
TEST_DB     := postgresql://maw:maw@127.0.0.1:55432/maw
TEST_QDRANT := http://127.0.0.1:6333
RUN         := .run
STORE       ?= docs

.PHONY: help setup db qdrant mocks stop-mocks services serve dev ingest test test-all live-test lint fmt \
        validate smoke up down logs stop clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-11s\033[0m %s\n", $$1, $$2}'

setup: ## Install dependencies and create .env from .env.example (first time)
	uv sync
	@if [ ! -f .env ]; then cp .env.example .env && echo "created .env - add your API keys to it"; else echo ".env already exists"; fi

db: ## Start Postgres + pgvector in Docker (127.0.0.1:55432) and wait for it
	$(COMPOSE_DEV) up -d postgres
	@for i in $$(seq 1 30); do $(COMPOSE_DEV) exec -T postgres pg_isready -U maw -d maw >/dev/null 2>&1 && echo "postgres ready" && exit 0; sleep 1; done; echo "postgres did not become ready" >&2; exit 1

qdrant: ## Start Qdrant in Docker (127.0.0.1:6333), for the external vector store
	docker compose --profile qdrant up -d qdrant

mocks: ## Start the mock MCP server (:8001) and mock A2A agent (:9001) in the background
	@mkdir -p $(RUN)
	@if [ -f $(RUN)/mcp.pid ] && kill -0 $$(cat $(RUN)/mcp.pid) 2>/dev/null; then echo "mock MCP already running"; else \
		nohup uv run python mocks/mcp_kb_server.py --host 127.0.0.1 --port 8001 > $(RUN)/mcp.log 2>&1 & echo $$! > $(RUN)/mcp.pid; echo "mock MCP started (logs: $(RUN)/mcp.log)"; fi
	@if [ -f $(RUN)/a2a.pid ] && kill -0 $$(cat $(RUN)/a2a.pid) 2>/dev/null; then echo "mock A2A already running"; else \
		nohup uv run python mocks/a2a_billing_agent.py --host 127.0.0.1 --port 9001 > $(RUN)/a2a.log 2>&1 & echo $$! > $(RUN)/a2a.pid; echo "mock A2A started (logs: $(RUN)/a2a.log)"; fi

stop-mocks: ## Stop the background mock services
	@for p in mcp a2a; do if [ -f $(RUN)/$$p.pid ]; then kill $$(cat $(RUN)/$$p.pid) 2>/dev/null || true; rm -f $(RUN)/$$p.pid; echo "stopped mock $$p"; fi; done

services: db mocks ## Start Postgres and both mocks (everything `make serve` needs)

serve: ## Run MAW on the host with auto-reload (reads .env); http://localhost:8000/docs
	uv run maw serve --reload

dev: services serve ## Start services, then MAW

ingest: ## Ingest examples/docs into a vector store: make ingest [STORE=docs|docs_gemini|docs_openai]
	uv run maw ingest $(STORE) examples/docs/*.md

test: ## Run the test suite (database, Qdrant and live tests are skipped)
	uv run pytest -q

test-all: db qdrant ## Run every test against Postgres + Qdrant (live tests skip without keys)
	MAW_TEST_DATABASE_URL=$(TEST_DB) MAW_TEST_QDRANT_URL=$(TEST_QDRANT) uv run pytest -q -rs

live-test: ## Run the live tests against real OpenAI/Gemini (keys from .env; costs a few cents)
	uv run pytest -m live -v -rs

lint: ## Ruff lint + format check + validate the catalog
	uv run ruff check src tests mocks scripts
	uv run ruff format --check src tests mocks scripts
	uv run maw validate workflows/*.json examples/*.json

fmt: ## Auto-format and fix lint issues
	uv run ruff format src tests mocks scripts
	uv run ruff check --fix src tests mocks scripts

validate: ## Validate the catalog and example workflows
	uv run maw validate workflows/*.json examples/*.json

smoke: ## Smoke-test a running server (default http://localhost:8000; BASE=... API_KEY=...)
	./scripts/smoke.sh

up: ## Run the whole stack in Docker (MAW + Postgres + mocks)
	docker compose up --build -d

down: ## Stop the Docker stack (data volumes are kept)
	docker compose --profile qdrant --profile tracing down

logs: ## Follow MAW's logs in the Docker stack
	docker compose logs -f maw

stop: stop-mocks ## Stop mocks and Docker services
	docker compose --profile qdrant --profile tracing down

clean: stop ## Stop everything and delete local data (Postgres/Qdrant volumes, caches)
	docker compose --profile qdrant --profile tracing down -v
	rm -rf $(RUN) .pytest_cache .ruff_cache
