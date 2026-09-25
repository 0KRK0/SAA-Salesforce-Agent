"""Org knowledge: what the agent has learned about one Salesforce org.

The failure mode this package exists to avoid is the obvious one: stuffing the
org into the context. An enterprise org has thousands of fields, hundreds of
flows and a decade of deployment history. None of that belongs in a prompt.

So knowledge here is an *index*, not a cache of everything:

  * each row is a short summary plus structured data, keyed by kind and name;
  * retrieval is by relevance to the current request, with a hard budget;
  * only observations sourced from the org are stored — a describe, a
    deployment outcome, a diagnosis backed by evidence. What the model asserted
    is never written back as knowledge, because that is how an agent convinces
    itself of something false and then acts on it.
"""
