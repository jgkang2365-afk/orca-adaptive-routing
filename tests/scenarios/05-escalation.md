# Scenario 05 - Escalation

## Initial Task

Investigate a seemingly simple bug where one configuration value
is not displayed correctly.

Initial routing should use the lowest sufficient capability.

## New Finding

During investigation, the worker discovers that the displayed value depends on
authorization rules, database state, and asynchronous synchronization across
multiple services.
