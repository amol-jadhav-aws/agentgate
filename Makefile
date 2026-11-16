.PHONY: install up down test eval demo demo-cost ask lint fmt chaos destroy plan apply

install:          ## dev install
	pip install -e ".[dev]"

up:               ## local platform: gateways + redis + jaeger (http://localhost:16686)
	docker compose up --build -d

down:
	docker compose down -v

test:             ## unit + integration tests (offline)
	pytest -q

eval:             ## evaluation gate, same command CI runs
	agentgate-eval --report .evals/latest

demo:             ## CPU-spike incident, pauses for approval (add APPROVE=you to auto-approve)
	agentgate --inproc demo cpu $(if $(APPROVE),--auto-approve $(APPROVE),)

demo-cost:
	agentgate --inproc demo cost

ask:              ## make ask Q="when should I scale out?"
	agentgate --inproc ask "$(Q)"

chaos:            ## inject the CPU spike into the running local platform
	python scripts/chaos_cpu_spike.py

lint:
	ruff check src tests && ruff format --check src tests

fmt:
	ruff format src tests && ruff check --fix src tests

plan:             ## terraform plan for dev
	cd infra/terraform/envs/dev && terraform init -input=false && terraform plan -input=false

apply:
	cd infra/terraform/envs/dev && terraform apply -input=false -auto-approve

destroy:          ## tear everything down after a session (the USD 40 rule)
	cd infra/terraform/envs/dev && terraform destroy -input=false -auto-approve
