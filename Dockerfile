FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Copy package metadata first for better layer caching
COPY pyproject.toml ./
COPY README.md ./

# Copy source
COPY fantasy_gm ./fantasy_gm

# Install fantasy-gm and its dependencies
RUN pip install --upgrade pip \
    && pip install .

# Run the CLI by default.
ENTRYPOINT ["fantasy-gm"]

# Drop privileges to a non-root user for security.
RUN groupadd -r fantasygm && useradd -r -g fantasygm fantasygm
USER fantasygm

# `docker compose run fantasy-gm` with no args will show help.
CMD ["run"]