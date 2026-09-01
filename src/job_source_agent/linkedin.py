"""Stage 1: LinkedIn job id -> company name and website. The only paid code.

Two ScrapingDog calls. The `linkedinjobs` endpoint turns a job id into a company
name and LinkedIn slug; the `linkedin?type=company` endpoint turns that slug into
the company's website. This mirrors what a person does by hand -- open the job,
click the company, read the website out of the About section.

Nothing else in this project spends credits.
"""
