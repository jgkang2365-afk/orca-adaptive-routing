# Expected 05

Initial routing:
- gpt-5.6-luna / low or gpt-5.6-terra / medium depending on initial inspection scope

After new findings:
- Worker reports escalation.
- Worker must not autonomously spawn a stronger worker.
- Coordinator reclassifies the task.
- Authorization/data-integrity risk routes to gpt-5.6-sol / medium by default;
  high requires a concrete destructive, rollback, data-loss, attack-path,
  high-ambiguity, or confidence-failure reason.
- WRITE authority is decided independently from model strength.
