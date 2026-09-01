"""Disk cache, so we never pay for the same lookup twice.

ScrapingDog costs credits per call, so Stage 1 results are keyed by job id and
kept permanently. Stage 2 results are keyed by domain, which also keeps the
deployed demo fast when Jobnova tests a company we have already seen.
"""
