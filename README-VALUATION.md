# Valuation Engine — First Pass

This package establishes the architecture for the Fantasy GM valuation system.

## Core principle

Every draftable player gets an intrinsic valuation from deterministic football/fantasy data.

Contextual AI does **not** replace that score. It proposes bounded, auditable adjustments that are applied on top of the quantitative model.

## Three layers

1. **Intrinsic player value**
   - league-scored projections
   - value over replacement
   - usage
   - efficiency
   - scarcity
   - risk

2. **Context/event layer**
   - injury
   - suspension
   - contract/holdout
   - role change
   - coaching
   - offensive line
   - off-field distraction / availability risk
   - breaking news

3. **Relationship propagation**
   - RB1 injury raises RB2/RB3 opportunity
   - WR1 injury redistributes target opportunity
   - QB injury lowers pass-catcher environment
   - OL injury modestly hurts QB/RB environment

## AI contract

The LLM returns structured ContextAdjustment objects with:
- category
- bounded delta
- confidence
- reason
- evidence
- optional relationship effects

The deterministic valuation engine decides how those adjustments affect the board.

This keeps the system explainable and prevents a single speculative article from overwhelming the quantitative model.
