"""The data that moves between stages.

`CompanyIdentity` is what Stage 1 produces from a LinkedIn job id -- the company
name and its website. `JobSourceResult` is what the pipeline returns: the final
listings URL, the hops taken to reach it, and why we believed we had arrived.
"""
