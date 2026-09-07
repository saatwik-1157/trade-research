"""The notification engine (L34).

    domain event -> event bus -> consumer -> rule -> preference -> channel

Every arrow in that chain already existed except the last three. The bus is
L07's, the events are the ones the platform already publishes, and the
`NOTIFICATION_CREATED` type and the `user:{id}` channel scope have been in
`app/realtime/catalogue.py` since L07 waiting for a producer.

**What this package may not do**, and a test parses every module here to
prove it: import a risk engine, an order manager, a position manager, a
sizing service or a broker adapter; place, modify or cancel anything; change a
limit, a bot's state or a strategy's. It reads events and writes rows. A bug
in this package can fail to tell somebody something. It cannot trade.

Submodules, in the order the chain uses them:

    contract     the vocabulary: severity, category, channel, delivery status
    catalogue    which events become notifications, and how they are graded
    templates    what the message says, using only recorded fields
    dedup        the same event once; the same condition not fifty times
    preferences  what a user asked for, and the floor they cannot go below
    service      the object that ties those together and writes the rows
    worker       the bus consumer and the delivery queue drainer
    channels/    in-app, email, and the seat L35 fills with Discord
"""

from __future__ import annotations
