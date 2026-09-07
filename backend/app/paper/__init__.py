"""The PAPER venue: real market data, virtual money, no broker anywhere.

Nothing in this package imports `app.brokers`, `MetaTrader5` or `mt5_paper`.
That is not a convention -- `tests/test_paper.py` parses every module here with
`ast` and asserts the absence, so paper execution cannot reach a venue because
there is no venue reference in the package to reach one with.
"""
