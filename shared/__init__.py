"""Helpers shared by the scripts in this repository.

A script inserts the repository root on sys.path and imports from here. Every job
runs out of the same clone, so no install step is involved; the only dependency
outside the standard library is `requests`, which every consumer already pins.
"""
