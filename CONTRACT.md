# Warehouse materialized view review package

This is a compact, running warehouse, not an empty optimizer exercise. The release
team needs a code review and a corrected design for its existing incremental
maintenance path. Production code and baseline assertions stay unchanged. The
schema-checked deployment plan is immutable after a database is initialized.
The embedding service serializes public Warehouse calls. External caller
transactions are outside this package; Warehouse starts its own transaction.
The proposed online state build must coordinate with the ongoing serialized calls.

## Entry points and source data
Warehouse(db, plan=None) in store.py opens a SQLite path or an existing SQLite
connection. An empty database requires plan. apply(batch, fail_at=None) returns
{"generation": integer, "delta": bag}; read() returns {"generation": integer,
"rows": bag}. Bag serialization is [[row, integer_weight], ...], sorted by
repr(tuple(row)); a visible bag has strictly positive weights. A delta may have
negative weights and omits zeros. Rows are arrays of integers, strings or null.
Weights are multiplicities, not event identities; they must not be expanded.

A batch is {"id": nonempty_string, "changes": [[source, id, rev, row_or_null], ...]}.
A source record identity is (source,id). Each batch mentions an identity at most
once. rev is a positive signed-64-bit integer. A higher revision entirely replaces the record;
null is a tombstone, and a later revision may revive it. Lower revisions are
ignored. An equal revision with equal content is ignored; unequal content raises
REVISION_CONFLICT. A same-ID batch with equal canonical changes returns its original
receipt before any other check, even after later batches. Different changes raise
REQUEST_CONFLICT. Canonical JSON is store.canonical. Validation and conflicts reject
the entire batch. Each new successful batch, including a no-op, increments the
generation once. Receipts describe that original generation, not the current read.
The public fault points after_sources and after_views raise and roll back.
Do not treat an incremental semantic mismatch as evidence that SQLite committed
only half a transaction.

All accepted source versions form bags: distinct record IDs may carry identical
rows. Every node must equal evaluation of that batch's final source bags. Output
changes are the signed net difference of the two completed bags, not a sequence
of transient changes. Changing an input's physical order cannot change a result.
The source table and all visible and internal node state belong to one published
generation together with its receipt.

## Plan algebra
A plan is {"nodes": [node,...], "root": node_id}. IDs are unique, all inputs precede
their consumers, any node may feed multiple consumers and an operator may use the
same child on multiple input edges. All nodes are materialized, not only root.
Trusted plans have compatible row widths, valid column indices and scalar types;
the review does not need to add a plan parser. There are no implicit keys,
uniqueness promises, foreign keys or NOT NULL constraints.

* source: {"id":s,"op":"source","types":["s","i",...]}.
  s and i describe nullable strings and integers.
* project: {"id":p,"op":"project","input":a,"cols":[integer,...]}.
  Repeated column positions are legal. Equal projected rows add multiplicities.
* filter: {"id":f,"op":"filter","input":a,"where":expression}.
  Expressions are ["col",index], ["lit",value], ["eq",x,y], ["gt",x,y],
  ["and",p,q], ["or",p,q], ["not",p] and ["isnull",x].
  Types are valid; predicates use SQL three-valued logic and retain only TRUE.
* union: {"id":u,"op":"union","inputs":[a,b,...]} is UNION ALL.
* inner, left, anti: {"id":j,"op":kind,"left":a,"right":b,"on":[[i,j],...]}.
  on is a nonempty conjunction of equalities. NULL never equals anything here,
  including NULL. inner and left concatenate complete left and right rows.
  Every matching pair contributes the product of the two multiplicities. A left
  row with no matching right occurrence contributes one all-NULL right extension
  per left occurrence. anti emits the left bag exactly when no right row matches.
* distinct: {"id":d,"op":"distinct","input":a}; each existing row has weight one.
* group: {"id":g,"op":"group","input":a,"keys":[index,...],"value":index,
  "having":integer_or_null}. The nullable integer value column is aggregated.
  Each output is [group_key_values..., SUM(value), COUNT(*), COUNT(value)] with
  weight one. GROUP BY treats NULL keys as equal. SUM ignores nulls and is NULL
  when COUNT(value)=0, even if COUNT(*) is positive. A zero sum with nonzero
  count is a real group. having=null means no HAVING; otherwise retain exactly
  groups whose SUM is strictly greater than the integer threshold.
  Nonempty keys produce no row when the group's input becomes empty. With keys=[]
  there is always the global group, including [null,0,0] on empty input before
  HAVING. HAVING may suppress that row. Group output is an ordinary bag that may
  feed any later operator, including another grouping or an outer/anti join.

