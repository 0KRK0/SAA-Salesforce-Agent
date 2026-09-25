"""Server-side data analysis.

The rule that shapes this package: analysis happens here, on whole datasets,
and only conclusions reach the model. Ten thousand Accounts are grouped, scored
and summarized in Python; Claude sees "312 duplicate groups, here are the
twenty largest". That is what makes data-quality work possible at org scale
instead of at context-window scale.
"""
