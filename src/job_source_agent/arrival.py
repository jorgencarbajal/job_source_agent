"""Have we reached a job listings page? Code decides this, not the model.

Two signals. First, the host: `myworkdayjobs.com`, `fa.ocs.oraclecloud.com`,
`icims.com` and friends are job boards by construction, and recognising them
needs no guessing. Second, the page: rendered job rows, listing-shaped URLs,
and the company name where it happens to appear.

Deliberately scored rather than gated. Real boards fail any single check --
Honeywell's board is titled "Aerospace", Esri's says "IP Global Career Site",
and Workiva's title is empty -- so no one signal gets a veto.
"""
