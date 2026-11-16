"""AgentGate: a governed agent platform on AWS.

Every agent reaches models through the inference gateway and tools through the
tool gateway. Policy is deterministic code (Cedar), never a prompt. Every change
to a prompt, route, tool or policy passes the evaluation gate in CI.
"""

__version__ = "0.1.0"
