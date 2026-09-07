FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# The build context is the REPOSITORY ROOT, not backend/. It has to be:
# `app/strategies/indicators.py` imports `rule_backtest` and `rule_search` from
# `tools/`, resolved as a sibling of this directory -- so an image built from
# `backend/` alone cannot run the strategy engine, the backtester, the replay
# engine or L23's feature engine. Found by the L24 dependency audit; nothing
# caught it because every gate this project has (pytest, ruff, mypy, CI) runs
# outside the image, and the health check imports none of that code.
#
# See `.dockerignore`: without it the context is ~580MB of node_modules and
# measured data that no image uses.
WORKDIR /srv/backend

COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY backend/alembic ./alembic
COPY backend/alembic.ini backend/pyproject.toml ./

# The research toolkit, at the path `_toolkit()` resolves: /srv/tools. Copied
# rather than vendored, because the whole project rests on the platform CALLING
# these functions instead of reimplementing them -- `test_indicators_match_live`
# exists to keep the two from drifting, and a copy in the image would be exactly
# the drift it guards against.
COPY tools /srv/tools

# Non-root; the image holds no secrets and reads them from the environment.
RUN useradd --create-home --uid 10001 app && chown -R app:app /srv
USER app

# Release identity (L41). Build arguments rather than a file, and stamped LAST
# so a rebuild at a new commit reuses every cached layer above and only redoes
# this one.
#
# An image built without them reports `"stamped": false` on /health, and
# PRODUCTION_READINESS.md treats that as not deployable to production: a
# release you cannot identify is one you cannot confirm or roll back to.
ARG GIT_COMMIT=unknown
ARG BUILD_TIME=unknown
ARG RELEASE_VERSION=unknown
ENV GIT_COMMIT=${GIT_COMMIT}
ENV BUILD_TIME=${BUILD_TIME}
ENV RELEASE_VERSION=${RELEASE_VERSION}

EXPOSE 8000

# No `--reload`, no `--workers`. One uvicorn process per container: the process
# holds the safe-mode latch, the step-up grants and the in-process rate limiter,
# and `--workers N` would fork N copies of all three that cannot see each other.
# Scale by adding containers, which is what the compose overlay does.
#
# uvicorn handles SIGTERM by running the lifespan's shutdown -- workers stopped
# before the hub, the hub before the bus -- which is why `stop_grace_period` in
# the production overlay is 60s rather than Docker's default 10s.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
