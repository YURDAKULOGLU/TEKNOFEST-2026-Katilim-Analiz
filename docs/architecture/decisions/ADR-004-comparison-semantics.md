# ADR-004: Comparison Semantics

- Status: accepted
- Date: 2026-07-18

## Decision

Implement comparison as a pure, versioned domain function over decimal canonical values. Products are comparable only when product family, rate kind, rate period/basis, currency context, validity, and relevant customer eligibility are compatible.

Return dimension-specific results—rate, term, fee, reward, and eligibility—plus reasons and evidence. Do not emit an opaque global “best bank” score. When trade-offs exist, expose them or return a Pareto set.

## Consequences

A monthly financing rate, annual cost rate, participation-account distribution rate, and historical return can never enter one numerical ranking merely because each contains a percentage.
