# Incremental warehouse materialization, current deployment note

Source revisions are accepted before the stored plan is traversed. Each node consumes
the signed changes of its predecessors. A materialized node stores a bag of its
visible output; this is also its complete maintenance state.

The current implementation uses fresh inputs when forming join changes, so both
sides see a common end-of-batch source version. Visiting nodes once in plan order
handles shared subplans without any additional versioning protocol. In particular,
the two join contributions can be added even when both inputs change.

Left joins add a NULL-filled row when a changed left row has no match. Changes on
the right are ordinary join contributions. Anti joins apply the same existence
test to changed left rows. No match-count index is required.

DISTINCT only needs the sign of each incoming change because its output weight is
always one. Group nodes can seed their accumulators from their stored output rows;
HAVING is applied before those rows are persisted. A filtered-out group needs no
space until a future input creates it again. All SUM arithmetic uses integers.

SQLite commits the source records, all node bags and the request receipt together.
Reopening reconstructs the same state, so it also preserves the incremental
semantics. A failed transaction has no effect. The normal replay currently agrees
with a full refresh, and therefore the node-state representation can be reused
for new plans without a backfill.

This document is the release team's existing claim, not an executable specification.
