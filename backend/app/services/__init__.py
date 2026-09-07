"""Domain services.

The layer between a route and the database. A route validates, authorizes and
shapes; a service queries and decides. The split matters here for a reason
beyond tidiness: the same service has to be callable from a worker that has no
request, so nothing in this package reads a `Request`, a cookie or a context
variable. Correlation ids are passed in.
"""
