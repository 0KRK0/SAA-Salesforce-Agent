"""The deployment lifecycle: change sets, validation, diffs, rollback.

A single-component deploy is a tool call. A *release* is a lifecycle, and this
package is that lifecycle:

    draft → validate (check-only, with tests) → diff against the target org
          → approve → deploy → verify → rollback if needed

The two things that make it more than a wrapper around the Metadata API:

  * **A real diff.** Before anything is deployed, the current state of every
    component is retrieved from the target org and compared. The human
    approving sees what changes, not a list of file names.

  * **A real rollback.** That same retrieve is kept as the rollback plan:
    previously-existing components can be redeployed at their prior source, and
    newly-created ones are removed with a destructive change. Where a component
    cannot be rolled back this way, the plan says so rather than implying an
    undo that does not exist.
"""
