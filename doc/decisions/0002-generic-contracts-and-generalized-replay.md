# Decision 0002: generic operation contracts and generalized replay

- Status: accepted for profile 0.1 (experimental)
- Date: 14 October 2026
- Scope: Sigima, DataLab Desktop, DataLab-Web and DataLab-Capsule
- Supersedes: points 4 and 5 and the replay limits of [decision 0001](0001-processing-chain-foundations.md)

## Context

Decision 0001 qualified replay for two operations only, through tables of operation names in Sigima, provisional filters (no ROI, no uncertainty, real values only) and an exact comparison. Desktop and Web each built their calls, captured and prepared replays in their own code, and dropped the identity of every operation that was not qualified. Most of the Sigima catalogue could not be replayed, and the two editions could diverge.

## Decision

1. **Contracts declared next to each function.** Every public computation function of Sigima declares its identifier, contract version and, when needed, its X-alignment policy, the global options it reads and its replay qualification, in its decorator. The contract structure (roles, parameter arguments, outputs) is inferred from the annotations. Sigima holds no table naming operations.
2. **Qualification backed by executed proofs.** A qualification names the contract version actually tested and its comparison rule. It is proven by the validation test of the function and by a generic replay test, which replays the calls captured during the validation tests as an application replays a recorded activity (parameters through JSON, recorded context, outputs by role). `--qualification-report` fails when a declared qualification is not proven.
3. **Comparison rules declared, measured, never widened.** The rule is `exact` by default. A tolerance is declared only with its justification, from differences measured by replaying the same calls in other environments (NumPy and SciPy versions), and states whether results must stay identical within an identical environment. A result beyond the rule is reported `different`.
4. **Identity is kept; opaque means no identity.** A call names its operation as soon as the computation library knows the function, whether or not the contract is qualified; qualification is checked when replaying, against the local library. Opaque calls are those of functions unknown to the library (e.g. plugins), legacy records and activities recorded without identity by earlier versions.
5. **One scientific path, one recorder.** Sigima describes calls (`describe_call`: identity, roles, encoded parameters, global options read) and prepares executions (`prepare_execution`: parameters decoded, X-alignment policy applied once, global options recorded and applied, detached copies for a replay). DataLab-Capsule provides the only recorder (capture, ledger lifecycle, replay preparation, verification); each edition only gives its accesses and runs the prepared computation (Desktop in its worker process, Web in its worker).
6. **Historical behaviour made explicit.** The X alignment that DataLab historically applied to signals on different grids is a named, versioned policy declared by each operation (operand interpolated on the source grid with constant extension; smallest grid for operations on several signals), recorded and applied again identically by a replay.
7. **Every output is verified.** Signals and images (rows, ROI, units), analysis results (structure and values) and the absence of result of an optional output are compared by role; a verification report lists each output and its verdict is the most severe one. A normal absence of result is recorded, unlike an error or a cancellation.
8. **States and parameters without loss.** `datalab-signal-v2` fingerprints signals whose rows do not share one dtype (float32, complex); image fingerprints cover every numeric dtype. The parameter codec writes typed JSON forms and lists of parameter sets, decoded against the local classes only.

## Consequences

- With Sigima 1.3, 213 of the 215 operations are qualified (175 exact, 38 with a measured tolerance); the phase spectrum is identified but not qualified, and its replay is refused with the reason.
- Patterns come from the contracts only: DataLab-Web derives its catalogue patterns from them, and a DataLab Desktop test checks every registered feature against its contract.
- The format changes are additive within profile 0.1 (new fingerprint scheme, report `outputs`); files recorded earlier stay readable, and their opaque activities are never given an identity afterwards.

## Limits

- Tolerances were measured between two CPython environments on x86-64 (NumPy 1.26 / SciPy 1.10 and NumPy 2.4 / SciPy 1.16). In Pyodide, the cross-edition scenario of each family replays exactly, but the whole catalogue has not been replayed there.
- State fingerprints do not cover metadata: a computation that reads metadata (e.g. the fit parameters used by `evaluate_fit`) is replayed with the current metadata of its input.
- Interactive Desktop fits and the conversion of a markers table to a signal are not operation calls on recorded states and are not recorded.
