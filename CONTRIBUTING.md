# Contributing

1. `make install && make test && make eval` must be green before a PR.
2. A change to a prompt, route (`config/capabilities.yaml`), tool, or policy must keep the evaluation gate green;
   if you change what "correct" means, update the golden case in the same PR and say why in the description.
3. Lowering a threshold in `config/thresholds.yaml` or editing `policies/` needs CODEOWNERS review.
4. New tool = manifest entry in `config/tools.yaml` + module in `toolgateway/tools/` + IAM role in
   `infra/terraform/modules/iam_tool_roles` + at least one policy golden case.
