# brikie — isolated runtime image
#
# The agent's shell and file tools operate inside this container; mount
# only what you want it to touch:
#
#   docker run -it --rm \
#     -v "$PWD":/workspace \
#     -e ANTHROPIC_API_KEY \
#     ghcr.io/veelacleave/brikie --preset anthropic
#
# Local model on the host? Add --network host (Linux) or use
# host.docker.internal as the base URL.

FROM python:3.12-slim

# The tools the agent actually shells out to — kept deliberately small.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ripgrep \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/brikie
COPY pyproject.toml README.md LICENSE ./
COPY brikie ./brikie
RUN pip install --no-cache-dir .

# Non-root: the agent has no business being uid 0 even inside the jail.
RUN useradd --create-home --shell /bin/bash brikie \
    && mkdir -p /workspace \
    && chown brikie:brikie /workspace /opt/brikie
USER brikie
WORKDIR /workspace

ENTRYPOINT ["brikie"]
