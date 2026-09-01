"""Haiku as a link chooser, not a driver.

Given the links on a page and the company we are looking for, the model picks
the one most likely to lead toward job listings. It sees a numbered list and
answers with an index, so its output is validated against the list we sent
before anything acts on it -- the model can be wrong, but it cannot be creative.
"""