rel.evaluate is the manual full-refresh implementation and is used by initialization
and the baseline comparison. It reads complete relations. Correct audit expectations
must be computed independently of rel and ivm; the checker may use rel as an
additional comparison. It is not an admissible incremental maintenance strategy.
A single changed source ID must not force scanning an unrelated key's rows at each
node. Proposed delta routines may read changed rows, matching opposite join
buckets needed to form changed pairs and left rows whose padding or anti status
actually changes. An existence test must use maintained support counts; if right
support stays positive and the left side did not change, do not reread that left
bucket. DISTINCT support and group accumulators must be updated arithmetically
from deltas; do not rescan a group's members, even for an unkeyed global group.
The work may grow with the actual affected join pairs or absence-boundary results.
Do not expand multiplicities. Source revisions must be coalesced before relation
deltas are propagated.

## Recorded business example
samples.PLAN enriches readings [site,day,value] with sites [site,region] and groups
by [day,region]. samples.NORMAL ends at [["d1","east",19,2,2],1].
In samples.INCIDENT a reading arrives before its site dimension. First read is
[[["d1",null,10,1,1],1]]. After the late dimension the only row must be
[[["d1","east",10,1,1],1]], not a second contribution for the same reading.
python3 checks.py normal exits 0 and prints NORMAL_REGRESSION_OK.
python3 checks.py incident exits 1 with IVM_OUTER_RETRACTION_MISMATCH on this release.
harness.py backs up the committed SQLite database into a standalone new
connection between selected batches and reconstructs all Warehouse objects.
That is a persisted-state reconstruction boundary,
not a claim to simulate a killed operating-system process.

## Review delivery
REVIEW.md identifies root causes and release decisions by source file and line.
Keep causally different defects separate even if one scenario exposes several.
For every issue explain the smallest input history, expected and observed bags,
why a nearby legal history does or does not trigger it and whether another defect
can mask it. An issue that exists only in a receipt still matters. Include both
samples.NORMAL and samples.INCIDENT in the evidence and keep the public mismatch
observable. Add independent isolated and interacting histories for the plan
operators and boundaries named in the request; do not count an exception as a
wrong-result demonstration.

Replace docs/ivm-design.md with a compositional maintenance specification: state
invariants, necessary indexes, old/new bag versions, local delta routines and the
publication boundary. The rules must cover arbitrary legal compositions of this
algebra, not only the recorded plan. Show that the node rules compose over the
shared DAG and retain exact signed net changes. Explain the single affected-key
boundary that drives both correctness and work, including when the downstream
consequence is necessarily large. Give executable-level pseudocode and a proof;
a list of missing tests or a suggestion to invoke full refresh is insufficient.
Distinguish reconstructible source data from existing possibly corrupt node bags,
and explain the backfill and switch conditions needed to install the proposed
state representation while the warehouse continues accepting source revisions.
One initial rebuild may scan the sources; per-batch full refresh is disallowed.
No implementation of that upgrade or corrected operators is requested.

review_evidence.py exposes build(), which reruns original Warehouse.apply/read and
returns a JSON-compatible object. Running python3 review_evidence.py writes
review.json containing the same object. build must compute expected results
without importing rel, ivm, their helpers or rel.evaluate, directly or indirectly.
harness.run may be used only for recorded actuals, not to supply expectations.
A hand-computed expected bag is acceptable for a small isolated case; general
design arguments must not depend on those examples.

The object is {"verdict":"block" or "allow","cases":[case,...]}. A case has:
id, plan, batches, reopen_after, expected, observed, expected_deltas, receipts,
status, locations, note. id is a nonempty unique string. plan and batches use
the public formats. reopen_after lists zero-based batch indices at which to
reconstruct SQLite. expected and observed contain the complete root bag after
every batch; expected_deltas are independent successive net changes, starting
at evaluation of the empty sources. receipts are the actual apply returns.
status is "mismatch" if either observed bags or receipt deltas differ from
expectations, otherwise "control". locations is a nonempty list of file:line
references into rel.py, ivm.py, store.py or harness.py. note explains what this
history isolates. Both review documents reference each case's id.

auditcheck.py replays observations, checks saved and fresh evidence agree, checks
expectations against manual refresh, validates source locations and demonstrates
actual and expected rows. It does not judge the completeness of a causal argument,
independence of the submitted evaluator or proof of the proposed design; those
are reviewed from the delivered source and documents. Do not use its success
message as the entire release justification.

Keep rel.py, ivm.py, store.py, samples.py, harness.py, checks.py and auditcheck.py,
including their existing assertions. The baseline runtime verifies normal
business and demonstrates the incident. At delivery the runtime verify is
["python3","checks.py","verify"] and demo is ["python3","auditcheck.py","demo"];
setup remains an argv-array list. verify retains normal assertions and replays
review.json. demo shows actual warehouse outcomes, not just a completion message.
