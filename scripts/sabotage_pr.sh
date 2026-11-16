#!/usr/bin/env bash
# Demo for the eval gate: make a tempting but wrong change and watch CI go red.
# Routes propose_action to the cheapest model and removes the "never destructive" line
# from the Ops agent's prompt. The golden set catches both.
set -euo pipefail
git checkout -b sabotage/cheap-propose
sed -i 's/propose_action:   { model: sonnet,     fallback: haiku, /propose_action:   { model: nova-micro, fallback: nova-micro,/' config/capabilities.yaml
sed -i 's/ Never propose destructive actions\.//' src/agentgate/agents/ops_agent.py
git commit -am "perf: route propose_action to nova-micro to cut cost"
echo "Now: git push -u origin sabotage/cheap-propose && gh pr create --fill  -> the eval gate must block it."
